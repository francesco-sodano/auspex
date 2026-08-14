$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

if ($env:AUSPEX_EXISTING_KEY_VAULT_NAME) {
    $rbacEnabled = az keyvault show `
        --name $env:AUSPEX_EXISTING_KEY_VAULT_NAME `
        --resource-group $env:AUSPEX_EXISTING_KEY_VAULT_RESOURCE_GROUP `
        --query properties.enableRbacAuthorization `
        --output tsv
    if ($rbacEnabled -ne "true") {
        throw "Existing Key Vault must use Azure RBAC authorization."
    }
}

if ($env:AUSPEX_MANAGE_ENTRA_REDIRECT_URI -eq "false") {
    Write-Host "Skipping Entra redirect URI management."
    exit 0
}

if (-not $env:AUSPEX_AUTH_CLIENT_ID -or -not $env:SERVICE_API_URI) {
    throw "AUSPEX_AUTH_CLIENT_ID and SERVICE_API_URI are required after provisioning."
}

$objectId = az ad app show --id $env:AUSPEX_AUTH_CLIENT_ID --query id --output tsv
if (-not $objectId) { throw "The configured Entra application was not found." }
$currentRedirects = @(
    az ad app show `
        --id $env:AUSPEX_AUTH_CLIENT_ID `
        --query spa.redirectUris `
        --output json | ConvertFrom-Json
)
$redirects = @(
    $currentRedirects
    "http://localhost:5173"
    $env:SERVICE_API_URI.TrimEnd("/")
) | Sort-Object -Unique
$body = @{
    spa = @{
        redirectUris = $redirects
    }
} | ConvertTo-Json -Depth 4 -Compress

az rest --method PATCH `
    --uri "https://graph.microsoft.com/v1.0/applications/$objectId" `
    --headers "Content-Type=application/json" `
    --body $body `
    --output none

Write-Host "Configured Entra SPA redirect URI: $($env:SERVICE_API_URI)"
