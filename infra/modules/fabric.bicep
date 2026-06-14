// fabric.bicep — Microsoft Fabric Capacity (F2, pausable)
//
// REGION NOTE: Microsoft Fabric Capacity supports Switzerland North.
// Verify at: https://learn.microsoft.com/en-us/fabric/admin/region-availability
//
// IMPORTANT — Fabric Workspace cannot be provisioned via Bicep today.
// The Fabric Workspace (Lakehouse, Warehouse, Notebooks, Pipelines) must be
// created manually in the Microsoft Fabric portal and linked to this capacity.
// Steps:
//   1. Deploy this Bicep to create the capacity.
//   2. In the Fabric portal, create a new Workspace and assign it to this capacity.
//   3. Enable Fabric Git integration for the Workspace pointing at the
//      fabric/ directory in this repository.
//   4. Sync the Workspace to deploy notebooks, pipelines, and warehouse DDL.
//
// The capacity starts in Active state. The capacity scheduler (CapacityScheduler
// Timer Function in auspex-{env}-func) handles resume/suspend on the daily build.

@description('Environment name (dev or prod)')
param env string

@description('Azure region (switzerlandnorth)')
param location string

@description('UPN of the Fabric capacity administrator (e.g. user@example.com)')
param fabricAdminUpn string

@description('Principal ID of the ingestion Function App managed identity — granted Contributor on the capacity for resume/suspend')
param ingestFuncPrincipalId string

var capacityName = 'auspex${env}fab' // Fabric capacity names: 3-63 chars, lowercase alphanumeric only

// ARM Contributor role — required for the capacity scheduler to call resume/suspend
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource fabricCapacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: capacityName
  location: location
  sku: {
    name: 'F2'
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: [fabricAdminUpn]
    }
  }
}

// RBAC: ingestion Function App MI — Contributor on the Fabric capacity
// Needed so the CapacityScheduler Function can call:
//   POST .../resume and POST .../suspend via ARM REST
resource ingestFuncFabricRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(fabricCapacity.id, ingestFuncPrincipalId, contributorRoleId)
  scope: fabricCapacity
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: ingestFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Fabric capacity resource name')
output capacityName string = fabricCapacity.name

@description('Fabric capacity resource ID')
output capacityId string = fabricCapacity.id
