// Control-M EM web load balancer: Standard LB fronting the EM nodes with an
// HTTPS health probe that only the active EM answers. Internal by default
// (zone-redundant private frontend); public mode adds a zone-redundant static
// public IP. Ported property-for-property from the old modules/load-balancer.

@description('Name prefix (e.g. ctm-prod); the LB is named <name>-lb.')
param name string

@description('Azure region for the load balancer (and public IP when not internal).')
param location string

@description('true = internal (private frontend in the app subnet), false = public standard LB.')
param internal bool = true

@description('Subnet for the private frontend when internal = true.')
param subnetId string

@description('DNS label for the public IP when internal = false (used by Traffic Manager). Empty = no label.')
param publicDnsLabel string = ''

@description('Control-M Web / Automation API HTTPS port on EM nodes.')
param webPort int = 8443

@description('Tags applied to every resource.')
param tags object = {}

// HTTPS path probed on EM nodes. Only the active EM answers; the standby fails the probe.
var healthPath = '/ControlM/'

var lbName = '${name}-lb'
var frontendName = 'frontend'
var backendPoolName = 'em-web'
var probeName = 'em-web-https'

// Frontend ports forwarded to the EM web port (443 for browsers, webPort for the API/native clients).
var frontendPorts = [443, webPort]

// Zone-redundant static public IP, only for the public (non-internal) variant.
resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = if (!internal) {
  name: '${name}-lb-pip'
  location: location
  sku: {
    name: 'Standard'
  }
  zones: ['1', '2', '3']
  tags: tags
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: publicDnsLabel != ''
      ? {
          domainNameLabel: publicDnsLabel
        }
      : null
  }
}

resource lb 'Microsoft.Network/loadBalancers@2023-11-01' = {
  name: lbName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  tags: tags
  properties: {
    frontendIPConfigurations: [
      {
        name: frontendName
        zones: internal ? ['1', '2', '3'] : null
        properties: internal
          ? {
              subnet: {
                id: subnetId
              }
              privateIPAllocationMethod: 'Dynamic'
            }
          : {
              publicIPAddress: {
                id: publicIp.id
              }
            }
      }
    ]
    backendAddressPools: [
      {
        name: backendPoolName
      }
    ]
    probes: [
      {
        name: probeName
        properties: {
          protocol: 'Https'
          port: webPort
          requestPath: healthPath
          intervalInSeconds: 10
          numberOfProbes: 3
        }
      }
    ]
    loadBalancingRules: [
      for port in frontendPorts: {
        name: 'em-web-${port}'
        properties: {
          protocol: 'Tcp'
          frontendPort: port
          backendPort: webPort
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIPConfigurations', lbName, frontendName)
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', lbName, backendPoolName)
          }
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', lbName, probeName)
          }
          idleTimeoutInMinutes: 30
          enableTcpReset: true
          disableOutboundSnat: true
          loadDistribution: 'Default'
        }
      }
    ]
  }
}

output lbId string = lb.id

@description('Private frontend IP (internal) or public IP address.')
output frontendIp string = internal
  ? lb.properties.frontendIPConfigurations[0].properties.privateIPAddress
  : publicIp!.properties.ipAddress

output backendPoolId string = resourceId(
  'Microsoft.Network/loadBalancers/backendAddressPools',
  lbName,
  backendPoolName
)

@description('Public IP resource id; empty string when internal.')
output publicIpId string = internal ? '' : publicIp!.id
