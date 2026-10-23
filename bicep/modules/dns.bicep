// Platform private DNS zone (e.g. prod.ctm.internal) with an auto-registration
// VNet link so VM hostnames resolve automatically, plus the static A/CNAME
// aliases (ctm-web, ctm-db) at TTL 60. Ported from the old modules/dns.

@description('Private DNS zone name for the platform (e.g. prod.ctm.internal).')
param zoneName string

@description('VNet to link with auto-registration so VM hostnames resolve automatically.')
param vnetId string

@description('Static A records to create: name => IPv4 address.')
param aRecords object = {}

@description('CNAME records to create: name => target FQDN (e.g. ctm-db => <server>.postgres.database.azure.com).')
param cnameRecords object = {}

@description('Tags applied to every resource created by this module (stored as record metadata on DNS records).')
param tags object = {}

resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: zoneName
  location: 'global'
  tags: tags
}

// registrationEnabled => every VM in the VNet gets an A record named after its hostname.
resource vnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: zone
  name: 'platform-vnet'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: true
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource staticARecords 'Microsoft.Network/privateDnsZones/A@2020-06-01' = [
  for entry in items(aRecords): {
    parent: zone
    name: entry.key
    properties: {
      ttl: 60
      aRecords: [
        {
          ipv4Address: entry.value
        }
      ]
      metadata: tags
    }
  }
]

resource staticCnameRecords 'Microsoft.Network/privateDnsZones/CNAME@2020-06-01' = [
  for entry in items(cnameRecords): {
    parent: zone
    name: entry.key
    properties: {
      ttl: 60
      cnameRecord: {
        cname: entry.value
      }
      metadata: tags
    }
  }
]

output zoneId string = zone.id
output zoneName string = zone.name
