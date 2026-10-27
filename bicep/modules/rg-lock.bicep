// Applies a CanNotDelete management lock to the resource group this module is
// deployed into. Needed by rg.bicep because a subscription-scope template
// cannot place a lock directly on a resource group it creates.
targetScope = 'resourceGroup'

@description('Name of the management lock.')
param name string

@description('Free-text note explaining why the lock exists.')
param notes string = ''

resource lock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: name
  properties: {
    level: 'CanNotDelete'
    notes: notes
  }
}

output lockId string = lock.id
