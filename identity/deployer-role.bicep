// Custom subscription role 'Control-M Platform Deployer': everything the
// platform pipeline needs to run EVERY ctmdeploy phase (rg, foundation,
// secrets*, compute, access, dr, replication, image, media*, kvsync deploy)
// EXCEPT creating or changing the network itself. VNets, subnets, NSGs and
// peerings are deliberately NOT writable by this role - the network/ folder
// belongs to the cloud team and their own identity (Network Contributor on
// the rg-network-* groups; see docs/IDENTITY-PERMISSIONS.md).
//
//   az deployment sub create -l <region> -f identity/deployer-role.bicep
//   az role assignment create --assignee-object-id <pipeline SP object id> \
//     --assignee-principal-type ServicePrincipal \
//     --role "Control-M Platform Deployer" --scope /subscriptions/<id>
//
// (*) Management-plane only. The starred phases ALSO need data-plane roles
// that no management action can replace: Key Vault Secrets Officer on the
// platform vaults (secrets seeding - the foundation grants it to
// deployerObjectId automatically) and Storage Blob Data Contributor on the
// platform storage accounts (media + node-script uploads). Full per-phase
// matrix: docs/IDENTITY-PERMISSIONS.md.
//
// Built-in fallback: Contributor + User Access Administrator also works
// (docs/SETUP.md prerequisites) but grants network writes and far more; this
// custom role is the least-privilege replacement.
//
// Idempotent: the role definition name is a deterministic guid over the
// subscription + role name, so re-deploys update the same definition.

targetScope = 'subscription'

@description('Display name of the custom role. Changing it creates a NEW role definition (the guid name below derives from it).')
param roleName string = 'Control-M Platform Deployer'

@description('Role description shown in the portal / az role definition list.')
param roleDescription string = 'Deploys the Control-M platform (resource groups, compute, storage, Key Vault, PostgreSQL, Recovery Services, monitoring, Function App, load balancers, Application Gateway, Bastion, private DNS) but can never create or modify VNets, subnets, NSGs or peerings - the network is owned by the cloud team (network/ folder).'

