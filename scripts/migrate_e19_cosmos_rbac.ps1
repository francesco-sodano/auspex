[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("dev", "prod")]
    [string]$Environment = "dev",

    [string]$SubscriptionId = $env:AZURE_SUBSCRIPTION_ID
)

$ErrorActionPreference = "Stop"
if (-not $SubscriptionId) { throw "SubscriptionId or AZURE_SUBSCRIPTION_ID is required" }

$sharedResourceGroup = "auspex-$Environment-shared"
$ingestResourceGroup = "auspex-$Environment-ingest"
$webResourceGroup = "auspex-$Environment-web"
$cosmosAccount = "auspex-$Environment-cosmos"
$ingestFunction = "auspex-$Environment-func"
$webFunction = "auspex-$Environment-wapi"
$databaseName = "auspex"
$contributorRoleSuffix = "/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
$readerRoleSuffix = "/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001"

function Get-FunctionPrincipalId([string]$ResourceGroup, [string]$FunctionName) {
    $principalId = az functionapp identity show `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $FunctionName `
        --query principalId `
        --output tsv
    if (-not $principalId) {
        throw "Could not resolve managed identity for $FunctionName"
    }
    return $principalId
}

function Assert-NarrowAssignments(
    [object[]]$Assignments,
    [string]$PrincipalId,
    [hashtable]$ContainerRoles
) {
    foreach ($containerName in $ContainerRoles.Keys) {
        $expectedScopeSuffix = "/dbs/$databaseName/colls/$containerName"
        $expectedRoleSuffix = $ContainerRoles[$containerName]
        $match = @($Assignments | Where-Object {
            $_.principalId -eq $PrincipalId `
                -and $_.roleDefinitionId.EndsWith($expectedRoleSuffix) `
                -and $_.scope.EndsWith($expectedScopeSuffix)
        })
        if ($match.Count -ne 1) {
            throw "Expected one assignment with role $expectedRoleSuffix for principal $PrincipalId on $expectedScopeSuffix; found $($match.Count)"
        }
    }
}

$accountId = az cosmosdb show `
    --subscription $SubscriptionId `
    --resource-group $sharedResourceGroup `
    --name $cosmosAccount `
    --query id `
    --output tsv
if (-not $accountId) {
    throw "Could not resolve Cosmos account $cosmosAccount"
}

$ingestPrincipalId = Get-FunctionPrincipalId $ingestResourceGroup $ingestFunction
$webPrincipalId = Get-FunctionPrincipalId $webResourceGroup $webFunction
$assignments = @(
    az cosmosdb sql role assignment list `
        --subscription $SubscriptionId `
        --resource-group $sharedResourceGroup `
        --account-name $cosmosAccount `
        --output json | ConvertFrom-Json
)

Assert-NarrowAssignments $assignments $ingestPrincipalId @{
    sources = $contributorRoleSuffix
    watermarks = $contributorRoleSuffix
    runs = $contributorRoleSuffix
    dedup = $contributorRoleSuffix
    security_catalog = $contributorRoleSuffix
    market_data = $contributorRoleSuffix
    ingestion_universe = $readerRoleSuffix
    portfolio_transactions = $readerRoleSuffix
}
Assert-NarrowAssignments $assignments $webPrincipalId @{
    app_users = $contributorRoleSuffix
    portfolio_transactions = $contributorRoleSuffix
    security_catalog = $readerRoleSuffix
    market_data = $readerRoleSuffix
    ingestion_universe = $contributorRoleSuffix
}

$legacyAssignments = @($assignments | Where-Object {
    $_.principalId -in @($ingestPrincipalId, $webPrincipalId) `
        -and $_.scope -eq $accountId
})

foreach ($assignment in $legacyAssignments) {
    if ($PSCmdlet.ShouldProcess($assignment.id, "Delete legacy account-scoped Cosmos role assignment")) {
        az cosmosdb sql role assignment delete `
            --subscription $SubscriptionId `
            --resource-group $sharedResourceGroup `
            --account-name $cosmosAccount `
            --role-assignment-id $assignment.name `
            --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to delete legacy Cosmos role assignment $($assignment.name)"
        }
    }
}

Write-Host "Verified narrow Cosmos RBAC and removed $($legacyAssignments.Count) legacy account-scoped Function assignment(s)."