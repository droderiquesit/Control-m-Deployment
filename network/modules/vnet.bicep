// One platform VNet with role-aware subnets and ONE NSG PER SUBNET (except
// the bastion subnet — see below), deployed (resource-group scope) by
// network/main.bicep for every non-external entry of
// network_infrastructure.vnets.
//
// Peer/spoke address spaces are resolved by network/main.bicep from the vnets
// plan itself (no python required) and passed in as parameters.
//
// Subnet roles and what they derive (rule sets per docs/NETWORK-REQUIREMENTS.md;
// allow rules ascend from priority 100, every NSG ends with an explicit
// deny-all inbound at 4096; outbound is left open — the agents and build
// subnets in particular stay outbound-open for jobs / pipeline tooling):
//   em      — EM VMs + internal LB frontend: Web/API from user CIDRs, ALL TCP
//             from the spoke address spaces (Server -> EM: 2369 + dynamic CORBA
//             ports, the same documented tradeoff as the server subnets; this
//             also covers the agents' 8443 to the Automation API), health
//             probes, bastion SSH, intra-VNet. The Application Gateway's
//             backend connections (8443 to the internal LBs of BOTH regions)
//             ride the user-CIDR rule: keep the appgw subnet prefix in
//             user_access_cidrs (the shipped plan does).
//   data    — PostgreSQL Flexible Server subnet: delegated to
//             Microsoft.DBforPostgreSQL/flexibleServers; 5432 from the utility
//             VNet + every spoke.
//   pe      — private endpoints: privateEndpointNetworkPolicies Disabled;
//             443/2049 from the utility VNet + every spoke.
//   server  — Control-M/Server VMs: ALL TCP from the peered utility address
//             space (EM <-> Server negotiate dynamic CORBA ports at run time —
//             documented tradeoff in docs/NETWORK-REQUIREMENTS.md; pin the
//             ranges in Control-M to narrow it), 7005-7006 + intra from its
//             own VNet, bastion SSH.
//   agents  — agent VM scale set: 7005-7006 + intra from its own VNet,
//             bastion SSH.
//   bastion — Azure Bastion: the subnet is created with the literal name
//             'AzureBastionSubnet' (an Azure requirement, whatever the plan
//             entry is called) and carries NO NSG — Azure Bastion manages its
//             own data plane. Put this subnet's prefix in bastion_cidrs so
//             bastion sessions pass the SSH rules of the other subnets.
//   appgw   — Application Gateway Standard_v2 (private frontend): the v2
//             control plane 65200-65535 from GatewayManager, health probes,
//             frontend 443/8443 from user CIDRs + VirtualNetwork (the VNet
//             and everything peered to it), deny the rest.
//   func    — Function App VNet integration (kvsync): delegated to
//             Microsoft.Web/serverFarms; the subnet only ORIGINATES traffic,
//             so inbound is intra-VNet + deny.
//   build   — Azure DevOps scale-set build agents: SSH 22 from bastion_cidrs
//             + the peered utility VNet address spaces only, deny the rest;
//             outbound open (ADO, ARM, Key Vault, blob are all outbound 443).

@description('One network_infrastructure.vnets entry, raw config shape: name, resource_group_name, location, role (utility|spoke|build), address_space, subnets [{name, prefix, role}], optional peer_to (string or list of VNet names).')
param vnet object

@description('Address spaces of every VNet this one is peered with, in either direction (resolved by network/main.bicep from the plan, external entries included).')
param peerAddressSpaces array = []

@description('Address spaces of ALL spoke VNets of the platform, both regions — deliberately wider than the peered set so DR failback keeps working. Passed on utility VNets only.')
param spokeAddressSpaces array = []

@description('CIDRs allowed to reach Control-M Web / Automation API (TCP 443, 8443) on em and appgw subnets (network_infrastructure.user_access_cidrs). Empty = no user-access rule.')
param userAccessCidrs array = []

@description('CIDRs allowed SSH (TCP 22) to node/build subnets (network_infrastructure.bastion_cidrs); include the AzureBastionSubnet prefixes. Empty = no SSH rule.')
param bastionCidrs array = []

@description('Tags applied to every resource created by this module.')
param tags object = {}

var ownAddressSpace = vnet.address_space

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

// Service-tag sources go in the singular sourceAddressPrefix (a prefix LIST
// may only hold CIDRs).
func allowTcpFromTag(ruleName string, priority int, sourceTag string, destinationPorts array, note string) object => {
  name: ruleName
  properties: {
    description: note
    priority: priority
    direction: 'Inbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: sourceTag
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRanges: destinationPorts
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
    : [allowTcpPorts('allow-user-web', 100, userAccessCidrs, ['443', '8443'], 'Control-M Web + Automation API from operator networks (+ the Application Gateway subnet, both regions)')],
  empty(spokeAddressSpaces)
    ? []
    : [allowAllPorts('allow-spoke-tcp', 110, 'Tcp', spokeAddressSpaces, 'Server -> EM: 2369 + dynamic CORBA ports (documented tradeoff; also covers agents -> Automation API 8443)')],
  [allowAzureLoadBalancer],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 130, bastionCidrs, ['22'], 'Operator SSH (Azure Bastion subnets + any extra operator CIDRs)')],
  [allowAllPorts('allow-intra-vnet', 140, '*', [ownAddressSpace], 'Utility VNet internal traffic (EM, LB, PG, PEs, appgw backend, kvsync function)')],
  [denyAllInbound]
)

