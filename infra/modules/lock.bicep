// lock.bicep — CanNotDelete lock for a resource group.
// Must be a separate module because subscription-scope Bicep files cannot
// deploy resource-group-scoped resources inline (BCP139).

param rgName string

resource lock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: 'lock-${rgName}'
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of this resource group'
  }
}
