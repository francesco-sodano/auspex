param(
    [string]$EnvironmentName = "dev",
    [string]$Location = "switzerlandnorth",
    [string]$OpenAiLocation = "swedencentral",
    [string]$PypiIndexUrl = "https://pypi.org/simple",
    # "workforce" uses the signed-in Azure tenant (organisational accounts only).
    # "external" targets a Microsoft Entra External ID tenant, which is what allows
    # sign-up with personal Gmail/Outlook addresses through a sign-up/sign-in user
    # flow. An external tenant is a separate directory: it is NOT created here, and
    # its app registration must already exist (see README "External tenant setup").
    [ValidateSet("workforce", "external")]
    [string]$AuthTenantType = "workforce",
    [string]$AuthTenantId = "",
    [string]$AuthTenantSubdomain = "",
    [string]$AuthClientId = "",
    [string]$InitialAdminEmail = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

function Get-AzdValue([string]$Name) {
    $previous = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        $value = azd env get-value $Name 2>$null
        if ($LASTEXITCODE -ne 0) { return "" }
        return ($value | Out-String).Trim()
    }
    finally {
        $PSNativeCommandUseErrorActionPreference = $previous
    }
}

function Set-AzdDefault([string]$Name, [string]$Value) {
    if (-not (Get-AzdValue $Name)) {
        azd env set $Name $Value | Out-Null
    }
}

function Read-Required([string]$Prompt, [string]$Default = "") {
    $suffix = if ($Default) { " [$Default]" } else { "" }
    $value = Read-Host "$Prompt$suffix"
    if (-not $value) { $value = $Default }
    if (-not $value) { throw "$Prompt is required." }
    return $value
}

function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

az account show --output none
if ($LASTEXITCODE -ne 0) { throw "Run 'az login' before configuring Auspex." }

$knownEnvironments = azd env list --output json | ConvertFrom-Json
if ($knownEnvironments.Name -contains $EnvironmentName) {
    azd env select $EnvironmentName | Out-Null
}
else {
    azd env new $EnvironmentName --no-prompt | Out-Null
}

$subscriptionId = az account show --query id --output tsv
$tenantId = az account show --query tenantId --output tsv
$ownerObjectId = az ad signed-in-user show --query id --output tsv
if (-not $subscriptionId -or -not $tenantId) {
    throw "Could not resolve Azure subscription or tenant."
}
if ($AuthTenantType -eq "workforce" -and -not $ownerObjectId) {
    throw "Could not resolve the signed-in workforce owner."
}

azd env set AZURE_SUBSCRIPTION_ID $subscriptionId | Out-Null
azd env set AZURE_LOCATION $Location | Out-Null
azd env set AZURE_OPENAI_LOCATION $OpenAiLocation | Out-Null
azd env set AUSPEX_PYPI_INDEX_URL $PypiIndexUrl | Out-Null
azd env set AUSPEX_AUTH_TENANT_TYPE $AuthTenantType | Out-Null

if ($AuthTenantType -eq "external") {
    azd env set AUSPEX_OWNER_OBJECT_ID "" | Out-Null
    # The external tenant is a separate directory that Bicep cannot provision:
    # tenant creation is a directory operation, not an ARM resource. Its details
    # are supplied here and only ever parameterised downstream.
    if (-not $AuthTenantId) { $AuthTenantId = Read-Required "External tenant ID (GUID)" }
    if (-not $AuthTenantSubdomain) {
        $AuthTenantSubdomain = Read-Required "External tenant subdomain (the 'contoso' in contoso.ciamlogin.com)"
    }
    if (-not $AuthClientId) {
        $AuthClientId = Read-Required "Application (client) ID registered in the external tenant"
    }
    azd env set AUSPEX_AUTH_TENANT_ID $AuthTenantId | Out-Null
    azd env set AUSPEX_AUTH_TENANT_SUBDOMAIN $AuthTenantSubdomain | Out-Null
    azd env set AUSPEX_AUTH_CLIENT_ID $AuthClientId | Out-Null
    # The app registration lives in the external tenant, so redirect-URI
    # management is opt-in: it needs `az login --tenant <external-tenant-id>`.
    Set-AzdDefault AUSPEX_MANAGE_ENTRA_REDIRECT_URI "false"
    Write-Host "External tenant configured. Confirm in that tenant:"
    Write-Host "  - a sign-up/sign-in user flow exists and this app is added to it"
    Write-Host "  - Email one-time passcode and/or Google federation is enabled for Gmail sign-ups"
    Write-Host "  - the SPA redirect URI includes http://localhost:5173 and the deployed API URL"
}
else {
    azd env set AUSPEX_OWNER_OBJECT_ID $ownerObjectId | Out-Null
    if (-not $AuthTenantId) { $AuthTenantId = $tenantId }
    azd env set AUSPEX_AUTH_TENANT_ID $AuthTenantId | Out-Null
}

