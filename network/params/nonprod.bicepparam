// NONPROD PoC network plan — the SAME plan as
// environments/nonprod/config.yaml -> network_infrastructure (keep in sync:
// that block is what ctmdeploy deploys when network_infrastructure.create is
// true; this file is the cloud team's az-only equivalent).
//
// Deploy:
//   az deployment sub create -l eastus2 \
//     -f network/main.bicep -p network/params/nonprod.bicepparam
//
// Deploy this plan BEFORE the prod plan: the prod plan peers the build VNet
// into vnet-dev-utility (declared there as role 'external'), which must
// already exist.
using '../main.bicep'

param vnets = [
  {
    name: 'vnet-dev-utility'
    resource_group_name: 'rg-network-nonprod'
    location: 'eastus2'
    role: 'utility'
    address_space: '10.60.0.0/22'
    subnets: [
      { name: 'snet-ctm-em', prefix: '10.60.0.0/24', role: 'em' }
      { name: 'snet-ctm-postgres', prefix: '10.60.1.0/27', role: 'data' }
      { name: 'snet-ctm-pe', prefix: '10.60.1.64/26', role: 'pe' }
      { name: 'AzureBastionSubnet', prefix: '10.60.2.0/26', role: 'bastion' }
      { name: 'snet-ctm-func', prefix: '10.60.2.64/26', role: 'func' }
    ]
  }
  {
    name: 'vnet-ctm-dev'
    resource_group_name: 'rg-network-nonprod'
    location: 'eastus2'
    role: 'spoke'
    peer_to: 'vnet-dev-utility'
    address_space: '10.60.8.0/24'
    subnets: [
      { name: 'snet-ctm-server', prefix: '10.60.8.0/26', role: 'server' }
      { name: 'snet-ctm-agents', prefix: '10.60.8.64/26', role: 'agents' }
    ]
  }
  {
    name: 'vnet-ctm-qa'
    resource_group_name: 'rg-network-nonprod'
    location: 'eastus2'
    role: 'spoke'
    peer_to: 'vnet-dev-utility'
    address_space: '10.60.9.0/24'
    subnets: [
      { name: 'snet-ctm-server', prefix: '10.60.9.0/26', role: 'server' }
      { name: 'snet-ctm-agents', prefix: '10.60.9.64/26', role: 'agents' }
    ]
  }
]

// Operator networks allowed to reach Control-M Web / Automation API (443,
// 8443) directly on the em subnet. Empty: access goes via Azure Bastion.
param userAccessCidrs = []

// SSH 22 sources for every node subnet. The AzureBastionSubnet prefix MUST
// stay here — Azure Bastion sessions egress from it, and the agents subnets
// only allow SSH from these CIDRs. Append operator/jump CIDRs as needed.
param bastionCidrs = ['10.60.2.0/26']

param tags = {
  costCenter: 'scheduling'
  owner: 'platform-team'
  environment: 'nonprod'
  platform: 'control-m'
  role: 'network'
  managedBy: 'bicep'
}
