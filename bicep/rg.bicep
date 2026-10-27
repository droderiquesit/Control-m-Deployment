// Subscription-scope entry point: resource group(s) for one Control-M
// environment — the primary RG, plus the DR RG when drLocation is set —
// with optional CanNotDelete locks.
// Deployed by: python -m ctmdeploy deploy --env <env> --phase rg
targetScope = 'subscription'

@description('Platform name, e.g. ctm-prod (ctm-<environment>).')
param name string

@description('Primary region.')
param location string

@description('DR region; empty string = no DR resource group.')
param drLocation string = ''

@description('Apply CanNotDelete locks on the resource groups.')
param resourceLock bool

@description('Tags applied to every resource.')
param tags object = {}

// Same tag merge the platform composition applied to its resource group.
var environmentName = replace(name, 'ctm-', '')
var baseTags = union(tags, {
  environment: environmentName
  platform: 'control-m'
  managedBy: 'bicep'
})

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: '${name}-rg'
  location: location
  tags: union(baseTags, { role: 'primary' })
}

resource drRg 'Microsoft.Resources/resourceGroups@2024-03-01' = if (drLocation != '') {
  name: '${name}-dr-rg'
  location: drLocation
  tags: union(baseTags, { role: 'dr' })
}

module rgLock 'modules/rg-lock.bicep' = if (resourceLock) {
  name: '${name}-rg-lock'
  scope: rg
  params: {
    name: '${name}-no-delete'
    notes: 'Control-M ${environmentName} platform (primary).'
  }
}

module drRgLock 'modules/rg-lock.bicep' = if (resourceLock && drLocation != '') {
  name: '${name}-dr-rg-lock'
  scope: drRg
  params: {
    name: '${name}-dr-no-delete'
    notes: 'Control-M ${environmentName} platform (dr).'
  }
}

output rgName string = rg.name
output drRgName string = drLocation != '' ? '${name}-dr-rg' : ''
