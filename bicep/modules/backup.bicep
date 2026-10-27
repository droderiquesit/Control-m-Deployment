// Recovery Services vault for the Control-M platform: daily V2 VM backup policy
// (23:00 UTC, instant restore 5 days, daily + Sunday-weekly retention), the
// protected EM/Server VMs, and an optional Azure Files share policy + protected
// share for the NFS account. Ported property-for-property from the old modules/backup.

@description('Platform name prefix (ctm-<env>); the vault is named <name>-rsv.')
param name string

@description('Azure region for the vault.')
param location string

@description('Vault redundancy: LocallyRedundant, ZoneRedundant or GeoRedundant (required for cross-region restore).')
param storageModeType string = 'GeoRedundant'

@description('Allow restoring VM backups into the paired region (GeoRedundant only).')
param crossRegionRestore bool = false

@description('Daily recovery point retention in days.')
param dailyRetentionDays int = 30

@description('Weekly (Sunday) recovery point retention in weeks.')
param weeklyRetentionWeeks int = 12

@description('Daily backup window start, UTC, HH:mm.')
param backupTimeUtc string = '23:00'

@description('Resource ids of the VMs to protect with the daily VM policy.')
param vmIds array = []

@description('Premium Files account whose share is backed up (empty string to skip file-share backup).')
param filesStorageAccountId string = ''

@description('Name of the file share to protect in the files account.')
param filesShareName string = ''

@description('Tags applied to the vault.')
param tags object = {}

// ARM wants a full timestamp for schedule/retention times; only the time-of-day is used.
var scheduleRunTime = '2021-01-01T${backupTimeUtc}:00Z'

// Placeholder id keeps split()/last() evaluable while the file-share resources are disabled.
var effectiveFilesAccountId = empty(filesStorageAccountId)
  ? '/subscriptions/none/resourceGroups/none/providers/Microsoft.Storage/storageAccounts/none'
  : filesStorageAccountId
var filesAccountResourceGroup = split(effectiveFilesAccountId, '/')[4]
var filesAccountName = last(split(effectiveFilesAccountId, '/'))

resource vault 'Microsoft.RecoveryServices/vaults@2024-04-01' = {
  name: '${name}-rsv'
  location: location
  tags: tags
  sku: {
    name: 'RS0'
    tier: 'Standard'
  }
  properties: {
    redundancySettings: {
      standardTierStorageRedundancy: storageModeType
      crossRegionRestore: (storageModeType == 'GeoRedundant' && crossRegionRestore) ? 'Enabled' : 'Disabled'
    }
  }
}

resource vmDailyPolicy 'Microsoft.RecoveryServices/vaults/backupPolicies@2024-04-01' = {
  parent: vault
  name: '${name}-vm-daily'
  location: location
  properties: {
    backupManagementType: 'AzureIaasVM'
    policyType: 'V2'
    timeZone: 'UTC'
    instantRpRetentionRangeInDays: 5
    schedulePolicy: {
      schedulePolicyType: 'SimpleSchedulePolicyV2'
      scheduleRunFrequency: 'Daily'
      dailySchedule: {
        scheduleRunTimes: [
          scheduleRunTime
        ]
      }
    }
    retentionPolicy: {
      retentionPolicyType: 'LongTermRetentionPolicy'
      dailySchedule: {
        retentionTimes: [
          scheduleRunTime
        ]
        retentionDuration: {
          count: dailyRetentionDays
          durationType: 'Days'
        }
      }
      weeklySchedule: {
        daysOfTheWeek: [
          'Sunday'
        ]
        retentionTimes: [
          scheduleRunTime
        ]
        retentionDuration: {
          count: weeklyRetentionWeeks
          durationType: 'Weeks'
        }
      }
    }
  }
}

// IaaS VM protection lives under the implicit Azure fabric; containers are
// addressed by the mangled name iaasvmcontainer;iaasvmcontainerv2;<rg>;<vm>.
// BCP081: vaults/backupFabrics is a proxy segment with no published Bicep types at
// any api-version; it is only referenced here to compose child resource names.
#disable-next-line BCP081
resource azureFabric 'Microsoft.RecoveryServices/vaults/backupFabrics@2024-04-01' existing = {
  parent: vault
  name: 'Azure'
}

resource vmContainers 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers@2024-04-01' existing = [
  for vmId in vmIds: {
    parent: azureFabric
    name: 'iaasvmcontainer;iaasvmcontainerv2;${split(vmId, '/')[4]};${last(split(vmId, '/'))}'
  }
]

resource protectedVms 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems@2024-04-01' = [
  for (vmId, i) in vmIds: {
    parent: vmContainers[i]
    name: 'vm;iaasvmcontainerv2;${split(vmId, '/')[4]};${last(split(vmId, '/'))}'
    properties: {
      protectedItemType: 'Microsoft.Compute/virtualMachines'
      policyId: vmDailyPolicy.id
      sourceResourceId: vmId
    }
  }
]

// ---------------- Azure Files share backup ----------------
// Registers the storage account with the vault, then protects the NFS share.
resource filesContainer 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers@2024-04-01' = if (!empty(filesStorageAccountId)) {
  parent: azureFabric
  name: 'storagecontainer;Storage;${filesAccountResourceGroup};${filesAccountName}'
  properties: {
    backupManagementType: 'AzureStorage'
    containerType: 'StorageContainer'
    sourceResourceId: effectiveFilesAccountId
    friendlyName: filesAccountName
  }
}

resource filesDailyPolicy 'Microsoft.RecoveryServices/vaults/backupPolicies@2024-04-01' = if (!empty(filesStorageAccountId)) {
  parent: vault
  name: '${name}-files-daily'
  location: location
  properties: {
    backupManagementType: 'AzureStorage'
    workLoadType: 'AzureFileShare'
    timeZone: 'UTC'
    schedulePolicy: {
      schedulePolicyType: 'SimpleSchedulePolicy'
      scheduleRunFrequency: 'Daily'
      scheduleRunTimes: [
        scheduleRunTime
      ]
    }
    retentionPolicy: {
      retentionPolicyType: 'LongTermRetentionPolicy'
      dailySchedule: {
        retentionTimes: [
          scheduleRunTime
        ]
        retentionDuration: {
          count: dailyRetentionDays
          durationType: 'Days'
        }
      }
    }
  }
}

resource protectedFileShare 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems@2024-04-01' = if (!empty(filesStorageAccountId)) {
  parent: filesContainer
  name: 'AzureFileShare;${filesShareName}'
  properties: {
    protectedItemType: 'AzureFileShareProtectedItem'
    policyId: filesDailyPolicy.id
    sourceResourceId: effectiveFilesAccountId
  }
}

output vaultId string = vault.id
output vaultName string = vault.name
