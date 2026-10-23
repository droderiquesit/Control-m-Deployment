// One platform VNet with role-aware subnets and ONE NSG PER SUBNET, deployed
// (resource-group scope) by bicep/network.bicep for every entry of
// network_infrastructure.vnets when network_infrastructure.create is true.
//
// Subnet roles and what they derive (rule sets per docs/NETWORK-REQUIREMENTS.md;
// allow rules ascend from priority 100, every subnet ends with an explicit
// deny-all inbound at 4096; outbound is left open — the agents subnet in
// particular stays outbound-open for whatever the jobs need):
//   em     — EM VMs + internal LB frontend: Web/API from user CIDRs, ALL TCP
//            from the spoke address spaces (Server -> EM: 2369 + dynamic CORBA
//            ports, the same documented tradeoff as the server subnets; this
//            also covers the agents' 8443 to the Automation API), health
//            probes, bastion SSH, intra-VNet.
//   data   — PostgreSQL Flexible Server subnet: delegated to
//            Microsoft.DBforPostgreSQL/flexibleServers; 5432 from the utility
//            VNet + every spoke.
//   pe     — private endpoints: privateEndpointNetworkPolicies Disabled;
//            443/2049 from the utility VNet + every spoke.
//   server — Control-M/Server VMs: ALL TCP from the utility address space
//            (EM <-> Server negotiate dynamic CORBA ports at run time —
//            documented tradeoff in docs/NETWORK-REQUIREMENTS.md; pin the
//            ranges in Control-M to narrow it), 7005-7006 + intra from its own
//            VNet, bastion SSH.
//   agents — agent VM scale set: 7005-7006 + intra from its own VNet, bastion SSH.

@description('One network_infrastructure.vnets entry, config.yaml shape: name, resource_group_name, location, role (utility|spoke), address_space, subnets [{name, prefix, role}], peer_to (spokes only). The orchestrator augments every item with peerAddressSpaces (address spaces of the VNets it peers with) and utility items with spokeAddressSpaces (address spaces of ALL spoke VNets of the platform, both regions — deliberately wider than the peered set so DR failback keeps working).')
param vnet object

@description('CIDRs allowed to reach Control-M Web / Automation API (TCP 443, 8443) on em subnets (network_infrastructure.user_access_cidrs). Empty = no user-access rule.')
param userAccessCidrs array = []

@description('CIDRs allowed SSH (TCP 22) to node subnets (network_infrastructure.bastion_cidrs). Empty = no SSH rule.')
param bastionCidrs array = []

@description('Tags applied to every resource created by this module.')
param tags object = {}

var ownAddressSpace = vnet.address_space
var peerAddressSpaces = vnet.?peerAddressSpaces ?? []
var spokeAddressSpaces = vnet.?spokeAddressSpaces ?? []

// ---------------------------------------------------------------------------
// Rule builders. NSG default rules would allow all peered-VNet traffic
// (VirtualNetwork tag), so every role pins its own allow list and closes with
// deny-all-inbound at 4096.
// ---------------------------------------------------------------------------
func allowTcpPorts(ruleName string, priority int, sourcePrefixes array, destinationPorts array, note string) object => {
  name: ruleName
  properties: {
    description: note
    priority: priority
    direction: 'Inbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefixes: sourcePrefixes
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRanges: destinationPorts
  }
}

