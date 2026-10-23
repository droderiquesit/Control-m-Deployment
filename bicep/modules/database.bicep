// Control-M metadata database: PostgreSQL Flexible Server on a delegated
// subnet with private DNS, optional HA, Sunday 03:00 maintenance window, tuned
// server parameters and pre-created Control-M databases (UTF8 / en_US.utf8).

@description('PostgreSQL Flexible Server name (globally unique, lowercase).')
param name string

@description('Azure region.')
param location string

@description('Subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers that hosts the server.')
param delegatedSubnetId string

@description('Private DNS zone id (<prefix>.postgres.database.azure.com) the server registers in.')
param privateDnsZoneId string

@description('PostgreSQL major version supported by your Control-M release (BMC: check the Control-M compatibility matrix).')
param postgresVersion string = '16'

@description('Flexible Server SKU with tier prefix, e.g. B_Standard_B2ms (dev), GP_Standard_D4ds_v5, MO_Standard_E8ds_v5.')
param skuName string

@description('Provisioned storage in MB (converted to GB for ARM; use multiples of 1024).')
param storageMb int

@description('High availability mode.')
@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
param highAvailabilityMode string

@description('Automated backup retention in days.')
param backupRetentionDays int

@description('Replicate automated backups to the paired region (DR).')
param geoRedundantBackup bool

@description('Administrator login name.')
param administratorLogin string = 'ctmadmin'

@description('Administrator password (the orchestrator reads/seeds it in Key Vault and passes it here as a secure parameter).')
@secure()
param administratorPassword string

@description('Databases to pre-create (Control-M installers can also create their own).')
param databases array

@description('Tags applied to every resource.')
param tags object = {}

// Primary zone 1, ZoneRedundant standby in zone 2. Azure swaps zones after an
// HA failover; a later redeployment may report that swap as a change.
var availabilityZone = '1'
var standbyZone = '2'

// Sku tier mapping: 'GP_Standard_D8ds_v5' -> { name: 'Standard_D8ds_v5', tier: 'GeneralPurpose' }.
var skuTierByPrefix = {
  B: 'Burstable'
  GP: 'GeneralPurpose'
  MO: 'MemoryOptimized'
}
var skuParts = split(skuName, '_')
var skuTier = skuTierByPrefix[skuParts[0]]
var skuSizeName = join(skip(skuParts, 1), '_')

// Server parameters ported from the old module defaults.
var serverParameters = [
  {
    name: 'max_connections'
    value: '500'
  }
  {
    name: 'log_min_duration_statement'
    value: '5000'
  }
  {
    name: 'azure.extensions'
    value: 'PG_STAT_STATEMENTS'
  }
  {
    name: 'idle_in_transaction_session_timeout'
    value: '600000'
  }
]

// BCP081: this Bicep CLI bundles types only up to 2023-12-01-preview for this
// provider; 2024-08-01 is the current stable GA api-version, kept deliberately.
#disable-next-line BCP081
resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuSizeName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    availabilityZone: availabilityZone
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: storageMb / 1024
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    highAvailability: highAvailabilityMode == 'Disabled'
      ? {
          mode: 'Disabled'
        }
      : {
          mode: highAvailabilityMode
          standbyAvailabilityZone: highAvailabilityMode == 'ZoneRedundant' ? standbyZone : null
        }
    maintenanceWindow: {
      customWindow: 'Enabled'
      dayOfWeek: 0 // Sunday
      startHour: 3
      startMinute: 0
    }
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
  }
}

// Server parameters and databases are applied one at a time: PostgreSQL
// Flexible Server rejects concurrent management operations on the same server.
@batchSize(1)
// BCP081: 2024-08-01 is stable GA; local types lag behind (see server resource).
#disable-next-line BCP081
resource configurations 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = [
  for parameter in serverParameters: {
    parent: server
    name: parameter.name
    properties: {
      value: parameter.value
      source: 'user-override'
    }
  }
]

@batchSize(1)
// BCP081: 2024-08-01 is stable GA; local types lag behind (see server resource).
#disable-next-line BCP081
resource serverDatabases 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = [
  for db in databases: {
    parent: server
    name: db
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
    dependsOn: [
      configurations
    ]
  }
]

output serverId string = server.id
output serverName string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
