#!/usr/bin/env sh
set -eu

if [ -n "${AUSPEX_EXISTING_KEY_VAULT_NAME:-}" ]; then
  RBAC_ENABLED="$(az keyvault show \
    --name "$AUSPEX_EXISTING_KEY_VAULT_NAME" \
    --resource-group "$AUSPEX_EXISTING_KEY_VAULT_RESOURCE_GROUP" \
    --query properties.enableRbacAuthorization \
    --output tsv)"
  if [ "$RBAC_ENABLED" != "true" ]; then
    echo "Existing Key Vault must use Azure RBAC authorization." >&2
    exit 1
  fi
fi

if [ "${AUSPEX_MANAGE_ENTRA_REDIRECT_URI:-true}" = "false" ]; then
  echo "Skipping Entra redirect URI management."
  exit 0
fi

: "${AUSPEX_AUTH_CLIENT_ID:?AUSPEX_AUTH_CLIENT_ID is required}"
: "${SERVICE_API_URI:?SERVICE_API_URI is required}"

OBJECT_ID="$(az ad app show --id "$AUSPEX_AUTH_CLIENT_ID" --query id --output tsv)"
API_URI="${SERVICE_API_URI%/}"
CURRENT_REDIRECTS="$(az ad app show \
  --id "$AUSPEX_AUTH_CLIENT_ID" \
  --query spa.redirectUris \
  --output json)"
export CURRENT_REDIRECTS API_URI
BODY="$(python -c 'import json, os; values=json.loads(os.environ["CURRENT_REDIRECTS"]); values.extend(["http://localhost:5173", os.environ["API_URI"]]); print(json.dumps({"spa":{"redirectUris":sorted(set(values))}}))')"

az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
  --headers "Content-Type=application/json" \
  --body "$BODY" \
  --output none

echo "Configured Entra SPA redirect URI: $API_URI"
