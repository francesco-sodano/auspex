[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("dev", "prod")]
    [string]$Environment,
    [string]$IngestionAppName = "",
    [string]$WebApiAppName = "",
    [switch]$IngestionOnly,
    [switch]$WebApiOnly
)

$ErrorActionPreference = "Stop"
if ($IngestionOnly -and $WebApiOnly) {
    throw "IngestionOnly and WebApiOnly cannot be used together"
}
if (-not $IngestionAppName) { $IngestionAppName = "auspex-$Environment-func" }
if (-not $WebApiAppName) { $WebApiAppName = "auspex-$Environment-wapi" }
$repositoryRoot = (Resolve-Path "$PSScriptRoot\..").Path
$stagingRoot = Join-Path ([IO.Path]::GetTempPath()) "auspex-e7-$([Guid]::NewGuid())"

function New-FunctionBundle([string]$AppSource, [string]$Target) {
    New-Item -ItemType Directory -Path $Target | Out-Null
    Copy-Item (Join-Path $repositoryRoot $AppSource "*") $Target -Recurse
    Copy-Item (Join-Path $repositoryRoot "search") $Target -Recurse
    Copy-Item (Join-Path $repositoryRoot "engine") $Target -Recurse
    Copy-Item (Join-Path $repositoryRoot "agent") $Target -Recurse
    Copy-Item (Join-Path $repositoryRoot "prompts") $Target -Recurse
    Get-ChildItem $Target -Recurse -Force | Where-Object {
        $_.Name -in @("local.settings.json", ".env") -or
        $_.Extension -in @(".pfx", ".pem", ".key", ".crt", ".cer", ".pyc") -or
        ($_.PSIsContainer -and $_.Name -eq "__pycache__")
    } | Sort-Object FullName -Descending | Remove-Item -Recurse -Force
}

function Publish-FunctionBundle([string]$AppName, [string]$BundlePath) {
    Push-Location $BundlePath
    try {
        func azure functionapp publish $AppName --python --build remote
        if ($LASTEXITCODE -ne 0) { throw "Function deployment failed for $AppName" }
    } finally {
        Pop-Location
    }
}

try {
    if (-not $WebApiOnly) {
        $ingestionBundle = Join-Path $stagingRoot "ingestion"
        New-FunctionBundle "connectors" $ingestionBundle
        Publish-FunctionBundle $IngestionAppName $ingestionBundle
    }
    if (-not $IngestionOnly) {
        $webApiBundle = Join-Path $stagingRoot "web-api"
        New-FunctionBundle "api" $webApiBundle
        Publish-FunctionBundle $WebApiAppName $webApiBundle
    }
} finally {
    if (Test-Path $stagingRoot) {
        Remove-Item $stagingRoot -Recurse -Force
    }
}