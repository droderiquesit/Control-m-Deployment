// Subscription-scope entry point: cross-region disaster recovery (prod only).
//   1. PostgreSQL cross-region read replica (promote on failover)
//   2. Site Recovery vault + replication cache account (fabrics/mappings/
//      replicated items are created by ctmdeploy --phase replication)
//   3. Blob object replication of installers/backups to the DR account
//   4. Optional Traffic Manager priority failover for a public web endpoint
// Deployed by: python -m ctmdeploy deploy --env prod --phase dr
targetScope = 'subscription'

@description('Platform name, e.g. ctm-prod.')
param name string

@description('Primary resource group name (ctm-<env>-rg).')
param primaryRg string

@description('DR resource group name (ctm-<env>-dr-rg).')
param drRg string

@description('Primary region.')
param primaryLocation string

@description('DR region.')
param drLocation string

@description('Replica server name, ctm-<env>-dr-pg-<suffix>; must match the host override the DR foundation was given.')
param replicaName string

@description('Replica compute SKU (GP_/B_/MO_-prefixed).')
param replicaSkuName string

@description('Resource id of the primary PostgreSQL Flexible Server.')
param primaryDatabaseServerId string

@description('Delegated data subnet id in the DR region (DR foundation output).')
param drDataSubnetId string

@description('Postgres private DNS zone id in the DR region (DR foundation output).')
param drPostgresPrivateDnsZoneId string

@description('Primary blob account name (object replication source).')
param primaryBlobAccountName string

@description('DR blob account name (object replication destination).')
param drBlobAccountName string

@description('Create the Site Recovery vault and replication cache account.')
param enableSiteRecovery bool

@description('Traffic Manager settings: {enabled, relativeName}.')
param trafficManager object

@description('Public IP id of the primary load balancer; empty when internal.')
param primaryLbPublicIpId string = ''

@description('Public IP id of the DR load balancer; empty when internal.')
param drLbPublicIpId string = ''

@description('Tags applied to every resource.')
param tags object = {}

var compact = replace(name, '-', '')
var primaryBlobAccountId = resourceId(primaryRg, 'Microsoft.Storage/storageAccounts', primaryBlobAccountName)
var drBlobAccountId = resourceId(drRg, 'Microsoft.Storage/storageAccounts', drBlobAccountName)

// Traffic Manager needs PUBLIC endpoints on both sides.
var trafficManagerEnabled = (trafficManager.?enabled ?? false) && primaryLbPublicIpId != '' && drLbPublicIpId != ''

// ---------------- 1. Database replica ----------------
module replica 'modules/dr-replica.bicep' = {
  name: '${name}-dr-replica'
  scope: resourceGroup(drRg)
  params: {
    name: replicaName
    location: drLocation
    skuName: replicaSkuName
    sourceServerResourceId: primaryDatabaseServerId
    delegatedSubnetId: drDataSubnetId
    privateDnsZoneId: drPostgresPrivateDnsZoneId
    zone: '1'
    tags: union(tags, { 'ctm:role': 'dr-replica' })
  }
}

// ---------------- 2. Site Recovery ----------------
module asrVault 'modules/asr-vault.bicep' = if (enableSiteRecovery) {
  name: '${name}-asr-vault'
  scope: resourceGroup(drRg)
  params: {
    name: '${name}-asr-rsv'
    location: drLocation
    tags: tags
  }
}

// Replication cache account must live in the source region.
module asrCache 'modules/asr-cache.bicep' = if (enableSiteRecovery) {
  name: '${name}-asr-cache'
  scope: resourceGroup(primaryRg)
  params: {
    name: take('${compact}asrcache${take(uniqueString(primaryRg), 6)}', 24)
    location: primaryLocation
    tags: union(tags, { 'ctm:role': 'asr-cache' })
  }
}

// ---------------- 3. Installer media replication ----------------
// Destination policy first (the service mints policy/rule ids), then the
// source policy echoing them.
module orDestination 'modules/object-replication.bicep' = {
  name: '${name}-or-destination'
  scope: resourceGroup(drRg)
  params: {
    accountName: drBlobAccountName
    policyId: 'default'
    sourceAccountId: primaryBlobAccountId
    destinationAccountId: drBlobAccountId
    rules: [
      { sourceContainer: 'installers', destinationContainer: 'installers' }
      { sourceContainer: 'backups', destinationContainer: 'backups' }
    ]
  }
}

module orSource 'modules/object-replication.bicep' = {
  name: '${name}-or-source'
  scope: resourceGroup(primaryRg)
  params: {
    accountName: primaryBlobAccountName
    policyId: orDestination.outputs.policyId
    sourceAccountId: primaryBlobAccountId
    destinationAccountId: drBlobAccountId
    rules: [
      {
        sourceContainer: 'installers'
        destinationContainer: 'installers'
        ruleId: orDestination.outputs.ruleIds[0]
      }
      {
        sourceContainer: 'backups'
        destinationContainer: 'backups'
        ruleId: orDestination.outputs.ruleIds[1]
      }
    ]
  }
}

// ---------------- 4. Traffic Manager (public endpoints only) ----------------
module tm 'modules/traffic-manager.bicep' = if (trafficManagerEnabled) {
  name: '${name}-traffic-manager'
  scope: resourceGroup(primaryRg)
  params: {
    name: '${name}-tm'
    relativeName: trafficManager.?relativeName ?? '${name}-web'
    primaryEndpointTargetId: primaryLbPublicIpId
    drEndpointTargetId: drLbPublicIpId
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Outputs consumed by the replication / failover phases.
// ---------------------------------------------------------------------------
output replicaId string = replica.outputs.serverId
output replicaFqdn string = replica.outputs.fqdn
output asrVaultId string = enableSiteRecovery ? asrVault!.outputs.vaultId : ''
output asrVaultName string = enableSiteRecovery ? asrVault!.outputs.vaultName : ''
output cacheStorageAccountId string = enableSiteRecovery ? asrCache!.outputs.accountId : ''
output trafficManagerFqdn string = trafficManagerEnabled ? tm!.outputs.fqdn : ''
