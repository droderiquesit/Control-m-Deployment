// Subscription-scope entry point: OPTIONAL full-network provisioning
// ("Option A" in docs/NETWORK-REQUIREMENTS.md). Creates everything the
// placement blocks of environments/<env>/config.yaml reference — resource
// groups, VNets, subnets with one NSG per subnet, and the bidirectional
// spoke <-> utility peerings — from network_infrastructure.vnets.
//
// Bring-your-own networking stays the default: this file is deployed only
// when network_infrastructure.create is true.
// Deployed by: python -m ctmdeploy deploy --env <env> --phase network
//
// Idempotent by construction: deterministic resource and deployment names
// only (no newGuid/utcNow), so re-running converges instead of duplicating.
targetScope = 'subscription'

@description('network_infrastructure.vnets passed verbatim by the orchestrator, each item augmented with peerAddressSpaces (the address spaces of the VNets it peers with) and, on utility items, spokeAddressSpaces (the address spaces of ALL spoke VNets of the platform, both regions — deliberately wider than the peered set so DR failback keeps working). Items: {name, resource_group_name, location, role (utility|spoke), address_space, subnets [{name, prefix, role}], peer_to (spokes only)}.')
param vnets array

@description('CIDRs allowed to reach Control-M Web / Automation API (TCP 443, 8443) on em subnets (network_infrastructure.user_access_cidrs).')
param userAccessCidrs array = []

@description('CIDRs allowed SSH (TCP 22) to node subnets (network_infrastructure.bastion_cidrs).')
param bastionCidrs array = []

@description('Tags applied to every resource.')
param tags object = {}

// ---------------------------------------------------------------------------
// Resource groups: names deduped across entries; each group lands in the
// region of the first VNet that names it (the config keeps one region per RG).
// ---------------------------------------------------------------------------
var resourceGroupNames = union(map(vnets, item => item.resource_group_name), [])

resource networkResourceGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [
  for groupName in resourceGroupNames: {
    name: groupName
    location: filter(vnets, item => item.resource_group_name == groupName)[0].location
    tags: tags
  }
]

// ---------------------------------------------------------------------------
// VNets: one module per entry (VNet + per-subnet NSGs + delegations/policies).
// ---------------------------------------------------------------------------
module vnetDeployments 'modules/vnet.bicep' = [
  for item in vnets: {
    name: 'net-${item.name}'
    scope: resourceGroup(item.resource_group_name)
    dependsOn: [networkResourceGroups]
    params: {
      vnet: item
      userAccessCidrs: userAccessCidrs
      bastionCidrs: bastionCidrs
      tags: tags
    }
  }
]

// ---------------------------------------------------------------------------
// Peering, hub-and-spoke per platform region: each spoke peers with its
// utility VNet in BOTH directions (spoke->utility lives in the spoke's RG,
// utility->spoke in the utility's RG). Spokes are never peered to each other.
// ---------------------------------------------------------------------------
var spokes = filter(vnets, item => item.role == 'spoke')

module spokeToUtilityPeerings 'modules/vnet-peering.bicep' = [
  for spoke in spokes: {
    name: 'peer-${spoke.name}-to-${spoke.peer_to}'
    scope: resourceGroup(spoke.resource_group_name)
    dependsOn: [vnetDeployments]
    params: {
      localVnetName: spoke.name
      remoteVnetName: spoke.peer_to
      remoteVnetResourceGroupName: filter(vnets, item => item.name == spoke.peer_to)[0].resource_group_name
    }
  }
]

module utilityToSpokePeerings 'modules/vnet-peering.bicep' = [
  for spoke in spokes: {
    name: 'peer-${spoke.peer_to}-to-${spoke.name}'
    scope: resourceGroup(filter(vnets, item => item.name == spoke.peer_to)[0].resource_group_name)
    dependsOn: [vnetDeployments]
    params: {
      localVnetName: spoke.peer_to
      remoteVnetName: spoke.name
      remoteVnetResourceGroupName: spoke.resource_group_name
    }
  }
]

@description('Deduped resource group names created for the network.')
output resourceGroupNames array = resourceGroupNames

@description('VNets created: {name, id}, in network_infrastructure.vnets order.')
output vnetIds array = [
  for (item, index) in vnets: {
    name: item.name
    id: vnetDeployments[index].outputs.vnetId
  }
]
