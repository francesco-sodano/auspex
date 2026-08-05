targetScope = 'subscription'

@description('Environment name')
@allowed([
  'dev'
  'prod'
])
param env string

@description('Azure region for the Fabric capacity')
param location string = 'switzerlandnorth'

@description('UPN of the Fabric capacity administrator')
param fabricAdminUpn string

var dataResourceGroupName = 'auspex-${env}-data'
var capacityName = 'auspex${env}fab'
var tags = {
  environment: env
  workload: 'auspex'
}

module dataResourceGroup 'br/public:avm/res/resources/resource-group:0.4.3' = {
  name: 'bootstrapDataResourceGroup'
  params: {
    name: dataResourceGroupName
    location: location
    tags: tags
  }
}

module fabricCapacity 'br/public:avm/res/fabric/capacity:0.1.2' = {
  name: 'bootstrapFabricCapacity'
  scope: resourceGroup(dataResourceGroupName)
  params: {
    name: capacityName
    adminMembers: [
      fabricAdminUpn
    ]
    location: location
    skuName: 'F2'
    tags: tags
  }
  dependsOn: [
    dataResourceGroup
  ]
}

output capacityName string = fabricCapacity.outputs.name
output capacityResourceId string = fabricCapacity.outputs.resourceId
output dataResourceGroupName string = dataResourceGroup.outputs.name
