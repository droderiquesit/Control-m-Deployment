// Key Vault -> Control-M secret sync infrastructure: EP1 Linux Elastic
// Premium plan, Functions storage account and the <name>-kvsync python 3.11
// Function App (Durable Functions, hub ctmkvsync). The app's SYSTEM-assigned
// identity gets Key Vault Secrets User - it only READS the tagged secrets it
// syncs (and the emuser password it logs in with); nothing here can write the
// vault. VNet integration into snet-ctm-func (delegated Microsoft.Web/
// serverFarms) with route-all, so calls to the EM Automation API ride the
// private network to ctm-web. Function CODE arrives separately via
// `ctmdeploy kvsync deploy --env <env>` (scm zip-deploy of functions/kvsync).

@description('Platform name prefix (e.g. ctm-prod); the app is named <name>-kvsync, the plan <name>-kvsync-plan.')
param name string

@description('Azure region for all resources.')
param location string

@description('Functions storage account name (3-24 lowercase alphanumerics, deterministic - computed by access.bicep).')
param storageAccountName string

@description('snet-ctm-func subnet id (delegated to Microsoft.Web/serverFarms) for VNet integration.')
param funcSubnetId string

@description('Platform Key Vault id; the Secrets User role is assigned at this scope (same resource group).')
param keyVaultId string

@description('Platform Key Vault URI (KEY_VAULT_URI app setting).')
param keyVaultUri string

@description('EM Automation API base URL, e.g. https://ctm-web.prod.ctm.internal:8443/automation-api (CTM_API_BASE app setting).')
param ctmApiBase string

@description('NCRONTAB sync schedule (SYNC_SCHEDULE app setting), e.g. 0 */15 * * * *.')
param syncSchedule string

@description('Tags applied to every resource.')
param tags object = {}

var planName = '${name}-kvsync-plan'
var functionAppName = '${name}-kvsync'

// Built-in data-plane role: Key Vault Secrets User (read secret values only).
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

// ---------------- Functions storage (host state + durable task hub) ----------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  tags: tags
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // The Functions host authenticates with the account key (AzureWebJobsStorage
    // + the EP content share below), so shared-key access stays on.
    allowSharedKeyAccess: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ---------------- EP1 Linux Elastic Premium plan ----------------
// Premium (not Consumption) is what makes VNet integration + always-warm
// workers possible; nonprod may set kv_sync.enabled false to save the cost.
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  kind: 'elastic'
  tags: tags
  sku: {
    name: 'EP1'
    tier: 'ElasticPremium'
    family: 'EP'
    size: 'EP1'
    capacity: 1
  }
  properties: {
    reserved: true // Linux
    maximumElasticWorkerCount: 3
  }
}

// Connection string referenced twice below: host storage + EP content share.
// It never leaves the deployment (resolved inside ARM at runtime).
var storageConnection = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${az.environment().suffixes.storage}'

// ---------------- Function App (python 3.11, system-assigned MI) ----------------
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    // VNet integration: all outbound (KV private endpoint, EM API) rides the
    // delegated func subnet; route-all keeps nothing on the public path.
    virtualNetworkSubnetId: funcSubnetId
    vnetRouteAllEnabled: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnection
        }
        {
          name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING'
          value: storageConnection
        }
        {
          name: 'WEBSITE_CONTENTSHARE'
          value: toLower(functionAppName)
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'KEY_VAULT_URI'
          value: keyVaultUri
        }
        {
          name: 'CTM_API_BASE'
          value: ctmApiBase
        }
        {
          name: 'SYNC_SCHEDULE'
          value: syncSchedule
        }
        {
          // Control-M/EM ships a self-signed web certificate by default; the
          // function flags every unverified call. Replace the cert and set
          // 'true' in production (see functions/kvsync/README.md).
          name: 'CTM_VERIFY_TLS'
          value: 'false'
        }
      ]
    }
  }
}

// ---------------- Key Vault Secrets User for the app's identity ----------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: last(split(keyVaultId, '/'))
}

// Deterministic guid(scope, principal-stand-in, role) name, the same
// idempotency pattern as modules/security.bicep: the app's RESOURCE id stands
// in for its runtime-only principalId, so re-deploys update in place.
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultId, functionApp.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output principalId string = functionApp.identity.principalId
