[CmdletBinding()]
param(
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$LakehouseId = $env:FABRIC_LAKEHOUSE_ID,
    [string]$EnginePath = "$PSScriptRoot\..\engine\narrative_premium.py",
    [string]$EngineTargetPath = "Files/config/e22/09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea.py",
    [string]$AnchorEnginePath = "$PSScriptRoot\..\engine\fundamental_anchor.py",
    [string]$AnchorEngineTargetPath = "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py",
    [string]$OpportunityEnginePath = "$PSScriptRoot\..\engine\thesis.py",
    [string]$OpportunityEngineTargetPath = "Files/config/e14/c2e46ed74b73c478528b4b39177990e988f9477dbd1be91c9d756eb5b844adab.py",
    [string]$AnchorNotebookPath = "$PSScriptRoot\..\fabric\nb_09_fundamental_anchor.Notebook",
    [string]$E8NotebookPath = "$PSScriptRoot\..\fabric\nb_05_alpha_vantage_to_gold.Notebook",
    [string]$NotebookPath = "$PSScriptRoot\..\fabric\nb_12_narrative_premium.Notebook",
    [string]$MetricsNotebookPath = "$PSScriptRoot\..\fabric\nb_04_metrics.Notebook"
)

$ErrorActionPreference = "Stop"
if (-not $WorkspaceId -or -not $LakehouseId) { throw "WorkspaceId and LakehouseId are required" }
$fabricResource = "https://api.fabric.microsoft.com"
$apiBase = "$fabricResource/v1/workspaces/$WorkspaceId"

function Get-Sha256([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToHexString($sha.ComputeHash($Bytes)).ToLower() }
    finally { $sha.Dispose() }
}

function Publish-Engine([string]$SourcePath, [string]$TargetPath) {
    $source = [IO.File]::ReadAllText((Resolve-Path $SourcePath)) -replace "`r`n", "`n"
    $bytes = [Text.Encoding]::UTF8.GetBytes($source)
    $expectedHash = Get-Sha256 $bytes
    $addressHash = [IO.Path]::GetFileNameWithoutExtension($TargetPath).ToLower()
    if ($addressHash -ne $expectedHash) {
        throw "Content-addressed target mismatch: path=$TargetPath expectedHash=$expectedHash"
    }
    $storageToken = az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv
    if (-not $storageToken) { throw "OneLake access token is unavailable" }

    $uri = "https://onelake.dfs.fabric.microsoft.com/$WorkspaceId/$LakehouseId/$TargetPath"
    $targetDirectory = [IO.Path]::GetDirectoryName($TargetPath).Replace("\", "/")
    $client = [Net.Http.HttpClient]::new()
    $client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $storageToken)
    $client.DefaultRequestHeaders.Add("x-ms-version", "2023-11-03")
    try {
        foreach ($directory in @("Files/config", $targetDirectory)) {
            $directoryUri = "https://onelake.dfs.fabric.microsoft.com/$WorkspaceId/$LakehouseId/$directory`?resource=directory"
            $directoryResponse = $client.PutAsync($directoryUri, $null).GetAwaiter().GetResult()
            if (-not $directoryResponse.IsSuccessStatusCode -and [int]$directoryResponse.StatusCode -ne 409) {
                [void]$directoryResponse.EnsureSuccessStatusCode()
            }
        }
        $create = $client.PutAsync("$uri`?resource=file", $null).GetAwaiter().GetResult()
        [void]$create.EnsureSuccessStatusCode()
        $append = $client.PatchAsync(
            "$uri`?action=append&position=0",
            [Net.Http.ByteArrayContent]::new($bytes)
        ).GetAwaiter().GetResult()
        [void]$append.EnsureSuccessStatusCode()
        $flush = $client.PatchAsync(
            "$uri`?action=flush&position=$($bytes.Length)",
            $null
        ).GetAwaiter().GetResult()
        [void]$flush.EnsureSuccessStatusCode()
        $remoteBytes = $client.GetByteArrayAsync($uri).GetAwaiter().GetResult()
    } finally {
        $client.Dispose()
        Remove-Variable storageToken
    }
    $actualHash = Get-Sha256 $remoteBytes
    if ($actualHash -ne $expectedHash) {
        throw "Engine hash mismatch for $TargetPath`: expected=$expectedHash actual=$actualHash"
    }
    return [ordered]@{
        path = $TargetPath
        bytes = $bytes.Length
        sha256 = $actualHash
    }
}

function Invoke-FabricRequest([string]$Method, [string]$Uri, [object]$Body = $null) {
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $script:headers
        SkipHttpErrorCheck = $true
    }
    if ($null -ne $Body) { $parameters.Body = $Body | ConvertTo-Json -Depth 20 -Compress }
    $response = Invoke-WebRequest @parameters
    if ([int]$response.StatusCode -ge 400) {
        throw "Fabric request failed ($($response.StatusCode)) $Uri`: $($response.Content)"
    }
    if ([int]$response.StatusCode -ne 202) {
        return $response.Content | ConvertFrom-Json
    }
    $operationUri = @($response.Headers.Location)[0]
    if (-not $operationUri) { $operationUri = @($response.Headers["Operation-Location"])[0] }
    if (-not $operationUri) { throw "Fabric accepted the request without an operation URL" }
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Seconds 2
        try { $operation = Invoke-RestMethod -Method Get -Uri $operationUri -Headers $script:headers }
        catch { continue }
        if ($operation.status -eq "Succeeded") {
            try { return Invoke-RestMethod -Method Get -Uri ($operationUri.TrimEnd("/") + "/result") -Headers $script:headers }
            catch { return $operation }
        }
        if ($operation.status -eq "Failed") {
            throw "Fabric operation failed: $($operation | ConvertTo-Json -Depth 20 -Compress)"
        }
    }
    throw "Fabric operation did not finish within 120 checks"
}

