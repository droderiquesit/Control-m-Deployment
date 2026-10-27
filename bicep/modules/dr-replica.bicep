// Cross-region PostgreSQL Flexible Server read replica for DR. Promoted to a
// standalone server by `ctmdeploy failover`; deployments are incremental, so
// the orchestrator skips this phase once the replica has been promoted.
@description('Replica server name, e.g. ctm-prod-dr-pg-p01.')
param name string

@description('DR region.')
param location string

@description('GP_/B_/MO_-prefixed SKU, e.g. GP_Standard_D8ds_v5; keep >= primary so it can take production load after promotion.')
param skuName string

@description('Resource id of the primary PostgreSQL Flexible Server.')
param sourceServerResourceId string

@description('Delegated data subnet id in the DR region.')
param delegatedSubnetId string

@description('Postgres private DNS zone id in the DR region.')
param privateDnsZoneId string

@description('Availability zone for the replica.')
param zone string = '1'

@description('Tags applied to the server.')
param tags object = {}

// Same GP_/B_/MO_ prefix mapping as modules/database.bicep.
var skuTierByPrefix = {
  B: 'Burstable'
  GP: 'GeneralPurpose'
  MO: 'MemoryOptimized'
}
var skuParts = split(skuName, '_')
var skuTier = skuTierByPrefix[skuParts[0]]
var skuSizeName = join(skip(skuParts, 1), '_')

// 2024-08-01 is the current stable GA api-version; Bicep CLI 0.30.23 only bundles
// DBforPostgreSQL types through 2023-12-01-preview, hence the suppression (same as
// modules/database.bicep). Drop it once the pipeline pins a newer bicep CLI.
#disable-next-line BCP081
resource replica 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuSizeName
    tier: skuTier
  }
  properties: {
    createMode: 'Replica'
    sourceServerResourceId: sourceServerResourceId
    availabilityZone: zone
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
  }
}

output serverId string = replica.id
output serverName string = replica.name
output fqdn string = replica.properties.fullyQualifiedDomainName
