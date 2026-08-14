param location string
param vnetName string
param privateEndpointSubnetId string
param cosmosAccountId string
param cosmosAccountName string
param storageAccountId string
param storageAccountName string
param openAiAccountId string
param openAiAccountName string
param keyVaultId string
param keyVaultName string
param sourceLedgerAccountId string
param sourceLedgerAccountName string

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' existing = {
  name: vnetName
}

var zoneNames = [
  'privatelink.documents.azure.com'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.openai.azure.com'
  'privatelink.vaultcore.azure.net'
]

var services = [
  {
    name: 'cosmos'
    resourceId: cosmosAccountId
    resourceName: cosmosAccountName
    groupId: 'Sql'
    zone: 'privatelink.documents.azure.com'
  }
  {
    name: 'blob'
    resourceId: storageAccountId
    resourceName: storageAccountName
    groupId: 'blob'
    zone: 'privatelink.blob.${environment().suffixes.storage}'
  }
  {
    name: 'openai'
    resourceId: openAiAccountId
    resourceName: openAiAccountName
    groupId: 'account'
    zone: 'privatelink.openai.azure.com'
  }
  {
    name: 'keyvault'
    resourceId: keyVaultId
    resourceName: keyVaultName
    groupId: 'vault'
    zone: 'privatelink.vaultcore.azure.net'
  }
  {
    name: 'source-ledger'
    resourceId: sourceLedgerAccountId
    resourceName: sourceLedgerAccountName
    groupId: 'Sql'
    zone: 'privatelink.documents.azure.com'
  }
]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [
  for zoneName in zoneNames: {
    name: zoneName
    location: 'global'
  }
]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [
  for (zoneName, index) in zoneNames: {
    parent: zones[index]
    name: 'link-auspex'
    location: 'global'
    properties: {
      virtualNetwork: {
        id: vnet.id
      }
      registrationEnabled: false
    }
  }
]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-01-01' = [
  for service in services: {
    name: 'pe-auspex-${service.name}'
    location: location
    properties: {
      subnet: {
        id: privateEndpointSubnetId
      }
      privateLinkServiceConnections: [
        {
          name: 'connection-${service.name}'
          properties: {
            privateLinkServiceId: service.resourceId
            groupIds: [
              service.groupId
            ]
          }
        }
      ]
    }
  }
]

resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = [
  for (service, index) in services: {
    parent: endpoints[index]
    name: 'default'
    properties: {
      privateDnsZoneConfigs: [
        {
          name: service.name
          properties: {
            privateDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', service.zone)
          }
        }
      ]
    }
    dependsOn: [
      zones
      links
    ]
  }
]
