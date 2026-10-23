// PROD + DR + BUILD PoC network plan — the SAME plan as
// environments/prod/config.yaml -> network_infrastructure (keep in sync:
// that block is what ctmdeploy deploys when network_infrastructure.create is
// true; this file is the cloud team's az-only equivalent).
//
// Deploy (AFTER the nonprod plan — the build VNet peers into
// vnet-dev-utility, declared below as role 'external'):
//   az deployment sub create -l eastus2 \
//     -f network/main.bicep -p network/params/prod.bicepparam
//
// One deployment covers BOTH regions (each entry carries its own resource
// group + location) plus the build VNet:
//   - vnet-prod-utility <-> vnet-dr-utility is a GLOBAL peering: the
//     Application Gateway (snet-ctm-appgw) reaches the DR internal LB and
//     Azure Bastion reaches the DR utility VNet.
//   - vnet-ctm-build peers to ALL THREE utility VNets so the Azure DevOps
//     scale-set build agents can deploy into nonprod, prod AND DR.
using '../main.bicep'

param vnets = [
  {
    name: 'vnet-prod-utility'
    resource_group_name: 'rg-network-prod'
    location: 'eastus2'
    role: 'utility'
    peer_to: ['vnet-dr-utility'] // utility <-> utility global pairing
    address_space: '10.70.0.0/22'
    subnets: [
      { name: 'snet-ctm-em', prefix: '10.70.0.0/24', role: 'em' }
      { name: 'snet-ctm-postgres', prefix: '10.70.1.0/27', role: 'data' }
      { name: 'snet-ctm-pe', prefix: '10.70.1.64/26', role: 'pe' }
      { name: 'AzureBastionSubnet', prefix: '10.70.2.0/26', role: 'bastion' }
      { name: 'snet-ctm-appgw', prefix: '10.70.2.64/26', role: 'appgw' }
      { name: 'snet-ctm-func', prefix: '10.70.2.128/26', role: 'func' }
    ]
  }
  {
    name: 'vnet-ctm-uat'
    resource_group_name: 'rg-network-prod'
    location: 'eastus2'
    role: 'spoke'
    peer_to: 'vnet-prod-utility'
    address_space: '10.70.8.0/24'
    subnets: [
      { name: 'snet-ctm-server', prefix: '10.70.8.0/26', role: 'server' }
      { name: 'snet-ctm-agents', prefix: '10.70.8.64/26', role: 'agents' }
    ]
  }
  {
    name: 'vnet-ctm-prod'
    resource_group_name: 'rg-network-prod'
    location: 'eastus2'
    role: 'spoke'
    peer_to: 'vnet-prod-utility'
    address_space: '10.70.9.0/24'
    subnets: [
      { name: 'snet-ctm-server', prefix: '10.70.9.0/26', role: 'server' }
      { name: 'snet-ctm-agents', prefix: '10.70.9.64/26', role: 'agents' }
    ]
  }
  {
    name: 'vnet-dr-utility'
    resource_group_name: 'rg-network-prod-dr'
    location: 'centralus'
    role: 'utility'
    address_space: '10.80.0.0/22'
    subnets: [
      { name: 'snet-ctm-em', prefix: '10.80.0.0/24', role: 'em' }
      { name: 'snet-ctm-postgres', prefix: '10.80.1.0/27', role: 'data' }
      { name: 'snet-ctm-pe', prefix: '10.80.1.64/26', role: 'pe' }
    ]
  }
  {
    name: 'vnet-ctm-prod-dr'
    resource_group_name: 'rg-network-prod-dr'
    location: 'centralus'
    role: 'spoke'
    peer_to: 'vnet-dr-utility'
    address_space: '10.80.9.0/24'
    subnets: [
      { name: 'snet-ctm-server', prefix: '10.80.9.0/26', role: 'server' }
      { name: 'snet-ctm-agents', prefix: '10.80.9.64/26', role: 'agents' }
    ]
  }
  {
    // Azure DevOps scale-set build agents (deploy into nonprod, prod AND DR).
    name: 'vnet-ctm-build'
    resource_group_name: 'rg-network-prod'
    location: 'eastus2'
    role: 'build'
    peer_to: ['vnet-dev-utility', 'vnet-prod-utility', 'vnet-dr-utility']
    address_space: '10.90.0.0/24'
    subnets: [
      { name: 'snet-ctm-build', prefix: '10.90.0.0/26', role: 'build' }
    ]
  }
  {
    // Deployed by the NONPROD plan (network/params/nonprod.bicepparam) —
    // declared here resolution-only so the build VNet peering + NSG sources
    // can name it. Nothing is created for this entry; the nonprod-side
    // peering (vnet-dev-utility -> vnet-ctm-build) IS emitted from this
    // deployment, into rg-network-nonprod.
    name: 'vnet-dev-utility'
    resource_group_name: 'rg-network-nonprod'
    location: 'eastus2'
    role: 'external'
    address_space: '10.60.0.0/22'
  }
]

// Operator networks allowed to reach Control-M Web / Automation API (443,
// 8443) on the em subnets, and the Application Gateway frontend. The appgw
// subnet prefix MUST stay here: the gateway's backend connections to the EM
// internal LBs of BOTH regions ride this rule. Append operator CIDRs as needed.
param userAccessCidrs = ['10.70.2.64/26']

// SSH 22 sources for every node/build subnet. The AzureBastionSubnet prefix
// MUST stay here — Azure Bastion sessions egress from it, and the agents
// subnets only allow SSH from these CIDRs (cross-region DR reach included).
// Append operator/jump CIDRs as needed.
param bastionCidrs = ['10.70.2.0/26']

param tags = {
  costCenter: 'scheduling'
  owner: 'platform-team'
  criticality: 'tier-1'
  environment: 'prod'
  platform: 'control-m'
  role: 'network'
  managedBy: 'bicep'
}
