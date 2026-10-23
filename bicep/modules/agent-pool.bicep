// Control-M agent pool: uniform Linux VMSS booted from the Compute Gallery
// golden image (optionally Spot), with terminate scheduled events so agents can
// deregister, an embedded Azure Monitor Agent, CPU-based autoscale and an
// optional DCR association. Ported property-for-property from the old modules/agent-pool.

@description('Scale set name (e.g. ctm-prod-agents-prod).')
param name string

@description('Azure region for all resources.')
param location string

@description('Subnet the instances are placed in.')
param subnetId string

@description('VM size for the agent instances.')
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

@description('Zones the scale set balances across.')
param zones array

@description('Autoscale floor.')
param minInstances int

@description('Autoscale ceiling.')
param maxInstances int

@description('Capacity when no autoscale rule applies (also the initial size).')
param defaultInstances int

@description('Average CPU percent over 5 minutes that adds an instance.')
param scaleOutCpuPercent int

@description('Average CPU percent over 15 minutes that removes an instance.')
param scaleInCpuPercent int

@description('Run agents as Spot VMs (dev/qa cost saver). Spot VMs may be evicted; agents deregister gracefully.')
param useSpot bool = false

@description('User-assigned identity attached to the instances (Key Vault + blob access, AMA auth).')
param identityId string

@description('Azure Monitor DCR to associate. Empty string to skip.')
param dataCollectionRuleId string = ''

@description('Tags applied to every resource.')
param tags object = {}

// OS disk sizing is fixed for agents: the golden image carries the runtime and
// agents keep no persistent state.
var osDiskGb = 64

resource vmss 'Microsoft.Compute/virtualMachineScaleSets@2024-07-01' = {
  name: name
  location: location
  zones: zones
  tags: tags
  sku: {
    name: vmSize
    tier: 'Standard'
    capacity: defaultInstances
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    orchestrationMode: 'Uniform'
    upgradePolicy: {
      mode: 'Manual'
    }
    overprovision: false
    singlePlacementGroup: false
    // zoneBalance may only be set when the scale set spans more than one zone.
    zoneBalance: length(zones) > 1 ? true : null
    scaleInPolicy: {
      rules: ['OldestVM']
      forceDeletion: false
    }
    virtualMachineProfile: {
      priority: useSpot ? 'Spot' : 'Regular'
      evictionPolicy: useSpot ? 'Delete' : null
      billingProfile: useSpot
        ? {
            maxPrice: -1
          }
        : null
      osProfile: {
        computerNamePrefix: 'ctm-agent-'
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
        }
      }
      storageProfile: {
        imageReference: {
          id: imageId
        }
        osDisk: {
          createOption: 'FromImage'
          caching: 'ReadWrite'
          diskSizeGB: osDiskGb
          managedDisk: {
            storageAccountType: 'Premium_LRS'
          }
        }
      }
      networkProfile: {
        networkInterfaceConfigurations: [
          {
            name: 'primary'
            properties: {
              primary: true
              enableAcceleratedNetworking: true
              ipConfigurations: [
                {
                  name: 'primary'
                  properties: {
                    primary: true
                    subnet: {
                      id: subnetId
                    }
                  }
                }
              ]
            }
          }
        ]
      }
      // Scheduled Events "Terminate" is delivered to the VM so the agent can
      // deregister from Control-M/Server before the instance is removed.
      scheduledEventsProfile: {
        terminateNotificationProfile: {
          enable: true
          notBeforeTimeout: 'PT10M'
        }
      }
      extensionProfile: {
        extensions: [
          {
            name: 'AzureMonitorLinuxAgent'
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
        ]
      }
      diagnosticsProfile: {
        bootDiagnostics: {
          enabled: true
        }
      }
    }
  }
}

resource dcrAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = if (dataCollectionRuleId != '') {
  name: '${name}-dcra'
  scope: vmss
  properties: {
    dataCollectionRuleId: dataCollectionRuleId
  }
}

// CPU-based autoscale: scale out quickly (5 min window), scale in slowly (15 min
// window) so short lulls between job waves do not shrink the pool.
resource autoscale 'Microsoft.Insights/autoscaleSettings@2022-10-01' = {
  name: '${name}-autoscale'
  location: location
  tags: tags
  properties: {
    enabled: true
    targetResourceUri: vmss.id
    profiles: [
      {
        name: 'cpu'
        capacity: {
          minimum: string(minInstances)
          maximum: string(maxInstances)
          default: string(defaultInstances)
        }
        rules: [
          {
            metricTrigger: {
              metricName: 'Percentage CPU'
              metricNamespace: 'microsoft.compute/virtualmachinescalesets'
              metricResourceUri: vmss.id
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT5M'
              timeAggregation: 'Average'
              operator: 'GreaterThan'
              threshold: scaleOutCpuPercent
            }
            scaleAction: {
              direction: 'Increase'
              type: 'ChangeCount'
              value: '1'
              cooldown: 'PT5M'
            }
          }
          {
            metricTrigger: {
              metricName: 'Percentage CPU'
              metricNamespace: 'microsoft.compute/virtualmachinescalesets'
              metricResourceUri: vmss.id
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT15M'
              timeAggregation: 'Average'
              operator: 'LessThan'
              threshold: scaleInCpuPercent
            }
            scaleAction: {
              direction: 'Decrease'
              type: 'ChangeCount'
              value: '1'
              cooldown: 'PT15M'
            }
          }
        ]
      }
    ]
  }
}

output vmssId string = vmss.id
output vmssName string = vmss.name
