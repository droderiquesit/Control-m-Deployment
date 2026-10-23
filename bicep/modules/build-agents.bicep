// Azure DevOps scale-set build agents: uniform VMSS ctm-build-agents on
// Ubuntu 22.04 Gen2 in the build VNet (peered to the nonprod, prod AND DR
// utility VNets, so one pool deploys into all three platforms). Capacity is
// owned by the ADO elastic pool - Manual upgrades, overprovision off and NO
// autoscale here. Bootstrap is tooling only (python + bicep, pinned and
// checksum-verified); the elastic pool installs the pipeline agent itself when
// it brings an instance online and WIF provides the deployment identity.
// AFTER deployment (documented in docs/AZURE-DEVOPS.md, not granted here):
// register the VMSS as an ADO scale-set agent pool; ADO's service principal
// needs Virtual Machine Contributor on this resource group to manage capacity.

@description('Scale set name; fixed so the ADO elastic pool registration is stable.')
param name string = 'ctm-build-agents'

@description('Azure region for all resources.')
param location string

@description('VM size for the build agents.')
param vmSize string

@description('Baseline instance count. Sent on every deploy as the pool floor; the ADO elastic pool scales from there.')
param instances int

@description('snet-ctm-build subnet id in the build VNet.')
param subnetId string

@description('Admin user; ssh-only (password auth disabled), reachable through Azure Bastion.')
param adminUsername string = 'ctmops'

@description('SSH public key for the admin user.')
param sshPublicKey string

@description('Pinned bicep CLI release installed on the agents (same pin as the CI pipelines).')
param bicepVersion string = '0.30.23'

@description('sha256 of the pinned bicep-linux-x64 release asset.')
param bicepSha256 string = 'b6a41ce8bdbf448feaf8fe2286164fb8c84e5d006c293fbc20591ff9baf76f18'

@description('Tags applied to every resource.')
param tags object = {}

var identityName = 'ctm-build-id'

// ---------------------------------------------------------------------------
// Inline cloud-init bash (plain bash is fine here - these are stock Ubuntu
// build boxes, not Control-M nodes; the python node installer does not apply).
// Idempotent: guarded by bootstrap.done. Tools only, NO secrets.
// Multiline bicep strings are raw (no ${} interpolation), so the pinned
// version/checksum are substituted via __TOKENS__ below.
// ---------------------------------------------------------------------------
var cloudInitTemplate = '''#!/bin/bash
# ctm-build-agents bootstrap: python3.11 + pip + unzip + curl + pinned bicep.
# Idempotent - safe to re-run; cloud-init runs it once per instance.
set -euo pipefail
exec > >(tee -a /var/log/ctm-build-bootstrap.log) 2>&1

MARKER=/var/lib/ctm-build/bootstrap.done
if [ -f "$MARKER" ]; then
  echo "bootstrap already complete"
  exit 0
fi
mkdir -p /var/lib/ctm-build

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3.11 python3.11-venv python3-pip unzip curl ca-certificates

# Pinned + checksum-verified bicep CLI (same pin as the CI pipelines).
curl -fsSL -o /tmp/bicep-cli \
  "https://github.com/Azure/bicep/releases/download/v__BICEP_VERSION__/bicep-linux-x64"
echo "__BICEP_SHA256__  /tmp/bicep-cli" | sha256sum -c -
install -m 0755 /tmp/bicep-cli /usr/local/bin/bicep
rm -f /tmp/bicep-cli

touch "$MARKER"
echo "ctm-build-agents bootstrap complete"
'''

var cloudInit = replace(
  replace(cloudInitTemplate, '__BICEP_VERSION__', bicepVersion),
  '__BICEP_SHA256__',
  bicepSha256
)

// NEW user-assigned identity, dedicated to the build agents (kept separate
// from the Control-M node identity on purpose: pipeline deployments
// authenticate via workload identity federation, and role grants for this
// identity are documented in docs/IDENTITY-PERMISSIONS.md, not made here).
resource buildIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource vmss 'Microsoft.Compute/virtualMachineScaleSets@2024-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: vmSize
    tier: 'Standard'
    capacity: instances
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${buildIdentity.id}': {}
    }
  }
  properties: {
    orchestrationMode: 'Uniform'
    // Manual + overprovision off: the ADO elastic pool creates/deletes exactly
    // the instances it wants; Azure must never add or reimage any on its own.
    upgradePolicy: {
      mode: 'Manual'
    }
    overprovision: false
    singlePlacementGroup: false
    virtualMachineProfile: {
      osProfile: {
        computerNamePrefix: 'ctmbuild'
        adminUsername: adminUsername
        customData: base64(cloudInit)
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
          publisher: 'Canonical'
          offer: '0001-com-ubuntu-server-jammy'
          sku: '22_04-lts-gen2'
          version: 'latest'
        }
        osDisk: {
          createOption: 'FromImage'
          caching: 'ReadWrite'
          diskSizeGB: 128
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
      diagnosticsProfile: {
        bootDiagnostics: {
          enabled: true
        }
      }
    }
  }
}

output vmssId string = vmss.id
output vmssName string = vmss.name
output identityId string = buildIdentity.id
output identityPrincipalId string = buildIdentity.properties.principalId
output identityClientId string = buildIdentity.properties.clientId
