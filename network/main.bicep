// Subscription-scope entry point: OPTIONAL full-network provisioning
// ("Option A" in docs/NETWORK-REQUIREMENTS.md). Creates everything the
// placement blocks of environments/<env>/config.yaml reference — resource
// groups, VNets, subnets with one NSG per subnet (bastion subnets excepted),
// and the bidirectional peerings — from network_infrastructure.vnets.
//
// This folder is self-contained ON PURPOSE: peer address spaces are resolved
// HERE, by mapping/filtering over the raw `vnets` parameter, so the cloud
// team can own and deploy the network with the az CLI alone (no python):
//
//   az deployment sub create -l eastus2 \
//     -f network/main.bicep -p network/params/<env>.bicepparam
//
// See network/README.md for what it creates, deployment order, and how the
// application placement maps onto the subnets.
//
// Bring-your-own networking stays the default: ctmdeploy deploys this file
// only when network_infrastructure.create is true
// (python -m ctmdeploy deploy --env <env> --phase network).
//
// Idempotent by construction: deterministic resource and deployment names
// only (no newGuid/utcNow), so re-running converges instead of duplicating.
//
// VNet roles:
//   utility  — em/data/pe (+ optional bastion/appgw/func) subnets; the hub of
//              its platform region. May carry peer_to for utility <-> utility
//              GLOBAL peering (prod <-> DR: Application Gateway -> DR internal
//              LB, Azure Bastion -> DR utility VNet).
//   spoke    — one Control-M/Server + its agents; peer_to names its utility
//              VNet. Spokes are never peered to each other.
//   build    — Azure DevOps scale-set build agents; peer_to names EVERY
//              utility VNet so pipeline deployments reach nonprod, prod and DR.
//   external — resolution-only: declares the name, resource group and address
//              space of a VNet DEPLOYED BY ANOTHER PLAN (e.g. the prod plan
//              lists the nonprod utility VNet so the build VNet can peer into
//              it). No resource group, VNet or NSG is created for it, but
//              peer_to may reference it: both peering directions are still
//              emitted (the remote side scoped into the external VNet's
//              resource group — deploy that other plan FIRST).
//
// A peer_to name with NO matching vnets entry at all is skipped — no peering
// is emitted for it and its address space cannot feed any NSG rule — and is
// reported in the unresolvedPeerings output. Add a role 'external' entry to
// resolve it (ctmdeploy passes the config's vnets list verbatim, which only
// holds one environment, so the cross-environment build <-> nonprod-utility
// pair resolves via the params/prod.bicepparam external entry).

targetScope = 'subscription'

@description('network_infrastructure.vnets passed verbatim (raw config shape). Items: {name, resource_group_name, location, role (utility|spoke|build|external), address_space, subnets [{name, prefix, role}], peer_to (optional; a VNet name or a list of VNet names)}. Peer address spaces are resolved from this list — every peer_to entry must name another item (role external for VNets another plan deploys).')
param vnets array

@description('CIDRs allowed to reach Control-M Web / Automation API (TCP 443, 8443) on em and appgw subnets (network_infrastructure.user_access_cidrs). The shipped prod plan keeps the appgw subnet prefix here: the gateway\'s backend connections to the EM internal LBs of BOTH regions ride this rule.')
param userAccessCidrs array = []

@description('CIDRs allowed SSH (TCP 22) to node/build subnets (network_infrastructure.bastion_cidrs). Keep the AzureBastionSubnet prefixes here so Azure Bastion sessions pass the NSGs of the node subnets.')
param bastionCidrs array = []

@description('Tags applied to every resource.')
param tags object = {}

// ---------------------------------------------------------------------------
// Peer resolution — python-free, everything derives from the vnets param.
// ---------------------------------------------------------------------------

@description('peer_to normalized to a list of VNet names: absent/empty string -> [], a string -> [that string], a list stays as-is.')
func declaredPeers(vnet object) array => filter(array(vnet.?peer_to ?? []), peerName => !empty(peerName))

@description('Address spaces of the named VNets, resolved against the plan (external entries included).')
func addressSpacesOf(all array, names array) array =>
  map(filter(all, item => contains(names, item.name)), item => item.address_space)

@description('Names of the VNets whose peer_to names the given VNet (the reverse direction of every declared peering).')
func reversePeers(all array, vnetName string) array =>
  map(filter(all, item => contains(array(item.?peer_to ?? []), vnetName)), item => item.name)

// Entries this deployment creates; role 'external' entries only resolve names.
var managedVnets = filter(vnets, item => item.role != 'external')