function Get-DefinitionParts([string]$Path) {
    $resolvedRoot = (Resolve-Path $Path).Path
    return @(
        Get-ChildItem $resolvedRoot -File -Recurse |
            Where-Object { $_.Name -in @(".platform", "notebook-content.py") } |
            Sort-Object FullName |
            ForEach-Object {
                $relativePath = [IO.Path]::GetRelativePath($resolvedRoot, $_.FullName).Replace("\", "/")
                $text = [IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n"
                [ordered]@{
                    path = $relativePath
                    payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
                    payloadType = "InlineBase64"
                }
            }
    )
}

function Deploy-Notebook([string]$DisplayName, [string]$Path, [object]$Items) {
    $matches = @($Items.value | Where-Object { $_.displayName -eq $DisplayName -and $_.type -eq "Notebook" })
    if ($matches.Count -gt 1) { throw "Multiple $DisplayName notebooks exist" }
    $definition = @{ parts = Get-DefinitionParts $Path }
    if ($matches.Count -eq 1) {
        $item = $matches[0]
        $result = Invoke-FabricRequest -Method Post -Uri "$apiBase/items/$($item.id)/updateDefinition?updateMetadata=true" -Body @{ definition = $definition }
        return [ordered]@{ displayName = $DisplayName; id = $item.id; action = "updated"; result = $result }
    }
    $result = Invoke-FabricRequest -Method Post -Uri "$apiBase/items" -Body @{
        displayName = $DisplayName
        type = "Notebook"
        definition = $definition
    }
    return [ordered]@{ displayName = $DisplayName; id = $result.id; action = "created"; result = $result }
}

function Remove-ObsoleteEngineFiles([string[]]$Paths) {
    $storageToken = az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv
    if (-not $storageToken) { throw "OneLake access token is unavailable" }
    $client = [Net.Http.HttpClient]::new()
    $client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $storageToken)
    $client.DefaultRequestHeaders.Add("x-ms-version", "2023-11-03")
    try {
        foreach ($path in $Paths) {
            $uri = "https://onelake.dfs.fabric.microsoft.com/$WorkspaceId/$LakehouseId/$path"
            $response = $client.DeleteAsync($uri).GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode -and [int]$response.StatusCode -ne 404) {
                [void]$response.EnsureSuccessStatusCode()
            }
        }
    } finally {
        $client.Dispose()
        Remove-Variable storageToken
    }
}

$anchorEngine = Publish-Engine -SourcePath $AnchorEnginePath -TargetPath $AnchorEngineTargetPath
$premiumEngine = Publish-Engine -SourcePath $EnginePath -TargetPath $EngineTargetPath
$opportunityEngine = Publish-Engine -SourcePath $OpportunityEnginePath -TargetPath $OpportunityEngineTargetPath
$fabricToken = az account get-access-token --resource $fabricResource --query accessToken -o tsv
if (-not $fabricToken) { throw "Fabric access token is unavailable" }
$script:headers = @{ Authorization = "Bearer $fabricToken"; "Content-Type" = "application/json" }
try {
    $items = Invoke-FabricRequest -Method Get -Uri "$apiBase/items"
    $notebooks = @(
        Deploy-Notebook -DisplayName "nb_05_alpha_vantage_to_gold" -Path $E8NotebookPath -Items $items
        Deploy-Notebook -DisplayName "nb_09_fundamental_anchor" -Path $AnchorNotebookPath -Items $items
        Deploy-Notebook -DisplayName "nb_12_narrative_premium" -Path $NotebookPath -Items $items
        Deploy-Notebook -DisplayName "nb_04_metrics" -Path $MetricsNotebookPath -Items $items
    )
    Remove-ObsoleteEngineFiles @(
        "Files/config/e20/fundamental_anchor_e20_v1.py",
        "Files/config/e20/fundamental_anchor_e20_v2.py",
        "Files/config/e22/narrative_premium_e22_v1.py",
        "Files/config/e22/narrative_premium_e22_v2.py",
        "Files/config/e22/narrative_premium_e22_v3.py",
        "Files/config/e14/thesis_e6b_v1.py",
        "Files/config/e14/077f0df38891ec93265f1b830ed9e2890dedafa72ed6208ad465dc37213aac4d.py",
        "Files/config/e14/73ed4554c6e81f041b6322e34952aaf397f063406d3b5b560dcd242a827f722a.py",
        "Files/config/e14/35572486e85678d9af3f7adf225a0672e84b3f268c4994aa58755fb5eb00d241.py"
    )
    [ordered]@{
        engines = [ordered]@{ e20 = $anchorEngine; e22 = $premiumEngine; e14 = $opportunityEngine }
        notebooks = $notebooks
    } | ConvertTo-Json -Depth 12
} finally {
    Remove-Variable fabricToken
    Remove-Variable headers -Scope Script
}