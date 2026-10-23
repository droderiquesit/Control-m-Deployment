// One direction of a spoke <-> utility VNet peering, named <src>-to-<dst>.
// bicep/network.bicep instantiates this module twice per spoke — once scoped
// to the spoke's resource group (<spoke>-to-<utility>) and once to the
// utility's (<utility>-to-<spoke>) — so every pair is bidirectional. Spoke
// VNets are never peered to each other: that is the isolation guarantee of
// the topology (docs/NETWORK-REQUIREMENTS.md).
//
// allowForwardedTraffic is on so a future NVA/firewall in the utility VNet
// keeps working; gateway transit stays off — the PoC plan has no gateways.

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
