[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("dev", "prod")]
    [string]$Environment,
    [string]$ResourceGroup = "",
    [string]$FunctionAppName = "",
    [ValidateRange(1, 100)]
    [int]$PageSize = 20,
    [ValidateRange(1, 8)]
    [int]$MaxWorkers = 2,
    [string]$AfterId = "",
    [string]$StatePath = "$PSScriptRoot\..\.backfill\e21-narrative-extraction.jsonl"
)

$ErrorActionPreference = "Stop"
if (-not $ResourceGroup) { $ResourceGroup = "auspex-$Environment-ingest" }
if (-not $FunctionAppName) { $FunctionAppName = "auspex-$Environment-func" }
$keys = az functionapp keys list --resource-group $ResourceGroup --name $FunctionAppName -o json | ConvertFrom-Json
$functionKey = $keys.functionKeys.default
if (-not $functionKey) { throw "Function host key is unavailable" }
$baseUri = "https://$FunctionAppName.azurewebsites.net/api"
$stateDirectory = Split-Path -Parent $StatePath
if ($stateDirectory -and -not (Test-Path $stateDirectory)) {
    New-Item -ItemType Directory -Path $stateDirectory | Out-Null
}

$cursor = $AfterId
$pages = 0
$documents = 0
$scored = 0
$cacheHits = 0
do {
    $body = @{
        limit = $PageSize
        max_workers = $MaxWorkers
        after_id = $cursor
    } | ConvertTo-Json -Compress
    $result = $null
    for ($attempt = 0; $attempt -lt 5 -and $null -eq $result; $attempt++) {
        try {
            $result = Invoke-RestMethod -Method Post `
                -Uri "$baseUri/score_narrative_features?code=$functionKey" `
                -ContentType "application/json" -Body $body
        } catch {
            if ($attempt -eq 4) { throw }
            Start-Sleep -Seconds ([Math]::Min([Math]::Pow(2, $attempt), 8))
        }
    }
    if ($result.status -ne "ok") { throw "E21 extraction page failed" }
    $pages++
    $documents += [int]$result.documents
    $scored += [int]$result.scored
    $cacheHits += [int]$result.cache_hits
    $cursor = [string]$result.next_after_id
    [ordered]@{
        timestamp = [DateTime]::UtcNow.ToString("o")
        page = $pages
        documents = $result.documents
        scored = $result.scored
        cache_hits = $result.cache_hits
        next_after_id = $cursor
        has_more = $result.has_more
    } | ConvertTo-Json -Compress | Add-Content -Path $StatePath -Encoding utf8
} while ($result.has_more)

$publish = Invoke-RestMethod -Method Post `
    -Uri "$baseUri/publish_narrative_features?code=$functionKey" `
    -ContentType "application/json" -Body "{}"

[ordered]@{
    pages = $pages
    documents = $documents
    scored = $scored
    cache_hits = $cacheHits
    final_cursor = $cursor
    publication = $publish
} | ConvertTo-Json -Depth 10

Remove-Variable functionKey