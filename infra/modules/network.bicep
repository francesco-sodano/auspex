// network.bicep — Private DNS zones and private endpoints for Auspex.
// Deployed LAST (after all resources) so their resource IDs are available as inputs.
// The VNet and subnets live in network-vnet.bicep (deployed before Function Apps).
//
// Private DNS zones (linked to VNet):
//   privatelink.documents.azure.com   — Cosmos DB
//   privatelink.blob.core.windows.net — Storage (both function app accounts)
//   privatelink.vaultcore.azure.net   — Key Vault
//
// Private endpoints (all in snet-private-endpoints):
//   Cosmos DB → subresource Sql
//   Key Vault → subresource vault
//   Storage (ingest func) → subresource blob
//   Storage (wapi func)   → subresource blob

@description('Environment name (dev or prod)')
param env string

@description('Azure region')
param location string

@description('VNet resource ID (from network-vnet module output)')
param vnetId string

@description('Resource ID of the Cosmos DB account')
param cosmosAccountId string

@description('Resource ID of the Key Vault')
param kvId string

@description('Resource ID of the ingest Function App storage account')
param storageFuncId string

@description('Resource ID of the web API Function App storage account')
param storageWapiId string

var peSubnetId = '${vnetId}/subnets/snet-private-endpoints'

// ---------------------------------------------------------------------------
// Private DNS zones
// ---------------------------------------------------------------------------

resource dnsZoneCosmos 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.documents.azure.com'
  location: 'global'
}

resource dnsZoneBlob 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.blob.core.windows.net'
  location: 'global'
}

resource dnsZoneKv 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
}

// ---------------------------------------------------------------------------
// VNet links — connect each DNS zone to the VNet so name resolution works
// ---------------------------------------------------------------------------

resource dnsLinkCosmos 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZoneCosmos
  name: 'link-${env}-cosmos'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnetId }
    registrationEnabled: false
  }
}

resource dnsLinkBlob 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZoneBlob
  name: 'link-${env}-blob'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnetId }
    registrationEnabled: false
  }
}

resource dnsLinkKv 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZoneKv
  name: 'link-${env}-kv'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnetId }
    registrationEnabled: false
  }
}

// ---------------------------------------------------------------------------
// Private endpoint — Cosmos DB (subresource: Sql)
// ---------------------------------------------------------------------------

resource peCosmos 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'auspex-${env}-pe-cosmos'
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'conn-cosmos'
        properties: {
          privateLinkServiceId: cosmosAccountId
          groupIds: ['Sql']
        }
      }
    ]
  }
}

resource peCosmosZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: peCosmos
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-documents-azure-com'
        properties: {
          privateDnsZoneId: dnsZoneCosmos.id
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Private endpoint — Key Vault (subresource: vault)
// ---------------------------------------------------------------------------

resource peKv 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'auspex-${env}-pe-kv'
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'conn-kv'
        properties: {
          privateLinkServiceId: kvId
          groupIds: ['vault']
        }
      }
    ]
  }
}

resource peKvZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: peKv
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-vaultcore-azure-net'
        properties: {
          privateDnsZoneId: dnsZoneKv.id
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Private endpoint — ingest Function App storage (subresource: blob)
// ---------------------------------------------------------------------------

resource peStorageFunc 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'auspex-${env}-pe-stfunc'
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'conn-stfunc'
        properties: {
          privateLinkServiceId: storageFuncId
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource peStorageFuncZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: peStorageFunc
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-blob-core-windows-net'
        properties: {
          privateDnsZoneId: dnsZoneBlob.id
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Private endpoint — web API Function App storage (subresource: blob)
// ---------------------------------------------------------------------------

resource peStorageWapi 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'auspex-${env}-pe-stwapi'
  location: location
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'conn-stwapi'
        properties: {
          privateLinkServiceId: storageWapiId
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource peStorageWapiZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: peStorageWapi
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-blob-core-windows-net'
        properties: {
          privateDnsZoneId: dnsZoneBlob.id
        }
      }
    ]
  }
}

