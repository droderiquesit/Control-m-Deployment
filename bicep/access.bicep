// Subscription-scope entry point: the ACCESS layer, deployed after compute
// (it needs the regional LB frontend IPs) and before dr.
//   1. Azure Bastion (Standard + required static PIP) in the utility VNet -
//      private SSH to every node and build agent, DR included via peering.
//   2. PRIVATE Application Gateway Standard_v2 (prod only - the orchestrator
//      gates it): layer-4 TCP 443/8443 across BOTH regional internal LBs,
//      plus the ctm-global A record in the platform private DNS zone.
//   3. Key Vault -> Control-M sync Durable Function infra (EP1 plan, storage,
//      python 3.11 app, system MI + Key Vault Secrets User, VNet-integrated).
//   4. Azure DevOps build-agent VMSS in its own resource group (created here).
// Deployed by: python -m ctmdeploy deploy --env <env> --phase access

targetScope = 'subscription'

@description('Platform name, e.g. ctm-prod (ctm-<environment>).')
param name string

@description('Primary region; every access-layer resource deploys here.')
param location string

@description('Primary platform resource group (ctm-<env>-rg): hosts Bastion, the gateway, the kvsync Function and the platform private DNS zone.')
param platformRgName string

@description('Deploy Azure Bastion into the utility VNet.')
param bastionEnabled bool

@description('AzureBastionSubnet id in the utility VNet; required when bastionEnabled.')
param bastionSubnetId string = ''

@description('Deploy the private Application Gateway. The orchestrator honors access.appgw.enabled on prod only (the gateway balances the primary AND DR LBs).')
param appgwEnabled bool

@description('snet-ctm-appgw subnet id in the utility VNet; required when appgwEnabled.')
param appgwSubnetId string = ''

@description('Gateway backend pool IPs: [primary internal LB frontend IP, DR internal LB frontend IP] (foundation outputs). Healthy-only routing = automatic primary/DR behavior.')
param appgwBackendIps array = []

@description('Platform private DNS zone name (e.g. prod.ctm.internal); the gateway adds the ctm-global A record to it.')
param dnsZoneName string

@description('Deploy the Key Vault -> Control-M sync Function infrastructure (code arrives via `ctmdeploy kvsync deploy`).')
param kvSyncEnabled bool

@description('snet-ctm-func subnet id (delegated to Microsoft.Web/serverFarms); required when kvSyncEnabled.')
param funcSubnetId string = ''

@description('Platform Key Vault id (foundation names are deterministic); the Function identity gets Secrets User on it.')
param keyVaultId string

@description('Platform Key Vault URI (KEY_VAULT_URI app setting).')
param keyVaultUri string

@description('EM Automation API base URL through the internal LB, e.g. https://ctm-web.prod.ctm.internal:8443/automation-api (CTM_API_BASE app setting).')
param ctmApiBase string

@description('NCRONTAB sync schedule (SYNC_SCHEDULE app setting), e.g. 0 */15 * * * *.')
param syncSchedule string

@description('Azure DevOps build agents: {enabled, resourceGroupName, vmSize, instances, subnetId, sshPublicKey}. Their resource group is created here; registering the VMSS as an ADO elastic pool is a documented post-step.')
param buildAgents object

@description('Tags applied to every resource (the orchestrator passes the merged platform tags).')
param tags object = {}

var compact = replace(name, '-', '')
var buildAgentsEnabled = buildAgents.enabled

// kvsync storage account: deterministic across re-deploys (uniqueString over
// the stable RG name - no newGuid/utcNow), same pattern as dr.bicep's
// asr-cache account. 24-char storage limit enforced with take().
var kvsyncStorageName = take('${compact}func${take(uniqueString(platformRgName), 6)}', 24)

// ---------------- 1. Azure Bastion ----------------
module bastion 'modules/bastion.bicep' = if (bastionEnabled) {
  name: '${name}-access-bastion'
  scope: resourceGroup(platformRgName)
  params: {
    name: name
    location: location
    subnetId: bastionSubnetId
    tags: tags
  }
}

// ---------------- 2. Private global gateway + ctm-global ----------------
module appgw 'modules/appgw.bicep' = if (appgwEnabled) {
  name: '${name}-access-appgw'
  scope: resourceGroup(platformRgName)
  params: {
    name: name
    location: location
    subnetId: appgwSubnetId
    backendIps: appgwBackendIps
    dnsZoneName: dnsZoneName
    tags: tags
  }
}

// ---------------- 3. Key Vault -> Control-M sync Function ----------------
module kvsync 'modules/function-kvsync.bicep' = if (kvSyncEnabled) {
  name: '${name}-access-kvsync'
  scope: resourceGroup(platformRgName)
  params: {
    name: name
    location: location
    storageAccountName: kvsyncStorageName
    funcSubnetId: funcSubnetId
    keyVaultId: keyVaultId
    keyVaultUri: keyVaultUri
    ctmApiBase: ctmApiBase
    syncSchedule: syncSchedule
    tags: tags
  }
}

// ---------------- 4. Build agents (own resource group, created here) ----------------
resource buildRg 'Microsoft.Resources/resourceGroups@2024-03-01' = if (buildAgentsEnabled) {
  name: buildAgents.resourceGroupName
  location: location
  tags: union(tags, { role: 'build-agents' })
}

module buildPool 'modules/build-agents.bicep' = if (buildAgentsEnabled) {
  name: '${name}-access-build-agents'
  scope: resourceGroup(buildAgents.resourceGroupName)
  params: {
    location: location
    vmSize: buildAgents.vmSize
    instances: buildAgents.instances
    subnetId: buildAgents.subnetId
    sshPublicKey: buildAgents.sshPublicKey
    tags: tags
  }
  dependsOn: [
    buildRg
  ]
}

// ---------------------------------------------------------------------------
// Outputs consumed by the orchestrator (and `ctmdeploy kvsync deploy`).
// ---------------------------------------------------------------------------
output bastionId string = bastionEnabled ? bastion!.outputs.bastionId : ''
output appgwPrivateIp string = appgwEnabled ? appgw!.outputs.privateIp : ''
output ctmGlobalFqdn string = appgwEnabled ? 'ctm-global.${dnsZoneName}' : ''
output kvsyncFunctionAppName string = kvSyncEnabled ? kvsync!.outputs.functionAppName : ''
output kvsyncPrincipalId string = kvSyncEnabled ? kvsync!.outputs.principalId : ''
output buildAgentsVmssId string = buildAgentsEnabled ? buildPool!.outputs.vmssId : ''
output buildAgentsIdentityPrincipalId string = buildAgentsEnabled
  ? buildPool!.outputs.identityPrincipalId
  : ''
