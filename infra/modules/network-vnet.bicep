// network-vnet.bicep — VNet and subnets for Auspex.
// Deployed BEFORE Function Apps so subnet IDs exist when VNet integration is configured.
// Private endpoints and DNS zones are in network.bicep (deployed last).
//
// VNet address space: 10.0.0.0/16
//   snet-func-ingest       10.0.1.0/24  — ingest Function App VNet integration
//   snet-func-wapi         10.0.2.0/24  — web API Function App VNet integration
//   snet-private-endpoints 10.0.3.0/24  — private endpoint NICs (no delegation)

@description('Environment name (dev or prod)')
param env string

@description('Azure region')
param location string

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'auspex-${env}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: ['10.0.0.0/16']
    }
    subnets: [
      {
        name: 'snet-func-ingest'
        properties: {
          addressPrefix: '10.0.1.0/24'
          delegations: [
            {
              name: 'delegation-app-environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: 'snet-func-wapi'
        properties: {
          addressPrefix: '10.0.2.0/24'
          delegations: [
            {
              name: 'delegation-app-environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: '10.0.3.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

var vnetId = vnet.id

@description('VNet resource ID')
output vnetId string = vnetId

@description('Subnet ID for ingest Function App VNet integration')
output ingestSubnetId string = '${vnetId}/subnets/snet-func-ingest'

@description('Subnet ID for web API Function App VNet integration')
output wapiSubnetId string = '${vnetId}/subnets/snet-func-wapi'
