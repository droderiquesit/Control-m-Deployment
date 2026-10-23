// Platform private DNS zone (e.g. prod.ctm.internal) with one auto-registration
// VNet link PER platform VNet so every VM hostname resolves regardless of which
// isolated VNet it lands in, plus the static A/CNAME aliases (ctm-web, ctm-db)
// at TTL 60. Ported from the old modules/dns; v2 takes a list of VNets.

@description('Private DNS zone name for the platform (e.g. prod.ctm.internal).')
param zoneName string

@description('VNets to link with auto-registration so VM hostnames resolve automatically. Items: {name, vnetId}; the link is named after the entry. Azure supports multiple auto-registration links on one zone (one per VNet).')
param vnetLinks array

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

// registrationEnabled => every VM in the linked VNet gets an A record named
// after its hostname. One link per entry; a VNet may carry at most one
// registration-enabled link, which the orchestrator's dedupe guarantees.
resource zoneVnetLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for link in vnetLinks: {
    parent: zone
    name: link.name
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: true
      virtualNetwork: {
        id: link.vnetId
      }
    }
  }
]

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
