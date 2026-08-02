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
// SKU: Standard — required for custom authentication and bring-your-own API linking.

@description('Environment name (dev or prod)')
param env string

@description('Azure region for SWA — must be westeurope (Switzerland North not supported)')
param swaLocation string = 'westeurope'

@description('GitHub repository URL for the SWA deployment source')
param repositoryUrl string

@description('GitHub branch to deploy from')
param branch string = 'main'

@description('Microsoft app registration client ID; audience is personal Microsoft accounts only')
param microsoftAuthClientId string

@secure()
@description('Microsoft app registration client secret; stored encrypted by Static Web Apps')
param microsoftAuthClientSecret string

@description('Resource ID of the linked Web API Function App')
param webApiResourceId string

@description('Resource name of the linked Web API Function App')
param webApiName string

@description('Azure region of the linked Web API Function App')
param webApiLocation string

var swaName = 'auspex-${env}-swa'

resource staticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: swaName
  location: swaLocation
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    repositoryUrl: repositoryUrl
    branch: branch
    buildProperties: {
      appLocation: 'web'
      outputLocation: 'dist'
      apiLocation: '' // web API is a separate Function App (auspex-{env}-wapi)
    }
    // Provider and route policy lives in web/staticwebapp.config.json.
  }
}

resource appSettings 'Microsoft.Web/staticSites/config@2024-11-01' = {
  parent: staticWebApp
  name: 'appsettings'
  properties: {
    AZURE_CLIENT_ID: microsoftAuthClientId
    // The shared Key Vault is private-only and SWA has no VNet data-plane path.
    // Keep this secure ARM parameter in SWA's encrypted application settings.
    AZURE_CLIENT_SECRET_APP_SETTING_NAME: microsoftAuthClientSecret
  }
}

resource productionBuild 'Microsoft.Web/staticSites/builds@2024-11-01' existing = {
  parent: staticWebApp
  name: 'default'
}

resource webApiBackend 'Microsoft.Web/staticSites/builds/linkedBackends@2024-11-01' = {
  parent: productionBuild
  name: webApiName
  properties: {
    backendResourceId: webApiResourceId
    region: webApiLocation
  }
}

@description('Static Web App resource name')
output swaName string = staticWebApp.name

@description('Static Web App default hostname')
output defaultHostname string = staticWebApp.properties.defaultHostname

@description('Static Web App resource ID')
output swaId string = staticWebApp.id

@description('System-assigned managed identity principal ID')
output principalId string = staticWebApp.identity.principalId
