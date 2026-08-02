[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AsOfDate,
    [Parameter(Mandatory = $true)]
    [ValidateSet("dev", "prod")]
    [string]$Environment,
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$FabricCapacityResourceGroup = "",
    [string]$FabricCapacityName = "",
    [string]$ReleaseRunId = ""
)

$ErrorActionPreference = "Stop"
if (-not $WorkspaceId) { throw "WorkspaceId or FABRIC_WORKSPACE_ID is required" }
if (-not $FabricCapacityResourceGroup) { $FabricCapacityResourceGroup = "auspex-$Environment-data" }
if (-not $FabricCapacityName) { $FabricCapacityName = "auspex$($Environment)fab" }
$parsedAsOf = [DateTime]::ParseExact($AsOfDate, "yyyy-MM-dd", $null)
if ($parsedAsOf.Date -gt [DateTime]::UtcNow.Date) {
    throw "AsOfDate cannot be in the future"
}
if (-not $ReleaseRunId) {
    $ReleaseRunId = "e22-$($parsedAsOf.ToString('yyyyMMdd'))-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
}

$fabricResource = "https://api.fabric.microsoft.com"
$apiBase = "$fabricResource/v1/workspaces/$WorkspaceId"
$subscriptionId = az account show --query id -o tsv
$capacityId = "/subscriptions/$subscriptionId/resourceGroups/$FabricCapacityResourceGroup/providers/Microsoft.Fabric/capacities/$FabricCapacityName"

function Get-FabricHeaders {
    $token = az account get-access-token --resource $fabricResource --query accessToken -o tsv
    if (-not $token) { throw "Fabric access token is unavailable" }
    return @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
}

function Get-NotebookItems([object]$Headers) {
    $items = Invoke-RestMethod -Method Get -Uri "$apiBase/items" -Headers $Headers
    $required = @("nb_09_fundamental_anchor", "nb_11_narrative_intensity", "nb_12_narrative_premium", "nb_04_metrics")
    $result = @{}
    foreach ($name in $required) {
        $matches = @($items.value | Where-Object { $_.displayName -eq $name -and $_.type -eq "Notebook" })
        if ($matches.Count -ne 1) { throw "Expected one Fabric Notebook named $name, found $($matches.Count)" }
        $result[$name] = $matches[0].id
    }
    return $result
}

function Assert-NoActiveJobs([hashtable]$NotebookIds, [object]$Headers) {
    foreach ($entry in $NotebookIds.GetEnumerator()) {
        $jobs = Invoke-RestMethod -Method Get -Uri "$apiBase/items/$($entry.Value)/jobs/instances" -Headers $Headers
        $active = @($jobs.value | Where-Object { $_.status -in @("NotStarted", "InProgress") })
        if ($active.Count) {
            throw "Notebook $($entry.Key) already has $($active.Count) active job(s); E22 orchestration is serialized"
        }
    }
}

function Invoke-Notebook(
    [string]$Name,
    [string]$ItemId,
    [hashtable]$Parameters,
    [object]$Headers
) {
    $request = @{
        Method = "Post"
        Uri = "$apiBase/items/$ItemId/jobs/instances?jobType=RunNotebook"
        Headers = $Headers
    }
    if ($Parameters.Count) {
        $parameterMap = @{}
        foreach ($entry in $Parameters.GetEnumerator()) {
            $parameterMap[$entry.Key] = @{ value = [string]$entry.Value; type = "string" }
        }
        $request.Body = @{ executionData = @{ parameters = $parameterMap } } | ConvertTo-Json -Depth 10 -Compress
    }
    $response = Invoke-WebRequest @request
    $location = @($response.Headers.Location)[0]
    if (-not $location) { throw "Fabric did not return a job URL for $Name" }
    $jobId = $location.TrimEnd("/").Split("/")[-1]

    do {
        Start-Sleep -Seconds 5
        $job = Invoke-RestMethod -Method Get -Uri "$apiBase/items/$ItemId/jobs/instances/$jobId" -Headers $Headers
    } while ($job.status -in @("NotStarted", "InProgress"))
    if ($job.status -ne "Completed") {
        throw "$Name failed: $($job.failureReason | ConvertTo-Json -Depth 10 -Compress)"
    }

    $extended = Invoke-RestMethod -Method Get -Uri "$apiBase/notebooks/$ItemId/jobs/execute/instances/$jobId`?beta=true" -Headers $Headers
    return [ordered]@{
        notebook = $Name
        itemId = $ItemId
        jobId = $jobId
        status = $job.status
        exitValue = $extended.properties.exitValue
    }
}

$runs = @()
try {
    az rest --method post --url "https://management.azure.com$capacityId/resume?api-version=2023-11-01" --output none
    $headers = Get-FabricHeaders
    $notebooks = Get-NotebookItems $headers
    Assert-NoActiveJobs $notebooks $headers

    $runs += Invoke-Notebook "nb_09_fundamental_anchor" $notebooks["nb_09_fundamental_anchor"] @{
        from_date = $AsOfDate
        to_date = $AsOfDate
        max_anchor_dates = "1"
    } $headers
    $runs += Invoke-Notebook "nb_11_narrative_intensity" $notebooks["nb_11_narrative_intensity"] @{
        as_of_date = $AsOfDate
    } $headers
    $runs += Invoke-Notebook "nb_12_narrative_premium" $notebooks["nb_12_narrative_premium"] @{
        as_of_date = $AsOfDate
    } $headers
    $runs += Invoke-Notebook "nb_04_metrics" $notebooks["nb_04_metrics"] @{
        priority_as_of_date = $AsOfDate
    } $headers

    $warehouseOutput = & "$PSScriptRoot\..\.venv\Scripts\python.exe" `
        "$PSScriptRoot\deploy_e22_warehouse.py" `
        --as-of $AsOfDate `
        --gold-promotion-run-id $ReleaseRunId
    if ($LASTEXITCODE -ne 0) { throw "E22 Warehouse release failed" }

    [ordered]@{
        asOfDate = $AsOfDate
        releaseRunId = $ReleaseRunId
        notebookRuns = $runs
        warehouse = $warehouseOutput | ConvertFrom-Json
    } | ConvertTo-Json -Depth 20
} finally {
    az rest --method post --url "https://management.azure.com$capacityId/suspend?api-version=2023-11-01" --output none
}