// Address spaces of ALL spoke VNets of the platform, both regions —
// deliberately wider than any one peered set so DR failback keeps working
// (passed to utility VNets for their em/data/pe rules).
var allSpokeAddressSpaces = map(filter(vnets, item => item.role == 'spoke'), item => item.address_space)

// Every declared peering as a directed pair with both resource groups
// resolved. peer_to is declared on ONE side only (spoke -> utility,
// prod-utility -> dr-utility, build -> utilities); both directions are
// emitted below, so declaring A->B *and* B->A would duplicate deployments.
// Names with no matching vnets entry are skipped (and surfaced in the
// unresolvedPeerings output) — declare them as role 'external' to peer.
var declaredPeerings = flatten(map(managedVnets, local => map(
  filter(declaredPeers(local), peerName => !empty(filter(vnets, item => item.name == peerName))),
  peerName => {
    localName: local.name
    localResourceGroupName: local.resource_group_name
    remoteName: peerName
    remoteResourceGroupName: filter(vnets, item => item.name == peerName)[0].resource_group_name
  }
)))

var unresolvedPeerings = union(flatten(map(managedVnets, local =>
  filter(declaredPeers(local), peerName => empty(filter(vnets, item => item.name == peerName))))), [])

// ---------------------------------------------------------------------------
// Resource groups: names deduped across managed entries; each group lands in
// the region of the first VNet that names it (the config keeps one region per
// RG). External entries' resource groups are NOT created here.
// ---------------------------------------------------------------------------
var resourceGroupNames = union(map(managedVnets, item => item.resource_group_name), [])

resource networkResourceGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [
  for groupName in resourceGroupNames: {
    name: groupName
    location: filter(managedVnets, item => item.resource_group_name == groupName)[0].location
    tags: tags
  }
]

// ---------------------------------------------------------------------------
// VNets: one module per managed entry (VNet + per-subnet NSGs + delegations/
// policies). The peered address spaces (either direction, external entries
// included) feed the NSG rule sets inside the module.
// ---------------------------------------------------------------------------
module vnetDeployments 'modules/vnet.bicep' = [
  for item in managedVnets: {
    name: 'net-${item.name}'
    scope: resourceGroup(item.resource_group_name)
    dependsOn: [networkResourceGroups]
    params: {
      vnet: item
      peerAddressSpaces: addressSpacesOf(vnets, union(declaredPeers(item), reversePeers(vnets, item.name)))
      spokeAddressSpaces: item.role == 'utility' ? allSpokeAddressSpaces : []
      userAccessCidrs: userAccessCidrs
      bastionCidrs: bastionCidrs
      tags: tags
    }
  }
]

// ---------------------------------------------------------------------------
// Peering: every declared pair in BOTH directions (local->remote lives in the
// local VNet's RG, remote->local in the remote's). A remote declared as
// role 'external' must already exist (deploy the plan that owns it first);
// its reverse peering is emitted into ITS resource group from here.
// ---------------------------------------------------------------------------
module forwardPeerings 'modules/vnet-peering.bicep' = [
  for pair in declaredPeerings: {
    name: 'peer-${pair.localName}-to-${pair.remoteName}'
    scope: resourceGroup(pair.localResourceGroupName)
    dependsOn: [vnetDeployments]
    params: {
      localVnetName: pair.localName
      remoteVnetName: pair.remoteName
      remoteVnetResourceGroupName: pair.remoteResourceGroupName
    }
  }
]

module reversePeerings 'modules/vnet-peering.bicep' = [
  for pair in declaredPeerings: {
    name: 'peer-${pair.remoteName}-to-${pair.localName}'
    scope: resourceGroup(pair.remoteResourceGroupName)
    dependsOn: [vnetDeployments]
    params: {
      localVnetName: pair.remoteName
      remoteVnetName: pair.localName
      remoteVnetResourceGroupName: pair.localResourceGroupName
    }
  }
]

@description('Deduped resource group names created for the network (external entries excluded).')
output resourceGroupNames array = resourceGroupNames

@description('VNets created: {name, id}, in network_infrastructure.vnets order (external entries excluded).')
output vnetIds array = [
  for (item, index) in managedVnets: {
    name: item.name
    id: vnetDeployments[index].outputs.vnetId
  }
]

@description('Bidirectional peerings emitted, one entry per declared pair.')
output peerings array = map(declaredPeerings, pair => '${pair.localName} <-> ${pair.remoteName}')

@description('peer_to names with no matching vnets entry: NO peering was emitted for them. Add a role \'external\' entry (name, resource_group_name, address_space) to emit it — see network/README.md.')
output unresolvedPeerings array = unresolvedPeerings
