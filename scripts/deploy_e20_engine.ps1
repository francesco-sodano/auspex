[CmdletBinding()]
param(
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$LakehouseId = $env:FABRIC_LAKEHOUSE_ID,
    [string]$SourcePath = "$PSScriptRoot\..\engine\fundamental_anchor.py",
    [string]$TargetPath = "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py"
)

$ErrorActionPreference = "Stop"
if (-not $WorkspaceId -or -not $LakehouseId) { throw "WorkspaceId and LakehouseId are required" }
$source = [IO.File]::ReadAllText((Resolve-Path $SourcePath)) -replace "`r`n", "`n"
$bytes = [Text.Encoding]::UTF8.GetBytes($source)
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $expectedHash = [Convert]::ToHexString($sha.ComputeHash($bytes)).ToLower()
} finally {
    $sha.Dispose()
}

$token = az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv
if (-not $token) { throw "OneLake access token is unavailable" }

$uri = "https://onelake.dfs.fabric.microsoft.com/$WorkspaceId/$LakehouseId/$TargetPath"
$client = [Net.Http.HttpClient]::new()
$client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $token)
$client.DefaultRequestHeaders.Add("x-ms-version", "2023-11-03")
try {
    foreach ($directory in @("Files/config", "Files/config/e20")) {
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
    Remove-Variable token
}

$remoteSha = [Security.Cryptography.SHA256]::Create()
try {
    $actualHash = [Convert]::ToHexString($remoteSha.ComputeHash($remoteBytes)).ToLower()
} finally {
    $remoteSha.Dispose()
}
if ($actualHash -ne $expectedHash) {
    throw "E20 engine hash mismatch: expected=$expectedHash actual=$actualHash"
}

[pscustomobject]@{
    path = $TargetPath
    bytes = $bytes.Length
    sha256 = $actualHash
} | ConvertTo-Json