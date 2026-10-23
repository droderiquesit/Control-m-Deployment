// Global PRIVATE gateway for ctm-global: Application Gateway Standard_v2 with
// a static private-only frontend in snet-ctm-appgw, LAYER-4 TCP listeners
// (443 + 8443) proxying to ONE backend pool that holds BOTH regional internal
// LB frontend IPs (primary + DR, reached over the utility<->utility global
// peering). The TCP health probe keeps only answering backends in rotation:
// only the active EM's LB passes it, so ctm-global.<zone> follows whichever
// region is up - automatic primary/DR behavior, DR joins when its EM answers.
// Layer 4 (TLS/TCP proxy) needs api-version 2023-11-01 or later.

@description('Platform name prefix (e.g. ctm-prod); the gateway is named <name>-appgw.')
param name string

@description('Dedicated gateway subnet id (snet-ctm-appgw). The static private frontend IP is derived from its prefix.')
param subnetId string

@description('Azure region for the gateway.')
param location string

@description('Backend pool IPs: [primary internal LB frontend IP, DR internal LB frontend IP].')
param backendIps array

@description('Existing platform private DNS zone (e.g. prod.ctm.internal, in this resource group) receiving the ctm-global A record.')
param dnsZoneName string

@description('Control-M Web / Automation API port on the internal LBs (backend + probe port).')
param backendPort int = 8443

@description('Tags applied to every resource.')
param tags object = {}

var appgwName = '${name}-appgw'
var frontendName = 'private-frontend'
var backendPoolName = 'ctm-em-lbs'
var backendSettingsName = 'tcp-${backendPort}'
var probeName = 'em-lb-tcp'

// Frontend ports proxied to the internal LBs (443 for browsers, backendPort
// for the Automation API / native clients) - same pairing as the regional LBs.
var listenPorts = [443, backendPort]

// ---------------------------------------------------------------------------
// Static private frontend IP, derived from the gateway subnet's prefix so a
// re-deploy always converges on the same address (and the ctm-global A record
// below never drifts). Azure reserves the first four addresses of a subnet
// (cidrHost index 0 = .1 gateway, 1-2 = DNS): index 3 = the first assignable
// host, e.g. 10.70.2.68 in 10.70.2.64/26.
// ---------------------------------------------------------------------------
var subnetIdParts = split(subnetId, '/')

resource gatewayVnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: subnetIdParts[8]
  scope: resourceGroup(subnetIdParts[4])
}

resource gatewaySubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: gatewayVnet
  name: subnetIdParts[10]
}

var frontendPrivateIp = cidrHost(gatewaySubnet.properties.addressPrefix, 3)

resource appgw 'Microsoft.Network/applicationGateways@2023-11-01' = {
  name: appgwName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'Standard_v2'
      tier: 'Standard_v2'
    }
    // Autoscale 1-2 instances; capacity is intentionally omitted (autoscale owns it).
    autoscaleConfiguration: {
      minCapacity: 1
      maxCapacity: 2
    }
    gatewayIPConfigurations: [
      {
        name: 'gateway-ip'
        properties: {
          subnet: {
            id: subnetId
          }
        }
      }
    ]
    // PRIVATE-ONLY frontend: no public IP configuration at all.
    frontendIPConfigurations: [
      {
        name: frontendName
        properties: {
          privateIPAllocationMethod: 'Static'
          privateIPAddress: frontendPrivateIp
          subnet: {
            id: subnetId
          }
        }
      }
    ]
    frontendPorts: [
      for port in listenPorts: {
        name: 'port-${port}'
        properties: {
          port: port
        }
      }
    ]
    backendAddressPools: [
      {
        name: backendPoolName
        properties: {
          backendAddresses: [
            for ip in backendIps: {
              ipAddress: ip
            }
          ]
        }
      }
    ]
    // Layer-4 TCP health probe against the internal LB frontends: only a
    // region whose EM answers on the web port stays in rotation.
    probes: [
      {
        name: probeName
        properties: {
          protocol: 'Tcp'
          port: backendPort
          interval: 10
          timeout: 10
          unhealthyThreshold: 3
        }
      }
    ]
    // Layer-4 backend settings: plain TCP proxy to the LB web port. TLS stays
    // end-to-end between the client and the EM web tier (no decrypt here).
    backendSettingsCollection: [
      {
        name: backendSettingsName
        properties: {
          protocol: 'Tcp'
          port: backendPort
          timeout: 30
          probe: {
            id: resourceId('Microsoft.Network/applicationGateways/probes', appgwName, probeName)
          }
        }
      }
    ]
    // Layer-4 TCP listeners (TLS/TCP proxy): one per frontend port.
    listeners: [
      for port in listenPorts: {
        name: 'tcp-${port}'
        properties: {
          protocol: 'Tcp'
          frontendIPConfiguration: {
            id: resourceId(
              'Microsoft.Network/applicationGateways/frontendIPConfigurations',
              appgwName,
              frontendName
            )
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appgwName, 'port-${port}')
          }
        }
      }
    ]
    routingRules: [
      for (port, i) in listenPorts: {
        name: 'tcp-${port}'
        properties: {
          ruleType: 'Basic'
          priority: 100 + i * 10
          listener: {
            id: resourceId('Microsoft.Network/applicationGateways/listeners', appgwName, 'tcp-${port}')
          }
          backendAddressPool: {
            id: resourceId(
              'Microsoft.Network/applicationGateways/backendAddressPools',
              appgwName,
              backendPoolName
            )
          }
          backendSettings: {
            id: resourceId(
              'Microsoft.Network/applicationGateways/backendSettingsCollection',
              appgwName,
              backendSettingsName
            )
          }
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// ctm-global -> gateway frontend, in the EXISTING platform zone (created by
// the foundation phase in this resource group). Clients in any linked VNet
// use https://ctm-global.<zone> and land on whichever region is healthy;
// ctm-web stays the region-local alias.
// ---------------------------------------------------------------------------
resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' existing = {
  name: dnsZoneName
}

resource ctmGlobalRecord 'Microsoft.Network/privateDnsZones/A@2020-06-01' = {
  parent: zone
  name: 'ctm-global'
  properties: {
    ttl: 60
    aRecords: [
      {
        ipv4Address: frontendPrivateIp
      }
    ]
    metadata: tags
  }
  dependsOn: [
    appgw
  ]
}

output appgwId string = appgw.id
output appgwName string = appgw.name

@description('Static private frontend IP the ctm-global record points at.')
output privateIp string = frontendPrivateIp