func allowAllPorts(ruleName string, priority int, ruleProtocol string, sourcePrefixes array, note string) object => {
  name: ruleName
  properties: {
    description: note
    priority: priority
    direction: 'Inbound'
    access: 'Allow'
    protocol: ruleProtocol
    sourceAddressPrefixes: sourcePrefixes
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

var allowAzureLoadBalancer = {
  name: 'allow-azure-lb'
  properties: {
    description: 'Internal load balancer health probes'
    priority: 120
    direction: 'Inbound'
    access: 'Allow'
    protocol: '*'
    sourceAddressPrefix: 'AzureLoadBalancer'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

var denyAllInbound = {
  name: 'deny-all-inbound'
  properties: {
    description: 'Explicit deny tail; platform allows are pinned above'
    priority: 4096
    direction: 'Inbound'
    access: 'Deny'
    protocol: '*'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

// ---------------------------------------------------------------------------
// Role-derived inbound rule sets (docs/NETWORK-REQUIREMENTS.md). Rules whose
// source list is empty are omitted entirely (an NSG rule may not carry an
// empty prefix list); the remaining priorities keep their fixed, ascending slots.
// ---------------------------------------------------------------------------
var emRules = concat(
  empty(userAccessCidrs)
    ? []
    : [allowTcpPorts('allow-user-web', 100, userAccessCidrs, ['443', '8443'], 'Control-M Web + Automation API from operator networks')],
  empty(spokeAddressSpaces)
    ? []
    : [allowAllPorts('allow-spoke-tcp', 110, 'Tcp', spokeAddressSpaces, 'Server -> EM: 2369 + dynamic CORBA ports (documented tradeoff; also covers agents -> Automation API 8443)')],
  [allowAzureLoadBalancer],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 130, bastionCidrs, ['22'], 'Operator SSH')],
  [allowAllPorts('allow-intra-vnet', 140, '*', [ownAddressSpace], 'Utility VNet internal traffic (EM, LB, PG, PEs)')],
  [denyAllInbound]
)

var serverRules = concat(
  empty(peerAddressSpaces)
    ? []
    : [allowAllPorts('allow-utility-tcp', 100, 'Tcp', peerAddressSpaces, 'EM <-> Server: 2369 + dynamic CORBA ports (documented tradeoff)')],
  [allowTcpPorts('allow-agent-server', 110, [ownAddressSpace], ['7005-7006'], 'Agent <-> Server from this VNet')],
  [allowAllPorts('allow-intra-vnet', 120, '*', [ownAddressSpace], 'Server VNet internal traffic')],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 130, bastionCidrs, ['22'], 'Operator SSH')],
  [denyAllInbound]
)

var agentsRules = concat(
  [
    allowTcpPorts('allow-server-agent', 100, [ownAddressSpace], ['7005-7006'], 'Server -> Agent from this VNet')
    allowAllPorts('allow-intra-vnet', 110, '*', [ownAddressSpace], 'Server VNet internal traffic')
  ],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 120, bastionCidrs, ['22'], 'Operator SSH')],
  [denyAllInbound]
)

var dataRules = [
  allowTcpPorts('allow-postgres', 100, concat([ownAddressSpace], spokeAddressSpaces), ['5432'], 'EM + every Server -> PostgreSQL Flexible Server')
  denyAllInbound
]

var peRules = [
  allowTcpPorts('allow-private-endpoints', 100, concat([ownAddressSpace], spokeAddressSpaces), ['443', '2049'], 'Key Vault/blob 443 + NFS 2049 from every platform subnet')
  denyAllInbound
]

var rulesByRole = {
  em: emRules
  server: serverRules
  agents: agentsRules
  data: dataRules
  pe: peRules
}

// ---------------------------------------------------------------------------
// One NSG per subnet, named <vnet>-<subnet>-nsg, then the VNet with every
// subnet attached to its NSG. Deterministic names only — safe to re-deploy.
// ---------------------------------------------------------------------------
resource subnetNsgs 'Microsoft.Network/networkSecurityGroups@2023-11-01' = [
  for subnet in vnet.subnets: {
    name: '${vnet.name}-${subnet.name}-nsg'
    location: vnet.location
    tags: tags
    properties: {
      securityRules: rulesByRole[subnet.role]
    }
  }
]

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnet.name
  location: vnet.location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [ownAddressSpace]
    }
    subnets: [
      for (subnet, index) in vnet.subnets: {
        name: subnet.name
        properties: {
          addressPrefix: subnet.prefix
          networkSecurityGroup: {
            id: subnetNsgs[index].id
          }
          // data role hosts PostgreSQL Flexible Server and nothing else.
          delegations: subnet.role == 'data'
            ? [
                {
                  name: 'postgres-flexible-servers'
                  properties: {
                    serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
                  }
                }
              ]
            : []
          // pe role carries the platform private endpoints; policies off so
          // endpoint NICs can land here (NSG control lives on the sources).
          privateEndpointNetworkPolicies: subnet.role == 'pe' ? 'Disabled' : 'Enabled'
        }
      }
    ]
  }
}

output vnetId string = virtualNetwork.id
output vnetName string = virtualNetwork.name

@description('Subnets created, in config order: {name, role, id}.')
output subnets array = [
  for subnet in vnet.subnets: {
    name: subnet.name
    role: subnet.role
    id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, subnet.name)
  }
]
