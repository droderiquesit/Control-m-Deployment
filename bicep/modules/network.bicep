// Bring-your-own network: everything here is a lookup of pre-existing networking
// (see docs/NETWORK-REQUIREMENTS.md for what the subnets must provide). The only
// resources created are the privatelink DNS zones the platform needs and that were
// not supplied (hub-managed zones are preferred), each linked to the existing VNet.

@description('Name prefix for the VNet links of any private DNS zones created here.')
param namePrefix string

@description('Name of the EXISTING virtual network to deploy into.')
param vnetName string

@description('Resource group of the existing virtual network.')
param vnetResourceGroupName string

@description('Existing subnet for EM/Server VMs, the agent scale set and the load balancer frontend.')
param appSubnetName string

@description('Existing subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers.')
param dataSubnetName string

@description('Existing subnet for private endpoints (Key Vault, Blob, Files). Defaults to the app subnet.')
param peSubnetName string = ''

// Keys: blob (privatelink.blob...), file (privatelink.file...), vault
// (privatelink.vaultcore...), postgres (<x>.postgres.database.azure.com).
@description('Ids of private DNS zones that already exist (typically in a hub) and are linked to the VNet. Keys: blob, file, vault, postgres. Any key left out (or empty) is created in the platform resource group and linked to the VNet.')
param existingPrivateDnsZoneIds object = {}

@description('Prefix for a postgres private DNS zone created here (<prefix>.postgres.database.azure.com).')
param postgresDnsZonePrefix string

@description('Tags applied to every resource created by this module.')
param tags object = {}

// ---------------------------------------------------------------------------
// Existing network lookups (cross-RG via scope).
// ---------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
  scope: resourceGroup(vnetResourceGroupName)
}

var effectivePeSubnetName = empty(peSubnetName) ? appSubnetName : peSubnetName

// Subnet ids are composed with resourceId() against the VNet's resource group:
// 'existing' child resources cannot be addressed individually across RGs in outputs.
var appSubnetId = resourceId(subscription().subscriptionId, vnetResourceGroupName, 'Microsoft.Network/virtualNetworks/subnets', vnetName, appSubnetName)
var dataSubnetId = resourceId(subscription().subscriptionId, vnetResourceGroupName, 'Microsoft.Network/virtualNetworks/subnets', vnetName, dataSubnetName)
var peSubnetId = resourceId(subscription().subscriptionId, vnetResourceGroupName, 'Microsoft.Network/virtualNetworks/subnets', vnetName, effectivePeSubnetName)

// ---------------------------------------------------------------------------
// Private DNS zones: create only the ones not supplied, then link to the VNet.
// ---------------------------------------------------------------------------
var existingBlobZoneId = existingPrivateDnsZoneIds.?blob ?? ''
var existingFileZoneId = existingPrivateDnsZoneIds.?file ?? ''
var existingVaultZoneId = existingPrivateDnsZoneIds.?vault ?? ''
var existingPostgresZoneId = existingPrivateDnsZoneIds.?postgres ?? ''

var zoneCatalog = [
  {
    key: 'blob'
    // Privatelink zone names are fixed by Azure (public cloud); ported verbatim.
    #disable-next-line no-hardcoded-env-urls
    zoneName: 'privatelink.blob.core.windows.net'
    existingId: existingBlobZoneId
  }
  {
    key: 'file'
    // Privatelink zone names are fixed by Azure (public cloud); ported verbatim.
    #disable-next-line no-hardcoded-env-urls
    zoneName: 'privatelink.file.core.windows.net'
    existingId: existingFileZoneId
  }
  {
    key: 'vault'
    zoneName: 'privatelink.vaultcore.azure.net'
    existingId: existingVaultZoneId
  }
  {
    key: 'postgres'
    zoneName: '${postgresDnsZonePrefix}.postgres.database.azure.com'
    existingId: existingPostgresZoneId
  }
]

resource privateDnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [
  for entry in zoneCatalog: if (empty(entry.existingId)) {
    name: entry.zoneName
    location: 'global'
    tags: tags
  }
]

resource privateDnsZoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for (entry, i) in zoneCatalog: if (empty(entry.existingId)) {
    parent: privateDnsZones[i]
    name: '${namePrefix}-${entry.key}'
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: vnet.id
      }
    }
  }
]

// Supplied zones win; zones created here are addressed in this resource group.
var privatelinkZoneIdMap = {
  blob: empty(existingBlobZoneId) ? resourceId('Microsoft.Network/privateDnsZones', zoneCatalog[0].zoneName) : existingBlobZoneId
  file: empty(existingFileZoneId) ? resourceId('Microsoft.Network/privateDnsZones', zoneCatalog[1].zoneName) : existingFileZoneId
  vault: empty(existingVaultZoneId) ? resourceId('Microsoft.Network/privateDnsZones', zoneCatalog[2].zoneName) : existingVaultZoneId
  postgres: empty(existingPostgresZoneId) ? resourceId('Microsoft.Network/privateDnsZones', zoneCatalog[3].zoneName) : existingPostgresZoneId
}

output vnetId string = vnet.id
output appSubnetId string = appSubnetId
output dataSubnetId string = dataSubnetId
output peSubnetId string = peSubnetId

@description('Map blob/file/vault/postgres => private DNS zone id (existing or created).')
output privatelinkZoneIds object = privatelinkZoneIdMap

output postgresPrivateDnsZoneId string = privatelinkZoneIdMap.postgres