var serverRules = concat(
  empty(peerAddressSpaces)
    ? []
    : [allowAllPorts('allow-utility-tcp', 100, 'Tcp', peerAddressSpaces, 'EM <-> Server: 2369 + dynamic CORBA ports (documented tradeoff)')],
  [allowTcpPorts('allow-agent-server', 110, [ownAddressSpace], ['7005-7006'], 'Agent <-> Server from this VNet')],
  [allowAllPorts('allow-intra-vnet', 120, '*', [ownAddressSpace], 'Server VNet internal traffic')],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 130, bastionCidrs, ['22'], 'Operator SSH (Azure Bastion subnets + any extra operator CIDRs)')],
  [denyAllInbound]
)

var agentsRules = concat(
  [
    allowTcpPorts('allow-server-agent', 100, [ownAddressSpace], ['7005-7006'], 'Server -> Agent from this VNet')
    allowAllPorts('allow-intra-vnet', 110, '*', [ownAddressSpace], 'Server VNet internal traffic')
  ],
  empty(bastionCidrs) ? [] : [allowTcpPorts('allow-bastion-ssh', 120, bastionCidrs, ['22'], 'Operator SSH (Azure Bastion subnets + any extra operator CIDRs)')],
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

var appgwRules = concat(
  [allowTcpFromTag('allow-gateway-manager', 100, 'GatewayManager', ['65200-65535'], 'Application Gateway v2 control plane (platform requirement)')],
  empty(userAccessCidrs)
    ? []
    : [allowTcpPorts('allow-user-frontend', 110, userAccessCidrs, ['443', '8443'], 'Gateway frontend (ctm-global) from operator networks')],
  [allowAzureLoadBalancer],
  [allowTcpFromTag('allow-vnet-frontend', 130, 'VirtualNetwork', ['443', '8443'], 'Gateway frontend from this VNet + everything peered to it (bastion sessions, platform nodes)')],
  [denyAllInbound]
)

var funcRules = [
  allowAllPorts('allow-intra-vnet', 100, '*', [ownAddressSpace], 'Utility VNet internal traffic; the subnet only originates traffic (Function VNet integration)')
  denyAllInbound
]

var buildRules = concat(
  empty(concat(bastionCidrs, peerAddressSpaces))
    ? []
    : [allowTcpPorts('allow-build-ssh', 100, concat(bastionCidrs, peerAddressSpaces), ['22'], 'SSH to build agents from Azure Bastion + the peered utility VNets only')],
  [denyAllInbound]
)

var rulesByRole = {
  em: emRules
  server: serverRules
  agents: agentsRules
  data: dataRules
  pe: peRules
  appgw: appgwRules
  func: funcRules
  build: buildRules
  bastion: [] // placeholder only: bastion subnets never get an NSG (loop below skips them)
}

// ---------------------------------------------------------------------------
// One NSG per subnet, named <vnet>-<subnet>-nsg — except the bastion subnet,
// which Azure Bastion manages itself (no NSG). Then the VNet with every
// subnet attached to its NSG. Deterministic names only — safe to re-deploy.
// ---------------------------------------------------------------------------
resource subnetNsgs 'Microsoft.Network/networkSecurityGroups@2023-11-01' = [
  for subnet in vnet.subnets: if (subnet.role != 'bastion') {
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
        // Azure requires the Bastion subnet to be named exactly this.
        name: subnet.role == 'bastion' ? 'AzureBastionSubnet' : subnet.name
        properties: {
          addressPrefix: subnet.prefix
          networkSecurityGroup: subnet.role == 'bastion' ? null : { id: subnetNsgs[index].id }
          // data hosts PostgreSQL Flexible Server, func hosts the kvsync
          // Function App's VNet integration; nothing else is delegated.
          delegations: subnet.role == 'data'
            ? [
                {
                  name: 'postgres-flexible-servers'
                  properties: {
                    serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
                  }
                }
              ]
            : (subnet.role == 'func'
                ? [
                    {
                      name: 'app-service-plans'
                      properties: {
                        serviceName: 'Microsoft.Web/serverFarms'
                      }
                    }
                  ]
                : [])
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
    name: subnet.role == 'bastion' ? 'AzureBastionSubnet' : subnet.name
    role: subnet.role
    id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, subnet.role == 'bastion' ? 'AzureBastionSubnet' : subnet.name)
  }
]