// ---------------------------------------------------------------------------
// Pinned action list. Grouped by phase/provider; every entry is either a
// provider-scoped wildcard over resources the platform owns outright, or a
// narrow action on shared/network resources. There is intentionally NO
// Microsoft.Network/virtualNetworks/write, no .../subnets/write, no
// networkSecurityGroups and no virtualNetworkPeerings action in this list.
// ---------------------------------------------------------------------------
var actions = [
  // -- deployments + resource groups (every phase; rg phase CRUD) ----------
  'Microsoft.Resources/deployments/*' // validate/what-if/create at sub + RG scope
  'Microsoft.Resources/subscriptions/read'
  'Microsoft.Resources/subscriptions/resourceGroups/read'
  'Microsoft.Resources/subscriptions/resourceGroups/write' // ctm-<env>[-dr]-rg, ctm-images-rg, ctm-build-rg
  'Microsoft.Resources/subscriptions/resourceGroups/delete'
  'Microsoft.Resources/tags/*'

  // -- locks + role assignments (rg phase locks; foundation + kvsync grants)
  'Microsoft.Authorization/locks/*' // CanNotDelete locks when resource_lock: true
  'Microsoft.Authorization/roleDefinitions/read'
  'Microsoft.Authorization/roleAssignments/read'
  // Scoped USE (documented): the templates only assign roles at Key Vault /
  // storage-account / container scope with deterministic guid names
  // (Secrets User/Officer, Blob Data Reader/Contributor) - never at
  // subscription scope. ARM cannot express that restriction in an action
  // list, so the review gate is the what-if artifact.
  'Microsoft.Authorization/roleAssignments/write'
  'Microsoft.Authorization/roleAssignments/delete'

  // -- managed identities (foundation: ctm-<env>-nodes-id; access: ctm-build-id)
  'Microsoft.ManagedIdentity/userAssignedIdentities/*'

  // -- compute (compute/access phases, image builds) ------------------------
  'Microsoft.Compute/virtualMachines/*'
  'Microsoft.Compute/virtualMachineScaleSets/*' // agent pools + ctm-build-agents
  'Microsoft.Compute/disks/*'
  'Microsoft.Compute/images/*' // Packer intermediate managed image
  'Microsoft.Compute/galleries/*' // ctmgallery + ctm-rhel9 versions
  'Microsoft.Compute/locations/*/read' // async operation polling

  // -- storage, management plane only (foundation, dr object replication) ---
  // Data-plane blob writes (media/script uploads) need Storage Blob Data
  // Contributor separately; listKeys (kvsync Functions storage) is a
  // management action and IS covered by this wildcard.
  'Microsoft.Storage/storageAccounts/*'

  // -- Key Vault, management plane only (foundation) ------------------------
  // Secret reads/writes are data-plane: Key Vault Secrets Officer/User.
  'Microsoft.KeyVault/vaults/*'
  'Microsoft.KeyVault/checkNameAvailability/read'
  'Microsoft.KeyVault/locations/*/read'

  // -- PostgreSQL Flexible Server (foundation, dr replica) -------------------
  'Microsoft.DBforPostgreSQL/flexibleServers/*'
  'Microsoft.DBforPostgreSQL/locations/*'

  // -- Recovery Services: backup vaults + Site Recovery (compute, dr, replication)
  'Microsoft.RecoveryServices/*'

  // -- observability (foundation, compute) -----------------------------------
  'Microsoft.OperationalInsights/workspaces/*'
  'Microsoft.OperationsManagement/solutions/*' // VMInsights(law)
  'Microsoft.Insights/dataCollectionRules/*'
  'Microsoft.Insights/dataCollectionRuleAssociations/*'
  'Microsoft.Insights/actionGroups/*'
  'Microsoft.Insights/metricAlerts/*'
  'Microsoft.Insights/activityLogAlerts/*'
  'Microsoft.Insights/autoscalesettings/*' // agent-pool autoscale
  'Microsoft.Insights/components/*'

  // -- Web: kvsync Function App plan + site (access phase, kvsync deploy) ----
  // sites/* includes sites/publish/action, which the scm zipdeploy bearer
  // auth of `ctmdeploy kvsync deploy` requires.
  'Microsoft.Web/serverfarms/*'
  'Microsoft.Web/sites/*'

  // -- Network, LIMITED (the deliberate boundary) ----------------------------
  // Read everything (placement validation, what-if), write ONLY the
  // network children the platform owns, and JOIN - never write - the
  // cloud team's VNets/subnets. No virtualNetworks/write, no subnets/write,
  // no networkSecurityGroups, no virtualNetworkPeerings.
  'Microsoft.Network/*/read'
  'Microsoft.Network/loadBalancers/*' // regional internal LBs (+ backend joins)
  'Microsoft.Network/applicationGateways/*' // ctm-<env>-appgw (private L4 gateway)
  'Microsoft.Network/bastionHosts/*' // ctm-<env>-bastion
  'Microsoft.Network/publicIPAddresses/*' // bastion PIP (+ public LB mode)
  'Microsoft.Network/networkInterfaces/*' // node NICs
  'Microsoft.Network/privateEndpoints/*' // kv/blob/files PEs
  'Microsoft.Network/privateDnsZones/*' // platform + privatelink zones, links, A/CNAME records
  'Microsoft.Network/trafficmanagerprofiles/*' // optional dr.traffic_manager
  'Microsoft.Network/virtualNetworks/subnets/join/action' // place NICs/PEs/appgw/function integration into existing subnets
  'Microsoft.Network/virtualNetworks/join/action' // link private DNS zones to existing VNets
]

resource deployerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  // Deterministic: one definition per (subscription, roleName) that
  // re-deploys converge on instead of duplicating.
  name: guid(subscription().id, roleName)
  properties: {
    roleName: roleName
    description: roleDescription
    type: 'CustomRole'
    assignableScopes: [
      subscription().id
    ]
    permissions: [
      {
        actions: actions
        notActions: []
        // No data actions on purpose: the data-plane needs (Key Vault
        // secrets, blob uploads) are granted through the scoped built-in
        // roles listed in docs/IDENTITY-PERMISSIONS.md.
        dataActions: []
        notDataActions: []
      }
    ]
  }
}

@description('Full resource id of the custom role definition (use as --role in az role assignment create).')
output roleDefinitionId string = deployerRole.id

@description('The role definition GUID name.')
output roleDefinitionName string = deployerRole.name

@description('The display name to reference in role assignments.')
output roleDisplayName string = roleName
