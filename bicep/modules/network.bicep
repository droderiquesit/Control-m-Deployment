// Bring-your-own network, v2 placement: resolves the platform UTILITY VNet
// (EM subnet + delegated data subnet + private-endpoint subnet) and creates
// only the privatelink DNS zones that were not supplied (hub-managed zones are
// preferred). Every zone created here is linked to EVERY VNet of the platform
// region (utility + each isolated server/agent VNet) so all nodes resolve it.

@description('Utility VNet reference, config.yaml shape: vnet_name, vnet_resource_group_name, em_subnet_name, data_subnet_name, pe_subnet_name.')
param utility object

@description('Deduped list of EVERY VNet in this platform region (utility + each server VNet + agent overrides + extra_dns_vnets), computed by the orchestrator. Items: {name, resourceGroupName}. Each private DNS zone created here gets one link per entry.')
param allVnets array

// Keys: blob (privatelink.blob...), file (privatelink.file...), vault
// (privatelink.vaultcore...), postgres (<x>.postgres.database.azure.com).
@description('Ids of private DNS zones that already exist (typically in a hub) and are linked to the VNets. Keys: blob, file, vault, postgres. Any key left out (or empty) is created in the platform resource group and linked to every VNet in allVnets.')
param existingPrivateDnsZoneIds object = {}

@description('Prefix for a postgres private DNS zone created here (<prefix>.postgres.database.azure.com).')
param postgresDnsZonePrefix string

@description('Tags applied to every resource created by this module.')
param tags object = {}

// ---------------------------------------------------------------------------
// Existing utility network lookups (cross-RG via scope).
// ---------------------------------------------------------------------------
resource utilityVnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: utility.vnet_name
  scope: resourceGroup(utility.vnet_resource_group_name)
}

var peSubnetNameRaw = utility.?pe_subnet_name ?? ''
var effectivePeSubnetName = empty(peSubnetNameRaw) ? utility.em_subnet_name : peSubnetNameRaw

// Subnet ids are composed with resourceId() against the VNet's resource group:
// 'existing' child resources cannot be addressed individually across RGs in outputs.
var emSubnetId = resourceId(subscription().subscriptionId, utility.vnet_resource_group_name, 'Microsoft.Network/virtualNetworks/subnets', utility.vnet_name, utility.em_subnet_name)
var dataSubnetId = resourceId(subscription().subscriptionId, utility.vnet_resource_group_name, 'Microsoft.Network/virtualNetworks/subnets', utility.vnet_name, utility.data_subnet_name)
var peSubnetId = resourceId(subscription().subscriptionId, utility.vnet_resource_group_name, 'Microsoft.Network/virtualNetworks/subnets', utility.vnet_name, effectivePeSubnetName)

// ---------------------------------------------------------------------------
// Private DNS zones: create only the ones not supplied, then link each created
// zone to every VNet of the platform (link name = vnet name, registration off).
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

// Cross product zone x vnet, flattened because Bicep resource loops cannot nest.
var zoneLinkPlan = flatten(map(range(0, length(zoneCatalog)), zoneIndex => map(allVnets, vnet => {
  zoneIndex: zoneIndex
  vnetName: vnet.name
  vnetResourceGroupName: vnet.resourceGroupName
})))

resource privateDnsZoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for pair in zoneLinkPlan: if (empty(zoneCatalog[pair.zoneIndex].existingId)) {
    parent: privateDnsZones[pair.zoneIndex]
    name: pair.vnetName
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: resourceId(subscription().subscriptionId, pair.vnetResourceGroupName, 'Microsoft.Network/virtualNetworks', pair.vnetName)
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

output utilityVnetId string = utilityVnet.id
output emSubnetId string = emSubnetId
output dataSubnetId string = dataSubnetId
output peSubnetId string = peSubnetId

@description('Map blob/file/vault/postgres => private DNS zone id (existing or created).')
output privatelinkZoneIds object = privatelinkZoneIdMap

output postgresPrivateDnsZoneId string = privatelinkZoneIdMap.postgres

@description('Ids of the VNet links created on the postgres private DNS zone, one per allVnets entry ([] when the zone was supplied).')
output postgresDnsLinkIds array = empty(existingPostgresZoneId)
  ? map(allVnets, vnet => resourceId('Microsoft.Network/privateDnsZones/virtualNetworkLinks', zoneCatalog[3].zoneName, vnet.name))
  : []
