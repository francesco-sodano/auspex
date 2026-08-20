#!/usr/bin/env sh
set -eu

ENVIRONMENT_NAME="${1:-dev}"
LOCATION="${2:-switzerlandnorth}"
OPENAI_LOCATION="${3:-swedencentral}"
PYPI_INDEX_URL="${4:-https://pypi.org/simple}"
# "workforce" uses the signed-in Azure tenant (organisational accounts only).
# "external" targets a Microsoft Entra External ID tenant, which is what allows
# sign-up with personal Gmail/Outlook addresses through a sign-up/sign-in user
# flow. An external tenant is a separate directory: it is NOT created here, and
# its app registration must already exist (see README "External tenant setup").
AUTH_TENANT_TYPE="${5:-workforce}"
AUTH_TENANT_ID="${6:-}"
AUTH_TENANT_SUBDOMAIN="${7:-}"
AUTH_CLIENT_ID_INPUT="${8:-}"
INITIAL_ADMIN_EMAIL="${9:-}"

get_azd_value() {
  azd env get-value "$1" 2>/dev/null || true
}

set_default() {
  if [ -z "$(get_azd_value "$1")" ]; then
    azd env set "$1" "$2" >/dev/null
  fi
}

az account show --output none

if azd env list --output json | grep -q "\"$ENVIRONMENT_NAME\""; then
  azd env select "$ENVIRONMENT_NAME" >/dev/null
else
  azd env new "$ENVIRONMENT_NAME" --no-prompt >/dev/null
fi

SUBSCRIPTION_ID="$(az account show --query id --output tsv)"
TENANT_ID="$(az account show --query tenantId --output tsv)"
OWNER_OBJECT_ID="$(az ad signed-in-user show --query id --output tsv)"

azd env set AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION_ID" >/dev/null
azd env set AZURE_LOCATION "$LOCATION" >/dev/null
azd env set AZURE_OPENAI_LOCATION "$OPENAI_LOCATION" >/dev/null
azd env set AUSPEX_PYPI_INDEX_URL "$PYPI_INDEX_URL" >/dev/null
azd env set AUSPEX_AUTH_TENANT_TYPE "$AUTH_TENANT_TYPE" >/dev/null

if [ "$AUTH_TENANT_TYPE" = "external" ]; then
  azd env set AUSPEX_OWNER_OBJECT_ID "" >/dev/null
  # The external tenant is a separate directory that Bicep cannot provision:
  # tenant creation is a directory operation, not an ARM resource. Its details
  # are supplied here and only ever parameterised downstream.
  if [ -z "$AUTH_TENANT_ID" ]; then
    printf "External tenant ID (GUID): "
    read -r AUTH_TENANT_ID
  fi
  if [ -z "$AUTH_TENANT_SUBDOMAIN" ]; then
    printf "External tenant subdomain (the 'contoso' in contoso.ciamlogin.com): "
    read -r AUTH_TENANT_SUBDOMAIN
  fi
  if [ -z "$AUTH_CLIENT_ID_INPUT" ]; then
    printf "Application (client) ID registered in the external tenant: "
    read -r AUTH_CLIENT_ID_INPUT
  fi
  azd env set AUSPEX_AUTH_TENANT_ID "$AUTH_TENANT_ID" >/dev/null
  azd env set AUSPEX_AUTH_TENANT_SUBDOMAIN "$AUTH_TENANT_SUBDOMAIN" >/dev/null
  azd env set AUSPEX_AUTH_CLIENT_ID "$AUTH_CLIENT_ID_INPUT" >/dev/null
  # The app registration lives in the external tenant, so redirect-URI
  # management is opt-in: it needs `az login --tenant <external-tenant-id>`.
  set_default AUSPEX_MANAGE_ENTRA_REDIRECT_URI "false"
  echo "External tenant configured. Confirm in that tenant:"
  echo "  - a sign-up/sign-in user flow exists and this app is added to it"
  echo "  - Email one-time passcode and/or Google federation is enabled for Gmail sign-ups"
  echo "  - the SPA redirect URI includes http://localhost:5173 and the deployed API URL"
