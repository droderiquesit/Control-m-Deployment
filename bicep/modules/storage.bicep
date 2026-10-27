// Control-M storage module: OAuth-only blob account (installers + backups,
// lifecycle tiering, node RBAC) and a Premium FileStorage account exposing the
// NFS 4.1 share 'ctm-shared'. Both accounts are reached via private endpoints.

@description('Name prefix (e.g. ctm-prod).')
param name string

@description('Azure region for all resources.')
param location string

@description('Globally unique name for the installers/backups blob account.')
param blobAccountName string

@description('Globally unique name for the premium Azure Files (NFS) account.')
param filesAccountName string

@description('Replication for the blob account.')
@allowed([
  'LRS'
  'ZRS'
  'GRS'
  'RAGRS'
  'GZRS'
  'RAGZRS'
])
param blobReplicationType string

@description('Replication for premium files: LRS or ZRS (Premium ZRS needs a supported region).')
@allowed([
  'LRS'
  'ZRS'
])
param filesReplicationType string

@description('Size in GiB of the NFS share holding shared Control-M files (install media cache, EM/Server backups, logs).')
param sharedShareQuotaGb int

@description('Subnet id for the storage private endpoints.')
param peSubnetId string

@description('Private DNS zone id of the privatelink blob zone for the blob private endpoint.')
param blobPrivateDnsZoneId string

@description('Private DNS zone id of the privatelink file zone for the files private endpoint.')
param filePrivateDnsZoneId string

@description('Public CIDRs allowed through the blob storage firewall (upload installers, deployment data-plane calls).')
param allowedIpCidrs array = []

@description('Principal id of the node identity that downloads installers and writes backups.')
param nodeIdentityPrincipalId string

@description('Tags applied to every resource.')
param tags object = {}

// Azure storage firewalls reject host entries written as /32 CIDRs.
var ipRules = [
  for cidr in allowedIpCidrs: {
    value: replace(cidr, '/32', '')
    action: 'Allow'
  }
]

// Built-in role definitions.
var storageBlobDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
)
var storageBlobDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)

// ---------------- Blob account: installers + Control-M backups ----------------
resource blobAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: blobAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_${blobReplicationType}'
  }
  tags: tags
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: ipRules
    }
  }
}

// Versioning + change feed stay on so the account can be an object-replication
// source/destination (DR pairs the two regions' accounts).
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: blobAccount
  name: 'default'
  properties: {
    isVersioningEnabled: true
    changeFeed: {
      enabled: true
    }
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

resource installersContainerRes 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'installers'
  properties: {
    publicAccess: 'None'
  }
}

resource backupsContainerRes 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'backups'
  properties: {
    publicAccess: 'None'
  }
}

resource blobLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: blobAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'tier-and-expire-backups'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              prefixMatch: [
                'backups/'
              ]
              blobTypes: [
                'blockBlob'
              ]
            }
            actions: {
              baseBlob: {
                tierToCool: {
                  daysAfterModificationGreaterThan: 30
                }
                tierToArchive: {
                  daysAfterModificationGreaterThan: 90
                }
                delete: {
                  daysAfterModificationGreaterThan: 365
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: 60
                }
              }
            }
          }
        }
      ]
    }
  }
}

// Nodes download installers from the whole account.
resource nodesBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(blobAccount.id, nodeIdentityPrincipalId, storageBlobDataReaderRoleId)
  scope: blobAccount
  properties: {
    roleDefinitionId: storageBlobDataReaderRoleId
    principalId: nodeIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Nodes write EM/Server backups (util -U ...) to the backups container.
resource nodesBackupsWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(backupsContainerRes.id, nodeIdentityPrincipalId, storageBlobDataContributorRoleId)
  scope: backupsContainerRes
  properties: {
    roleDefinitionId: storageBlobDataContributorRoleId
    principalId: nodeIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${name}-blob-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-blob'
        properties: {
          privateLinkServiceId: blobAccount.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource blobPeZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'default'
        properties: {
          privateDnsZoneId: blobPrivateDnsZoneId
        }
      }
    ]
  }
}

// ---------------- Premium Files (NFS 4.1): shared Control-M filesystem ----------------
resource filesAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: filesAccountName
  location: location
  kind: 'FileStorage'
  sku: {
    name: 'Premium_${filesReplicationType}'
  }
  tags: tags
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: false // required for NFS
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: filesAccount
  name: 'default'
}

resource sharedShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'ctm-shared'
  properties: {
    shareQuota: sharedShareQuotaGb
    enabledProtocols: 'NFS'
  }
}

resource filesPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${name}-files-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-files'
        properties: {
          privateLinkServiceId: filesAccount.id
          groupIds: [
            'file'
          ]
        }
      }
    ]
  }
}

resource filesPeZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: filesPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'default'
        properties: {
          privateDnsZoneId: filePrivateDnsZoneId
        }
      }
    ]
  }
}

output blobAccountId string = blobAccount.id
output blobAccountName string = blobAccount.name
output filesAccountId string = filesAccount.id
output filesAccountName string = filesAccount.name
output installersContainer string = installersContainerRes.name
output backupsContainer string = backupsContainerRes.name
// environment().suffixes.storage is 'core.windows.net' in the public cloud.
output nfsServer string = '${filesAccount.name}.file.${environment().suffixes.storage}'
output nfsExportPath string = '/${filesAccount.name}/${sharedShare.name}'
output shareName string = sharedShare.name
