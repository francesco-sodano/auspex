[CmdletBinding()]
param(
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$NotebookPath = "$PSScriptRoot\..\fabric\nb_11_narrative_intensity.Notebook",
    [string]$MetricsNotebookPath = "$PSScriptRoot\..\fabric\nb_04_metrics.Notebook"
)

$ErrorActionPreference = "Stop"
if (-not $WorkspaceId) { throw "WorkspaceId or FABRIC_WORKSPACE_ID is required" }
$fabricResource = "https://api.fabric.microsoft.com"
$apiBase = "$fabricResource/v1/workspaces/$WorkspaceId"
$token = az account get-access-token --resource $fabricResource --query accessToken -o tsv
if (-not $token) { throw "Fabric access token is unavailable" }
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

function Invoke-FabricRequest([string]$Method, [string]$Uri, [object]$Body = $null) {
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $headers
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
        try { $operation = Invoke-RestMethod -Method Get -Uri $operationUri -Headers $headers }
        catch { continue }
        if ($operation.status -eq "Succeeded") {
            try { return Invoke-RestMethod -Method Get -Uri ($operationUri.TrimEnd("/") + "/result") -Headers $headers }
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
        Where-Object { $_.Extension -ne ".pyc" -and $_.FullName -notmatch "[\\/]__pycache__[\\/]" } |
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

$items = Invoke-FabricRequest -Method Get -Uri "$apiBase/items"
@(
    Deploy-Notebook -DisplayName "nb_11_narrative_intensity" -Path $NotebookPath -Items $items
    Deploy-Notebook -DisplayName "nb_04_metrics" -Path $MetricsNotebookPath -Items $items
) | ConvertTo-Json -Depth 12

Remove-Variable token