else
  azd env set AUSPEX_OWNER_OBJECT_ID "$OWNER_OBJECT_ID" >/dev/null
  azd env set AUSPEX_AUTH_TENANT_ID "${AUTH_TENANT_ID:-$TENANT_ID}" >/dev/null
fi

CLIENT_ID="$(get_azd_value AUSPEX_AUTH_CLIENT_ID)"
if [ -z "$CLIENT_ID" ] && [ "$AUTH_TENANT_TYPE" = "workforce" ]; then
  APP_NAME="auspex-$ENVIRONMENT_NAME"
  CLIENT_ID="$(az ad app create \
    --display-name "$APP_NAME" \
    --sign-in-audience AzureADMyOrg \
    --enable-id-token-issuance true \
    --query appId \
    --output tsv)"
  OBJECT_ID="$(az ad app show --id "$CLIENT_ID" --query id --output tsv)"
  az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
    --headers "Content-Type=application/json" \
    --body '{"spa":{"redirectUris":["http://localhost:5173"]}}' \
    --output none
  azd env set AUSPEX_AUTH_CLIENT_ID "$CLIENT_ID" >/dev/null
fi

printf "Monitored email for alerts and SEC identification: "
read -r CONTACT_EMAIL
if [ -z "$INITIAL_ADMIN_EMAIL" ]; then
  printf "Verified email for the first Auspex administrator: "
  read -r INITIAL_ADMIN_EMAIL
fi
printf "Alpha Vantage API key: "
stty -echo
read -r PRICE_API_KEY
stty echo
printf "\nFinnhub API key: "
stty -echo
read -r NEWS_API_KEY
stty echo
printf "\n"

azd env set AUSPEX_ALERT_EMAIL "$CONTACT_EMAIL" >/dev/null
azd env set AUSPEX_EDGAR_USER_AGENT "Auspex/1.0 ($CONTACT_EMAIL)" >/dev/null
azd env set AUSPEX_PRICE_API_KEY "$PRICE_API_KEY" >/dev/null
azd env set AUSPEX_NEWS_API_KEY "$NEWS_API_KEY" >/dev/null
azd env set AUSPEX_INITIAL_ADMIN_EMAIL "$INITIAL_ADMIN_EMAIL" >/dev/null

set_default AUSPEX_EXISTING_KEY_VAULT_NAME ""
set_default AUSPEX_EXISTING_KEY_VAULT_RESOURCE_GROUP ""
set_default AUSPEX_EXISTING_LEDGER_ACCOUNT_NAME ""
set_default AUSPEX_EXISTING_LEDGER_RESOURCE_GROUP ""
set_default AUSPEX_LEDGER_DATABASE_NAME "auspex"
set_default AUSPEX_OWNER_LEDGER_PARTITION_KEY ""
set_default AUSPEX_OWNER_LEGACY_OBJECT_ID ""
set_default AUSPEX_AUTH_LEGACY_ISSUER ""
set_default AUSPEX_AUTH_LEGACY_JWKS_URL ""
set_default AUSPEX_AUTH_LEGACY_AUDIENCE ""
set_default AUSPEX_PRESERVE_LEGACY_RESOURCE_NAMES "false"
set_default AUSPEX_PRIMARY_COSMOS_ACCOUNT_NAME ""
set_default AUSPEX_STORAGE_ACCOUNT_NAME ""
set_default AUSPEX_REGISTRY_NAME ""
set_default AUSPEX_OPENAI_ACCOUNT_NAME ""
set_default AUSPEX_MONTHLY_BUDGET "165"
set_default AUSPEX_EXTRACTION_MODEL_CAPACITY "200"
set_default AUSPEX_NARRATIVE_MODEL_CAPACITY "30"
set_default AUSPEX_MANAGE_ENTRA_REDIRECT_URI "true"

echo "Auspex environment '$ENVIRONMENT_NAME' is configured."
echo "Review with: azd env get-values"
echo "Deploy with: azd up"
