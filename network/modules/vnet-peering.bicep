// One direction of a VNet peering, named <src>-to-<dst>.
//
// network/main.bicep instantiates this module twice per DECLARED peering
// (every `peer_to` entry of a vnets item) — once scoped to the declaring
// VNet's resource group (<local>-to-<remote>) and once to the remote VNet's
// (<remote>-to-<local>) — so every pair is bidirectional. The pairs the plan
// declares are:
//   spoke   -> its utility VNet  (hub-and-spoke per platform region; spokes
//              are never peered to each other — the isolation guarantee of
//              the topology, docs/NETWORK-REQUIREMENTS.md)
//   utility -> a paired utility VNet (GLOBAL peering, e.g. prod <-> DR, so
//              the Application Gateway reaches the DR internal LB and Azure
//              Bastion reaches the DR utility VNet)
//   build   -> every utility VNet (the Azure DevOps build agents deploy into
//              nonprod, prod AND DR over these peerings)
//
// The remote VNet may live outside the deployment that calls this module
// (a `role: external` entry of the plan, e.g. the prod plan peering the
// build VNet into the NONPROD utility VNet): the remote id is built from
// name + resource group, never looked up, so the module only requires that
// the remote VNet already exists at deploy time.
//
// allowForwardedTraffic is on so a future NVA/firewall in a utility VNet
// keeps working; gateway transit stays off — the PoC plan has no gateways.
// Global (cross-region) peerings need no extra settings: the properties
// below are valid for both regional and global peering.

@description('VNet in this module\'s resource group that owns the peering.')
param localVnetName string

@description('VNet to peer with.')
param remoteVnetName string

@description('Resource group of the remote VNet (same subscription).')
param remoteVnetResourceGroupName string

resource localVnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: localVnetName
}

resource peering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-11-01' = {
  parent: localVnet
  name: '${localVnetName}-to-${remoteVnetName}'
  properties: {
    remoteVirtualNetwork: {
      id: resourceId(subscription().subscriptionId, remoteVnetResourceGroupName, 'Microsoft.Network/virtualNetworks', remoteVnetName)
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

output peeringId string = peering.id