$clientId = Get-AzdValue "AUSPEX_AUTH_CLIENT_ID"
if (-not $clientId -and $AuthTenantType -eq "workforce") {
    $appName = "auspex-$EnvironmentName"
    $clientId = az ad app create `
        --display-name $appName `
        --sign-in-audience AzureADMyOrg `
        --enable-id-token-issuance true `
        --query appId `
        --output tsv
    if (-not $clientId) { throw "Could not create or resolve the Entra application." }
    $objectId = az ad app show --id $clientId --query id --output tsv
    if (-not $objectId) { throw "Could not resolve the Entra application object." }
    $body = @{
        spa = @{
            redirectUris = @("http://localhost:5173")
        }
    } | ConvertTo-Json -Depth 4 -Compress
    az rest --method PATCH `
        --uri "https://graph.microsoft.com/v1.0/applications/$objectId" `
        --headers "Content-Type=application/json" `
        --body $body `
        --output none
    azd env set AUSPEX_AUTH_CLIENT_ID $clientId | Out-Null
}

$contactEmail = Read-Required "Monitored email for alerts and SEC identification"
$InitialAdminEmail = Read-Required "Verified email for the first Auspex administrator" $InitialAdminEmail
$priceApiKey = Read-Secret "Alpha Vantage API key"
$newsApiKey = Read-Secret "Finnhub API key"

azd env set AUSPEX_ALERT_EMAIL $contactEmail | Out-Null
azd env set AUSPEX_EDGAR_USER_AGENT "Auspex/1.0 ($contactEmail)" | Out-Null
azd env set AUSPEX_PRICE_API_KEY $priceApiKey | Out-Null
azd env set AUSPEX_NEWS_API_KEY $newsApiKey | Out-Null
azd env set AUSPEX_INITIAL_ADMIN_EMAIL $InitialAdminEmail | Out-Null

Set-AzdDefault AUSPEX_EXISTING_KEY_VAULT_NAME ""
Set-AzdDefault AUSPEX_EXISTING_KEY_VAULT_RESOURCE_GROUP ""
Set-AzdDefault AUSPEX_EXISTING_LEDGER_ACCOUNT_NAME ""
Set-AzdDefault AUSPEX_EXISTING_LEDGER_RESOURCE_GROUP ""
Set-AzdDefault AUSPEX_LEDGER_DATABASE_NAME "auspex"
Set-AzdDefault AUSPEX_OWNER_LEDGER_PARTITION_KEY ""
Set-AzdDefault AUSPEX_OWNER_LEGACY_OBJECT_ID ""
Set-AzdDefault AUSPEX_AUTH_LEGACY_ISSUER ""
Set-AzdDefault AUSPEX_AUTH_LEGACY_JWKS_URL ""
Set-AzdDefault AUSPEX_AUTH_LEGACY_AUDIENCE ""
Set-AzdDefault AUSPEX_PRESERVE_LEGACY_RESOURCE_NAMES "false"
Set-AzdDefault AUSPEX_PRIMARY_COSMOS_ACCOUNT_NAME ""
Set-AzdDefault AUSPEX_STORAGE_ACCOUNT_NAME ""
Set-AzdDefault AUSPEX_REGISTRY_NAME ""
Set-AzdDefault AUSPEX_OPENAI_ACCOUNT_NAME ""
Set-AzdDefault AUSPEX_MONTHLY_BUDGET "165"
Set-AzdDefault AUSPEX_EXTRACTION_MODEL_CAPACITY "200"
Set-AzdDefault AUSPEX_NARRATIVE_MODEL_CAPACITY "30"
Set-AzdDefault AUSPEX_MANAGE_ENTRA_REDIRECT_URI "true"

Write-Host "Auspex environment '$EnvironmentName' is configured."
Write-Host "Review with: azd env get-values"
Write-Host "Deploy with: azd up"
