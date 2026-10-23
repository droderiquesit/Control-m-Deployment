// One Control-M node VM (EM or Server): NIC with optional LB pool join, zonal
// VM booted from the Compute Gallery golden image, a separate persistent data
// disk for the Control-M home directory, the Azure Monitor Agent and an
// optional DCR association. Ported property-for-property from the old modules/node.

@description('VM name; also becomes the OS hostname Control-M is installed against (e.g. ctm-em-a).')
param name string

@description('Azure region for all resources.')
param location string

@description('Availability zone (1, 2 or 3).')
param zone string

@description('Subnet the NIC lives in.')
param subnetId string

@description('VM size (e.g. Standard_D8ds_v5).')
param vmSize string

@description('Compute Gallery image id (.../images/<def>/versions/<x.y.z|latest>), resolved by the python orchestrator.')
param imageId string

@description('Admin user; Control-M bootstrap expects ctmops.')
param adminUsername string = 'ctmops'

@description('SSH public key for the admin user (password auth is disabled).')
param sshPublicKey string

@description('cloud-init / shell bootstrap script (plain text; this module base64-encodes). Contains names/ids only - secrets are fetched from Key Vault at boot.')
@secure()
param customData string

@description('Persistent data disk size (GB) for the Control-M home/installation directory.')
param dataDiskGb int

@description('Premium_LRS or Premium_ZRS (ZRS disks survive a zone loss and can be re-attached in another zone).')
param diskType string

@description('OS disk size in GB.')
param osDiskGb int = 128

@description('User-assigned identity attached to the VM (Key Vault + blob access, AMA auth).')
param identityId string

@description('Load balancer backend pool to join (EM nodes). Empty string to skip.')
param lbBackendPoolId string = ''

@description('Azure Monitor DCR to associate. Empty string to skip.')
param dataCollectionRuleId string = ''

@description('Tags applied to every resource.')
param tags object = {}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${name}-nic'
  location: location
  tags: tags
  properties: {
    enableAcceleratedNetworking: true
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          subnet: {
            id: subnetId
          }
          privateIPAllocationMethod: 'Dynamic'
          // EM nodes join the LB pool; the pool association lives inline on the NIC.
          loadBalancerBackendAddressPools: lbBackendPoolId != ''
            ? [
                {
                  id: lbBackendPoolId
                }
              ]
            : null
        }
      }
    ]
  }
}

// Persistent data disk, attached at LUN 0. ZRS disks are regional (no zone);
// LRS disks are pinned to the VM's zone.
resource dataDisk 'Microsoft.Compute/disks@2024-03-02' = {
  name: '${name}-data'
  location: location
  zones: diskType == 'Premium_ZRS' ? null : [zone]
  sku: {
    name: diskType
  }
  tags: union(tags, { 'ctm:role': 'data' })
  properties: {
    creationData: {
      createOption: 'Empty'
    }
    diskSizeGB: dataDiskGb
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: name
  location: location
  zones: [zone]
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        id: imageId
      }
      osDisk: {
        name: '${name}-osdisk'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        diskSizeGB: osDiskGb
        managedDisk: {
          storageAccountType: diskType
        }
      }
      dataDisks: [
        {
          lun: 0
          name: dataDisk.name
          createOption: 'Attach'
          caching: 'None'
          managedDisk: {
            id: dataDisk.id
          }
        }
      ]
    }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      customData: base64(customData)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
        patchSettings: {
          patchMode: 'AutomaticByPlatform'
          assessmentMode: 'AutomaticByPlatform'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

// Azure Monitor Agent (syslog + perf -> Log Analytics through the DCR),
// authenticating with the node user-assigned identity.
resource ama 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'AzureMonitorLinuxAgent'
  location: location
  tags: tags
  properties: {
    publisher: 'Microsoft.Azure.Monitor'
    type: 'AzureMonitorLinuxAgent'
    typeHandlerVersion: '1.0'
    autoUpgradeMinorVersion: true
    enableAutomaticUpgrade: true
    settings: {
      authentication: {
        managedIdentity: {
          'identifier-name': 'mi_res_id'
          'identifier-value': identityId
        }
      }
    }
  }
}

resource dcrAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = if (dataCollectionRuleId != '') {
  name: '${name}-dcra'
  scope: vm
  properties: {
    dataCollectionRuleId: dataCollectionRuleId
  }
}

output vmId string = vm.id
output nicId string = nic.id

@description('OS disk resource id (needed by Site Recovery replication).')
output osDiskId string = vm.properties.storageProfile.osDisk.managedDisk.id

output dataDiskId string = dataDisk.id
output privateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
