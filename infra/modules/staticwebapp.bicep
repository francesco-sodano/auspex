// staticwebapp.bicep — Azure Static Web Apps (React SPA)
//
// REGION NOTE: Azure Static Web Apps has limited region availability.
// Switzerland North is NOT supported for Static Web Apps as of 2025.
// The closest supported region is West Europe (westeurope).
// Reference: https://azure.microsoft.com/en-us/explore/global-infrastructure/products-by-region/
//
// Data residency impact: The SPA hosts only static files (JS/CSS/HTML);
// no user data is stored in the SWA itself. All data remains in Switzerland North
// (Cosmos DB, AI Search, OpenAI, Key Vault, Function Apps).
//
// SKU: Standard — required for custom authentication (Entra External ID) and
// API linking. The Free tier does not support bring-your-own auth providers.

@description('Environment name (dev or prod)')
param env string

@description('Azure region for SWA — must be westeurope (Switzerland North not supported)')
param swaLocation string = 'westeurope'

@description('GitHub repository URL for the SWA deployment source')
param repositoryUrl string = 'https://github.com/francesco-sodano/auspex'

@description('GitHub branch to deploy from')
param branch string = 'main'

var swaName = 'auspex-${env}-swa'

resource staticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: swaName
  location: swaLocation
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    repositoryUrl: repositoryUrl
    branch: branch
    buildProperties: {
      appLocation: 'web'
      outputLocation: 'dist'
      apiLocation: '' // web API is a separate Function App (auspex-{env}-wapi)
    }
    // Entra External ID auth is configured via the SWA's staticwebapp.config.json
    // in the web/ directory, not here, to keep auth config in source control.
  }
}

@description('Static Web App resource name')
output swaName string = staticWebApp.name

@description('Static Web App default hostname')
output defaultHostname string = staticWebApp.properties.defaultHostname

@description('Static Web App resource ID')
output swaId string = staticWebApp.id

@description('Deployment token (used by GitHub Actions SWA CLI deploy)')
output deploymentToken string = staticWebApp.listSecrets().properties.apiKey
