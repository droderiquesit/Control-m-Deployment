// Control-M security module: node managed identity, RBAC-mode Key Vault with
// network ACLs + private endpoint, and the data-plane role assignments.
// Secrets are seeded by the python orchestrator (ctmdeploy), NOT by Bicep.

@description('Name prefix (e.g. ctm-prod).')
param name string

@description('Azure region for all resources.')
param location string

@description('Globally unique Key Vault name (3-24 alphanumerics and hyphens).')
param keyVaultName string

@description('Enable Key Vault purge protection (recommended for prod; cannot be disabled once on).')
param purgeProtectionEnabled bool

@description('Allow the Key Vault data plane from public networks (restricted by network ACLs). Set false when deploying from inside the VNet only.')
param publicNetworkAccessEnabled bool

@description('CIDRs allowed through Key Vault network ACLs (deployment runners, admins).')
param allowedIpCidrs array = []

@description('Subnet id for the Key Vault private endpoint (empty string to skip).')
param peSubnetId string

@description('privatelink.vaultcore.azure.net zone id for the private endpoint.')
param vaultPrivateDnsZoneId string

@description('Object id of the deployer principal; when non-empty it is granted Key Vault Secrets Officer so the orchestrator can seed secrets.')
param deployerObjectId string = ''

@description('Tags applied to every resource.')
param tags object = {}

// Built-in data-plane role definitions (Key Vault RBAC mode).
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)
var keyVaultSecretsOfficerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
)

// ---------------- Managed identity used by every Control-M VM ----------------
resource nodesIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-nodes-id'
  location: location
  tags: tags
}

// ---------------- Key Vault ----------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    // ARM rejects an explicit false here; null omits the property instead.
    enablePurgeProtection: purgeProtectionEnabled ? true : null
    publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    accessPolicies: []
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: [
        for cidr in allowedIpCidrs: {
          value: cidr
        }
      ]
    }
  }
}

// Nodes read secrets at boot (DB credentials, EM admin password, SSH key).
resource nodesSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, nodesIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: nodesIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Deployer needs data-plane rights to write secrets (python seeding phase).
resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerObjectId)) {
  name: guid(keyVault.id, deployerObjectId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsOfficerRoleId
    principalId: deployerObjectId
  }
}

// ---------------- Private endpoint (optional) ----------------
resource vaultPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = if (!empty(peSubnetId)) {
  name: '${name}-kv-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-kv'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource vaultPeZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (!empty(peSubnetId)) {
  parent: vaultPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'default'
        properties: {
          privateDnsZoneId: vaultPrivateDnsZoneId
        }
      }
    ]
  }
}

output identityId string = nodesIdentity.id
output identityPrincipalId string = nodesIdentity.properties.principalId
output identityClientId string = nodesIdentity.properties.clientId
output keyVaultId string = keyVault.id
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
