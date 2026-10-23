# Network requirements

Placement is per component and config-driven: the Enterprise Manager and all
shared services live in a per-platform **utility VNet**; every Control-M/Server
runs in its **own VNet** with its agent scale set beside it; the DR region
mirrors the layout with a DR utility VNet plus one DR VNet per DR-protected
Server. Three access-layer additions ride the utility VNets: **Azure Bastion**
(`AzureBastionSubnet`), the **private Application Gateway** behind the
`ctm-global` alias (`snet-ctm-appgw`, prod) and the **kvsync Function App's
VNet integration** (`snet-ctm-func`). A dedicated **build VNet**
(`vnet-ctm-build`) hosts the Azure DevOps scale-set build agents and peers to
*all three* utility VNets so one agent pool deploys into nonprod, prod and DR.

There are two ways to satisfy this document:

* **Option A — deploy the `network/` folder.** The standalone Bicep in
  `network/` creates everything below and needs **nothing but the az CLI** —
  it is built for the **cloud team to own**: `network/main.bicep` +
  `network/params/<env>.bicepparam` (quickstart in `network/README.md`). The
  exact same plan ships in `environments/<env>/config.yaml` under
  `network_infrastructure`, so the platform pipeline can alternatively deploy
  it with `ctmdeploy deploy --env <env> --phase network` when
  `network_infrastructure.create: true` — keep the two inputs in sync.
