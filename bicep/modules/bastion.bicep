// Azure Bastion (Standard SKU) in the utility VNet's AzureBastionSubnet:
// browser + native-client SSH into the Control-M nodes and the build agents
// (DR nodes included, over the utility<->utility global peering) without any
// VM ever carrying a public IP. Azure requires a Standard STATIC public IP for
// the Bastion control plane; operator sessions still terminate on private VM
// addresses only.

@description('Platform name prefix (e.g. ctm-prod); the host is named <name>-bastion.')
param name string

@description('Azure region for the Bastion host and its public IP.')
param location string

@description('Id of the AzureBastionSubnet (the literal name is an Azure requirement; /26 or larger, no NSG - Azure Bastion manages its own data plane).')
param subnetId string

@description('Tags applied to every resource.')
param tags object = {}

// Required by Azure Bastion: Standard SKU + Static allocation. This is the
// Bastion service's own control-plane endpoint, not an entry point to any VM.
resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${name}-bastion-pip'
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2023-11-01' = {
  name: '${name}-bastion'
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    // Standard features: native client (az network bastion ssh/tunnel) for
    // operators and file copy over the tunnel; both stay inside the VNet.
    enableTunneling: true
    enableIpConnect: true
    disableCopyPaste: false
    ipConfigurations: [
      {
        name: 'bastion-ipconfig'
        properties: {
          subnet: {
            id: subnetId
          }
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

output bastionId string = bastion.id
output bastionName string = bastion.name
output publicIpId string = publicIp.id