* **Option B — bring your own (the default, `create: false`)**: the network
  team provides everything in [the contract below](#required-vnets-and-subnets)
  before `ctmdeploy deploy` runs. The platform then never creates or modifies
  a VNet, subnet, NSG, peering, route table or firewall — it only **selects**
  them by name from `environments/<env>/config.yaml`, *joins* their subnets,
  and creates private-DNS zones/links onto them
  (inventory [below](#platform-created-private-endpoints-and-dns-inventory)).

Either way the identity boundary is hard: the platform deployer role
(`identity/deployer-role.bicep`) **cannot** write VNets/subnets/NSGs/peerings;
the network deployment needs the cloud team's identity (**Network
Contributor** on the `rg-network-*` groups — `docs/IDENTITY-PERMISSIONS.md`).

## Option A: the network/ folder

`network/main.bicep` is one subscription-scope deployment covering both
regions. Per `vnets` entry it creates:

* the **network resource groups** named by the entries (deduped);
* every **VNet** with its address space;
* every **subnet** — `data` delegated to
  `Microsoft.DBforPostgreSQL/flexibleServers`, `func` delegated to
  `Microsoft.Web/serverFarms` (Function VNet integration), `pe` set to
  `privateEndpointNetworkPolicies: Disabled`, and a role-`bastion` subnet
  force-named `AzureBastionSubnet` (an Azure requirement);
* **one NSG per subnet**, named `<vnet>-<subnet>-nsg`, with rules derived from
  the subnet `role` — exactly the rule sets of the
  [NSG guidance below](#nsg-guidance-per-subnet) — **except the bastion
  subnet**, which carries no NSG (Azure Bastion manages its own data plane);
* every **bidirectional peering** declared through `peer_to` (a VNet name or
  a list; declare each pairing on ONE side only): spoke ⇄ its utility VNet,
  the **prod-utility ⇄ dr-utility GLOBAL peering** (Application Gateway → DR
  LB, Bastion → DR), and **build ⇄ every utility VNet**.

VNet roles: `utility` (hub of a platform region), `spoke` (one Server + its
agents; peers to exactly one utility), `build` (the ADO build agents; peers
to one or more utilities, cross-environment by name) and `external`
(resolution-only — declares a VNet **another plan deploys** so peerings into
it can be emitted; used by `network/params/prod.bicepparam` for
`vnet-dev-utility`, which is why the **nonprod plan deploys first**). A
`peer_to` name with no matching entry is skipped and reported in the
deployment's `unresolvedPeerings` output — check it is empty.

When `network_infrastructure.create: true`, config loading also
**cross-validates** the placement blocks: every VNet/subnet referenced by
`network.utility`, `servers.<stage>.network`, `dr.network.utility`,
`dr.server_networks.*`, the access layer (bastion/appgw/func subnet roles in
the utility entries) and `build_agents.network` must exist in the vnets list
with the right subnet role; every address space and prefix must parse; VNet
spaces must not overlap (pairwise, across both regions); and subnets must sit
inside their VNet without overlapping each other.

### PoC address plan (shipped in the config files AND the bicepparam files)

Address spaces are non-overlapping across all platforms AND between primary
and DR — a hard requirement for DR/failback and for the build-agent peerings.

| Platform / region | VNet | Role | Address space | Subnets |
|---|---|---|---|---|
| nonprod, eastus2 | `vnet-dev-utility` | utility | `10.60.0.0/22` | `snet-ctm-em` `10.60.0.0/24` (em) · `snet-ctm-postgres` `10.60.1.0/27` (data) · `snet-ctm-pe` `10.60.1.64/26` (pe) · `AzureBastionSubnet` `10.60.2.0/26` (bastion) · `snet-ctm-func` `10.60.2.64/26` (func) |
| nonprod, eastus2 | `vnet-ctm-dev` | spoke → `vnet-dev-utility` | `10.60.8.0/24` | `snet-ctm-server` `10.60.8.0/26` (server) · `snet-ctm-agents` `10.60.8.64/26` (agents) |
| nonprod, eastus2 | `vnet-ctm-qa` | spoke → `vnet-dev-utility` | `10.60.9.0/24` | `snet-ctm-server` `10.60.9.0/26` (server) · `snet-ctm-agents` `10.60.9.64/26` (agents) |
| prod, eastus2 | `vnet-prod-utility` | utility, peer_to `[vnet-dr-utility]` (**global**) | `10.70.0.0/22` | `snet-ctm-em` `10.70.0.0/24` (em) · `snet-ctm-postgres` `10.70.1.0/27` (data) · `snet-ctm-pe` `10.70.1.64/26` (pe) · `AzureBastionSubnet` `10.70.2.0/26` (bastion) · `snet-ctm-appgw` `10.70.2.64/26` (appgw) · `snet-ctm-func` `10.70.2.128/26` (func) |
| prod, eastus2 | `vnet-ctm-uat` | spoke → `vnet-prod-utility` | `10.70.8.0/24` | `snet-ctm-server` `10.70.8.0/26` (server) · `snet-ctm-agents` `10.70.8.64/26` (agents) |
| prod, eastus2 | `vnet-ctm-prod` | spoke → `vnet-prod-utility` | `10.70.9.0/24` | `snet-ctm-server` `10.70.9.0/26` (server) · `snet-ctm-agents` `10.70.9.64/26` (agents) |
| prod DR, centralus | `vnet-dr-utility` | utility | `10.80.0.0/22` | `snet-ctm-em` `10.80.0.0/24` (em) · `snet-ctm-postgres` `10.80.1.0/27` (data) · `snet-ctm-pe` `10.80.1.64/26` (pe) |
| prod DR, centralus | `vnet-ctm-prod-dr` | spoke → `vnet-dr-utility` | `10.80.9.0/24` | `snet-ctm-server` `10.80.9.0/26` (server) · `snet-ctm-agents` `10.80.9.64/26` (agents) |
| prod, eastus2 | `vnet-ctm-build` | build, peer_to `[vnet-dev-utility, vnet-prod-utility, vnet-dr-utility]` | `10.90.0.0/24` | `snet-ctm-build` `10.90.0.0/26` (build) |

Network resource groups: `rg-network-nonprod`, `rg-network-prod`,
`rg-network-prod-dr`. The `build ⇄ vnet-dev-utility` pairing crosses the two
plans; `network/params/prod.bicepparam` carries a `role: external` entry for
`vnet-dev-utility` so both directions are emitted (the config-driven `--phase
network` deployment of prod, which only sees one environment's list, skips
that one pair and reports it in `unresolvedPeerings` — deploy it from the
`network/` folder, or add the nonprod side by hand).

### The two CIDR parameters

* `bastion_cidrs` MUST keep the `AzureBastionSubnet` prefixes (nonprod
  `10.60.2.0/26`, prod `10.70.2.0/26`): Bastion sessions egress from that
  subnet and the em/server/agents/build subnets allow SSH 22 **only** from
  these CIDRs (the prod prefix also covers Bastion → DR over the global
  peering). Append jump-host/operator CIDRs as needed.
* `user_access_cidrs` in prod MUST keep the appgw subnet prefix
  (`10.70.2.64/26`): the gateway's backend connections to the EM internal LBs
  of **both** regions ride the em-subnet 443/8443 rule. Append the operator
  networks that should reach Control-M Web / the Automation API.

### What stays bring-your-own even in Option A

* **DNS forwarding from custom DNS servers** — hub resolvers / AD DNS must
  forward the platform and privatelink zones to Azure DNS (see [DNS](#dns)).
* **On-prem / WAN connectivity** — no gateways, ExpressRoute, VPN or hub
  peering is created; reaching the VNets is your transit's job
  (`allowForwardedTraffic` is enabled on every peering, so an NVA keeps
  working).
* **Outbound internet egress** — no NAT Gateway or firewall is deployed;
  provide one if your subscription disables default outbound access.

(The bastion/jump host is **no longer** your problem: `access.bastion.enabled`
deploys Azure Bastion into the `AzureBastionSubnet`.)

The rest of this document is the contract Option A implements and Option B
must provide.

## Required VNets and subnets

| VNet role | Config keys | Subnets (role) | What lands there |
|-----------|-------------|----------------|------------------|
| **Utility VNet** (one per platform) | `network.utility.*` | `em_subnet_name` (em), `data_subnet_name` (data), `pe_subnet_name` (pe), `AzureBastionSubnet` (bastion), `snet-ctm-appgw` (appgw, prod), `snet-ctm-func` (func) | EM VMs + internal LB frontend; PostgreSQL (delegated); private endpoints; Azure Bastion; the private Application Gateway (`ctm-global`); the kvsync Function's VNet integration |
| **Server VNet** (one per Control-M/Server, REQUIRED) | `servers.<stage>.network.*` | `server_subnet_name` (server), `agents_subnet_name` (agents) | the stage's Server VM pair; its agent VM scale set |
| **DR utility VNet** (prod only) | `dr.network.utility.*` | em, data, pe | failed-over EM VMs + DR LB frontend; PostgreSQL replica; DR private endpoints |
| **DR server VNet** (one per DR-protected Server) | `dr.server_networks.<stage>.*` | server, agents | the stage's failed-over Server VMs; its warm agent scale set |
| **Build VNet** (prod config) | `build_agents.network.*` | `snet-ctm-build` (build) | the `ctm-build-agents` VM scale set (Azure DevOps elastic pool) |

A Server participates in DR **if and only if** it has a
`dr.server_networks.<stage>` entry (shipped prod config: CTM_PROD only).

Subnet sizing and must-haves:

| Subnet role | Size | Must have |
|-------------|------|-----------|
| em | /27 minimum (/24 shipped) | outbound internet (NAT Gateway, Azure Firewall or proxy) for BMC/Azure APIs |
| data | /28 minimum (/27 shipped) | **delegation to `Microsoft.DBforPostgreSQL/flexibleServers`**, nothing else in it |
| pe | /27 | private endpoint network policies disabled (default) |
| bastion | **/26 minimum**, name **exactly `AzureBastionSubnet`** | **no NSG** (Azure Bastion manages its own data plane); only in regions where `access.bastion.enabled` |
| appgw | /26 (Application Gateway v2 wants room to scale) | dedicated to the gateway; NSG must admit GatewayManager 65200-65535; prod utility VNet only |
| func | /26 | **delegation to `Microsoft.Web/serverFarms`**, nothing else in it; only where `kv_sync.enabled` |
| server | /28 | outbound internet as above |
| agents | /24 minimum, /22 recommended for the prod pool | outbound internet as above |
| build | /26 | outbound internet (Azure DevOps, ARM, Key Vault, blob — all 443) |

Agents can be relocated to an entirely different VNet with the optional
`servers.<stage>.network.agents_vnet_name` /
`agents_vnet_resource_group_name` overrides; that VNet then needs the same
peering, flows and DNS treatment as an agents subnet.

## Required peering

* **Each server VNet ⇄ its platform's utility VNet** — bidirectional,
  REQUIRED. EM ⇄ Server control traffic, Server → database and every node →
  private endpoints cross it.
* **Server VNets are never peered to each other.** That is the isolation
  guarantee: one stage has no network path to another stage's Server or
  agents.
* **`vnet-prod-utility` ⇄ `vnet-dr-utility` — GLOBAL peering, REQUIRED on
  prod**: the Application Gateway's backend pool holds the **DR** internal
  LB frontend IP (TCP 8443 + probe cross the peering), and Azure Bastion
  reaches DR nodes through it. New in v4 — previously no cross-region
  connectivity was required.
* **`vnet-ctm-build` ⇄ every utility VNet** (`vnet-dev-utility`,
  `vnet-prod-utility`, `vnet-dr-utility`): the build agents deploy into all
  three platforms and reach each platform's private endpoints through its
  utility VNet. The dev pairing crosses the nonprod/prod plans — see the
  `external` note above.
* DR region, same spoke rule: each DR server VNet ⇄ the DR utility VNet only.
* If the `agents_vnet_name` override is used, the agents VNet must peer with
  **both** the utility VNet and its Server's VNet.

## Cross-VNet flows (the full flow/port matrix)

These flows must be allowed end to end (peering, NSGs, any firewall on the
path):

| # | Flow | From → To | Ports |
|---|------|-----------|-------|
| 1 | EM ⇄ Server | utility em subnet ⇄ each server subnet (both directions) | TCP 2369 (Configuration Agent) + Control-M dynamic ports |
| 2 | Server → database | server subnet → utility data subnet | TCP 5432 |
| 3 | Agents → Automation API (via the internal LB) | agents subnet → utility em subnet | TCP 8443 |
| 4 | Every node → private endpoints | all em/server/agents subnets → utility pe subnet | TCP 443 (Key Vault + **blob: the node-install script for every node — agent scale sets included, via the shared node identity — and the EM/Server installer media**; v4.1: the agent itself is baked into the ctm-agent image), TCP 2049 (NFS) |
| 5 | Agent ⇄ Server | agents subnet ⇄ server subnet (same VNet unless relocated) | TCP 7005-7006 |
| 6 | Users / API clients → `ctm-global` | operator CIDRs (+ any platform VNet) → appgw frontend (snet-ctm-appgw) | TCP 443, 8443 |
| 7 | Application Gateway → primary internal LB | snet-ctm-appgw → prod utility em subnet | TCP 8443 (backend + TCP health probe) |
| 8 | Application Gateway → **DR** internal LB | snet-ctm-appgw → DR utility em subnet, over the **global** utility ⇄ utility peering | TCP 8443 (backend + probe; healthy-only routing = automatic primary/DR) |
| 9 | Azure Bastion → every node + build agent | AzureBastionSubnet → em/server/agents/build subnets (DR via the global peering) | TCP 22 |
| 10 | GatewayManager → Application Gateway | service tag → snet-ctm-appgw | TCP 65200-65535 (v2 control plane) |
| 11 | kvsync Function → Key Vault | snet-ctm-func → utility pe subnet (KV private endpoint) | TCP 443 |
| 12 | kvsync Function → EM Automation API | snet-ctm-func → utility em subnet (internal LB) | TCP 8443 |
| 13 | Build agents → nonprod/prod/DR private endpoints | snet-ctm-build → each utility pe subnet, over the build peerings | TCP 443 (Key Vault secrets officer ops, blob media/script uploads) |
| 14 | Build agents → Azure control planes | snet-ctm-build → Internet (ARM `management.azure.com`, Azure DevOps `dev.azure.com`, Entra ID) | TCP 443 outbound |
| 15 | Operators → Azure Bastion control plane | Internet → the Bastion public IP (Azure-managed; sessions terminate on private VM addresses only) | TCP 443 |

**Control-M dynamic ports** (flow 1): the CORBA naming service, GUI server
and gateway negotiate TCP ports at run time. Either keep EM ⇄ Server open on
all TCP between the em subnet and each server subnet (the shipped NSGs do —
the peering is point-to-point and both ends are platform-owned), or pin the
ranges in Control-M and mirror exactly those in the NSGs.

## NSG guidance per subnet

Write rules against the subnets' CIDRs; "server subnets" means every
`servers.<stage>.network` server subnet. End every inbound list with a
deny-all at 4096; outbound stays open (agents run jobs, build agents reach
Azure DevOps). This is exactly what `network/modules/vnet.bicep` derives per
subnet role.

### Utility em subnet (EM VMs, internal LB frontend)

| Priority | Direction | Source | Ports | Note |
|----------|-----------|--------|-------|------|
| 100 | Inbound | `user_access_cidrs` (**must include the appgw subnet prefix**) | 443, 8443 | Control-M Web / Automation API from operators **and the gateway's backend connections — both regions** |
| 110 | Inbound | all spoke address spaces | TCP * (or pinned ranges) | Server ⇄ EM 2369 + dynamic ports; also covers agents → 8443 |
| 120 | Inbound | `AzureLoadBalancer` | * | health probes |
| 130 | Inbound | `bastion_cidrs` | 22 | operator SSH via Bastion |
| 140 | Inbound | own VNet address space | * | intra-utility (LB, PG, PEs, appgw, func) |
| 4096 | Inbound | * | * | deny |

### Server subnet / Agents subnet (per server VNet)

As before: server subnets admit all TCP from the peered utility space (the
dynamic-port tradeoff), 7005-7006 + intra from their own VNet and 22 from
`bastion_cidrs`; agents subnets admit 7005-7006 + intra from their own VNet
and 22 from `bastion_cidrs`; deny the rest. Jobs' extra outbound rules belong
to the agents subnet's own policy, never to another stage's VNet.

### Utility data subnet (PostgreSQL, delegated)

Inbound TCP 5432 from the utility VNet and **every** server subnet, deny the
rest. Nothing but the Flexible Server lives here.

### Utility pe subnet (private endpoints)

Inbound TCP 443 and 2049 from the utility VNet and every spoke. With private
endpoint network policies disabled (the default) NSG rules here are not
enforced — the per-source control lives in the sources' outbound rules. The
build VNet's PE traffic (flow 13) arrives over peering regardless.

### Bastion subnet

**No NSG.** The subnet is named exactly `AzureBastionSubnet` and Azure
Bastion manages its own data plane. Keep its prefix in `bastion_cidrs`.

### Application Gateway subnet (prod utility VNet)

| Priority | Direction | Source | Ports | Note |
|----------|-----------|--------|-------|------|
| 100 | Inbound | `GatewayManager` (service tag) | 65200-65535 | v2 control plane — an Azure platform requirement |
| 110 | Inbound | `user_access_cidrs` | 443, 8443 | gateway frontend (`ctm-global`) from operator networks |
| 120 | Inbound | `AzureLoadBalancer` | * | platform probes |
| 130 | Inbound | `VirtualNetwork` (service tag) | 443, 8443 | frontend from the VNet + everything peered to it |
| 4096 | Inbound | * | * | deny |

### Function subnet (`snet-ctm-func`)

Delegated to `Microsoft.Web/serverFarms`; it only **originates** traffic
(Function VNet integration with `vnetRouteAllEnabled`), so inbound is
intra-VNet + deny. Egress needs are flows 11-12 plus the Functions host's own
calls to its storage account over 443.

### Build subnet (`snet-ctm-build`)

Inbound TCP 22 from `bastion_cidrs` + the peered utility address spaces only,
deny the rest; outbound open (flows 13-14).

## DNS

* The platform creates one private DNS zone per region named
  `<platform>.ctm.internal` with auto-registration links to every VNet of the
  platform (utility + each server VNet + relocated-agents VNets +
  `network.extra_dns_vnets`). VM hostnames resolve across VNets; the managed
  aliases are `ctm-web` (A → that region's LB frontend), `ctm-db` (CNAME →
  PostgreSQL / the DR replica) and — new in v4, prod primary zone only —
  **`ctm-global` (A → the Application Gateway's static private frontend IP,
  created by the access phase)**. Clients in any linked VNet use
  `https://ctm-global.prod.ctm.internal` and land on whichever region's EM is
  healthy; `ctm-web` stays the region-local alias.
* Private endpoints need the standard zones
  `privatelink.blob.core.windows.net`, `privatelink.file.core.windows.net`,
  `privatelink.vaultcore.azure.net` and a `<x>.postgres.database.azure.com`
  zone. Pass hub-managed zone ids through
  `network.existing_private_dns_zone_ids`; anything omitted is created in the
  platform resource group and linked to every platform VNet (registration
  off). Hub-managed zones you pass in are *not* linked by the deployment.
* **Build agents**: the build VNet is not linked to any platform zone by
  default. Add `{vnet_name: vnet-ctm-build, vnet_resource_group_name:
  rg-network-prod}` to `network.extra_dns_vnets` (in both configs, or at
  least prod) if the agents should resolve the Key Vault/blob **private**
  endpoints; otherwise their data-plane calls use the public endpoints and
  must pass the Key Vault/storage firewalls (`deployer_ip_cidrs` must then
  cover the agents' egress IP).
* If any VNet uses custom DNS servers, those servers must forward
  `*.ctm.internal`, the privatelink zones and
  `*.postgres.database.azure.com` to Azure DNS (`168.63.129.16`) or a DNS
  Private Resolver.

## Platform-created private endpoints and DNS (inventory)

Exactly what the platform drops into your networks. Names use the prod
platform (`p01`); nonprod and DR follow the same rules.

### Private endpoints (3 per platform region, all in the utility pe subnet)

| Private endpoint | Target resource | Group id | Zone wired via zone group |
|---|---|---|---|
| `ctm-prod-kv-pe` | Key Vault `ctm-prod-kv-p01` | `vault` | `privatelink.vaultcore.azure.net` |
| `ctm-prod-blob-pe` | Storage `ctmprodblobp01` (installers/backups) | `blob` | `privatelink.blob.core.windows.net` |
| `ctm-prod-files-pe` | Storage `ctmprodfilesp01` (NFS share) | `file` | `privatelink.file.core.windows.net` |

The DR region gets its own three in the DR utility pe subnet. PostgreSQL uses
VNet integration (the delegated data subnet), not a private endpoint. The
kvsync Functions **host storage account** (`<compact>func<hash>`) is reached
over its public endpoint by the Functions control plane — it holds only host
state and the durable task hub, never platform secrets.

### Access-layer records and endpoints (access phase)

| Resource | Where | Note |
|---|---|---|
| `ctm-global` A record → appgw private frontend IP | `prod.ctm.internal` (primary zone) | the gateway IP is static (derived from the subnet prefix), so the record never drifts |
| Bastion public IP `ctm-<env>-bastion-pip` | platform RG | Azure Bastion's own control-plane endpoint — **not** an entry point to any VM; sessions terminate on private addresses |

### Zone-link matrix

Each created zone gets one VNet link per platform VNet (utility + server
VNets + extras; the platform zone's links are registration-enabled). With the
shipped configs: nonprod 3 links per zone, prod primary 3, prod DR 2 — plus
one more everywhere if you add `vnet-ctm-build` to `extra_dns_vnets`.

## Cross-region (DR)

* DR applies to the EM plus the Servers in `dr.server_networks` (shipped:
  CTM_PROD only). The shared PostgreSQL replica still carries every database,
  `ctm_uat` included.
* Site Recovery network mappings are per VNet pair: utility → DR utility,
  each DR-protected server VNet → its DR VNet. Failed-over NICs land in the
  matching subnet role.
* The DR utility VNet needs the em/data/pe roles; each DR server VNet needs
  server + agents. NSGs and spoke peerings mirror the primary.
* **The prod-utility ⇄ dr-utility global peering is now REQUIRED** (gateway
  backend + Bastion reach — flows 8 and 9). Replication itself still runs
  over Azure backbone services and needs no peering; primary and DR address
  spaces must not overlap (they never do in the shipped plan).
* Platform secrets, DNS aliases and hostnames are identical in both regions,
  so failover needs no application reconfiguration; `ctm-global` follows the
  healthy region automatically through the gateway's TCP health probe.

## Packer builds

The temporary build VM Packer creates (own throwaway resource group, default
networking) needs outbound internet for RHEL packages and Node.js; it does
not touch the platform VNets. If your subscription policy forbids public IPs,
point the azure-arm builder's `virtual_network_name` /
`virtual_network_subnet_name` at a subnet with outbound access — the
`snet-ctm-build` subnet is a reasonable host.
