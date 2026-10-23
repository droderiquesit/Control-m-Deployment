# Network requirements

Placement is per component and config-driven: the Enterprise Manager and all
shared services live in a per-platform **utility VNet**; every Control-M/Server
runs in its **own VNet** with its agent scale set beside it; the DR region
mirrors the layout with a DR utility VNet plus one DR VNet per DR-protected
Server.

There are two ways to satisfy this document:

* **Option A — let this repo create the network**: flip
  `network_infrastructure.create: true` and run the `network` phase (next
  section). The platform deployments themselves still only *select* networks
  by name — the network phase is a separate, optional, subscription-scope
  deployment.
* **Option B — bring your own (the default)**: the network team provides
  everything in [the contract below](#required-vnets-and-subnets) before
  `ctmdeploy deploy` runs. The platform then never creates or modifies a
  VNet, subnet, NSG, peering, route table or firewall — it only **selects**
  them by name from `environments/<env>/config.yaml` and creates private-DNS
  zones/links onto them (inventory [below](#platform-created-private-endpoints-and-dns-inventory)).

## Option A: let this repo create the network

`environments/<env>/config.yaml` carries an optional
`network_infrastructure:` block (schema: `docs/CONFIGURATION.md`). With
`create: false` — the shipped default — it is inert documentation of the
address plan. With `create: true`:

```bash
python -m ctmdeploy deploy --env <env> --phase network
```

deploys `bicep/network.bicep` (one subscription-scope deployment,
`ctm-<env>-network`, covering both regions), which creates:

* the **network resource groups** named by the entries (deduped);
* every **VNet** in `network_infrastructure.vnets` with its address space;
* every **subnet**, with the `data` role delegated to
  `Microsoft.DBforPostgreSQL/flexibleServers` and the `pe` role set to
  `privateEndpointNetworkPolicies: Disabled`;
* **one NSG per subnet**, named `<vnet>-<subnet>-nsg`, with rules derived
  from the subnet `role` — exactly the rule sets of the [NSG guidance
  below](#nsg-guidance-per-subnet), with the EM ⇄ Server dynamic-port
  tradeoff resolved in **both directions**: *all TCP from the utility address
  space into server subnets* and *all TCP from the spoke address spaces into
  the em subnet* (the latter also covers the agents' 8443 to the Automation
  API; pin ranges in Control-M and tighten later if required),
  `user_access_cidrs` as the operator sources for 443/8443 on em subnets,
  `bastion_cidrs` as the SSH 22 sources, and an explicit deny-all inbound at
  priority 4096 on every subnet; outbound stays open;
* the **bidirectional spoke ⇄ utility peerings** (`<src>-to-<dst>`, both
  directions, forwarded traffic allowed, no gateway transit). Spokes are
  never peered to each other.

When `create` is true, config loading also **cross-validates** the placement
blocks: every VNet/subnet referenced by `network.utility`,
`servers.<stage>.network`, `dr.network.utility` and `dr.server_networks.*`
must exist in the vnets list with the right subnet role, every address space
and prefix must parse, VNet spaces must not overlap (pairwise, across both
regions) and subnets must sit inside their VNet without overlapping each
other. A reference to a VNet the list does not create fails the load with the
offending config path.

### PoC address plan (shipped in the config files)

Both config files ship the full plan with `create: false`, so provisioning it
is a one-flag change. Address spaces are non-overlapping across all platforms
AND between primary and DR — a hard requirement for DR/failback peering.

| Platform / region | VNet | Role | Address space | Subnets |
|---|---|---|---|---|
| nonprod, eastus2 | `vnet-dev-utility` | utility | `10.60.0.0/22` | `snet-ctm-em` `10.60.0.0/24` (em) · `snet-ctm-postgres` `10.60.1.0/27` (data) · `snet-ctm-pe` `10.60.1.64/26` (pe) |
| nonprod, eastus2 | `vnet-ctm-dev` | spoke → `vnet-dev-utility` | `10.60.8.0/24` | `snet-ctm-server` `10.60.8.0/26` (server) · `snet-ctm-agents` `10.60.8.64/26` (agents) |
| nonprod, eastus2 | `vnet-ctm-qa` | spoke → `vnet-dev-utility` | `10.60.9.0/24` | `snet-ctm-server` `10.60.9.0/26` (server) · `snet-ctm-agents` `10.60.9.64/26` (agents) |
| prod, eastus2 | `vnet-prod-utility` | utility | `10.70.0.0/22` | `snet-ctm-em` `10.70.0.0/24` (em) · `snet-ctm-postgres` `10.70.1.0/27` (data) · `snet-ctm-pe` `10.70.1.64/26` (pe) |
| prod, eastus2 | `vnet-ctm-uat` | spoke → `vnet-prod-utility` | `10.70.8.0/24` | `snet-ctm-server` `10.70.8.0/26` (server) · `snet-ctm-agents` `10.70.8.64/26` (agents) |
| prod, eastus2 | `vnet-ctm-prod` | spoke → `vnet-prod-utility` | `10.70.9.0/24` | `snet-ctm-server` `10.70.9.0/26` (server) · `snet-ctm-agents` `10.70.9.64/26` (agents) |
| prod DR, centralus | `vnet-dr-utility` | utility | `10.80.0.0/22` | `snet-ctm-em` `10.80.0.0/24` (em) · `snet-ctm-postgres` `10.80.1.0/27` (data) · `snet-ctm-pe` `10.80.1.64/26` (pe) |
| prod DR, centralus | `vnet-ctm-prod-dr` | spoke → `vnet-dr-utility` | `10.80.9.0/24` | `snet-ctm-server` `10.80.9.0/26` (server) · `snet-ctm-agents` `10.80.9.64/26` (agents) |

Network resource groups: `rg-network-nonprod`, `rg-network-prod`,
`rg-network-prod-dr`.

### What stays bring-your-own even in Option A

* **DNS forwarding from custom DNS servers** — if any VNet uses a hub
  resolver / AD DNS, those servers must forward the platform and privatelink
  zones to Azure DNS (see [DNS](#dns)); the repo cannot configure your
  resolver.
* **On-prem / WAN connectivity** — no gateways, ExpressRoute, VPN or hub
  peering is created; the NSG rules only *admit* `user_access_cidrs` and
  `bastion_cidrs`, reaching the VNets is your transit's job.
* **The bastion / jump host itself** — `bastion_cidrs` opens SSH 22 from it;
  deploying Azure Bastion or a jump box is outside this repo.
* **Outbound internet egress** — no NAT Gateway or firewall is deployed;
  provide one if your subscription disables default outbound access.

The rest of this document is the contract Option A implements and Option B
must provide.

## Required VNets and subnets

Each platform region needs one utility VNet plus one VNet per Control-M/Server:

| VNet role | Config keys | Subnets (config key) | What lands there |
|-----------|-------------|----------------------|------------------|
| **Utility VNet** (one per platform) | `network.utility.vnet_name`, `.vnet_resource_group_name` | `em_subnet_name`, `data_subnet_name`, `pe_subnet_name` | EM VMs + internal LB frontend; PostgreSQL Flexible Server (delegated subnet); private endpoints (Key Vault, blob, files) |
| **Server VNet** (one per Control-M/Server, REQUIRED) | `servers.<stage>.network.vnet_name`, `.vnet_resource_group_name` | `server_subnet_name`, `agents_subnet_name` (optional, defaults to the server subnet) | the stage's Server VM pair `ctm-srv-<stage>-a/b`; its agent VM scale set |
| **DR utility VNet** (prod only) | `dr.network.utility.*` (same shape) | `em_subnet_name`, `data_subnet_name`, `pe_subnet_name` | failed-over EM VMs + DR LB frontend; PostgreSQL replica; DR private endpoints |
| **DR server VNet** (one per DR-protected Server) | `dr.server_networks.<stage>.*` | `server_subnet_name`, `agents_subnet_name` | the stage's failed-over Server VMs; its warm agent scale set |

A Server participates in DR **if and only if** it has a
`dr.server_networks.<stage>` entry. Servers without one get no DR VNet, no
Site Recovery items and no warm agent pool.

PoC layout shipped in the two config files:

| Platform | Utility VNet | Server VNets |
|----------|--------------|--------------|
| nonprod | `vnet-dev-utility` (rg-network-nonprod) | `vnet-ctm-dev` (CTM_DEV), `vnet-ctm-qa` (CTM_QA) |
| prod, primary | `vnet-prod-utility` (rg-network-prod) | `vnet-ctm-uat` (CTM_UAT), `vnet-ctm-prod` (CTM_PROD) |
| prod, DR | `vnet-dr-utility` (rg-network-prod-dr) | `vnet-ctm-prod-dr` only — CTM_UAT is not DR-protected (see [Cross-region](#cross-region-dr)) |

Subnet sizing and must-haves:

| Subnet role | Size | Must have |
|-------------|------|-----------|
| utility `em_subnet_name` | /27 | outbound internet (NAT Gateway, Azure Firewall or proxy) for BMC media mirrors and Azure APIs |
| utility `data_subnet_name` | /28 minimum (/27 if the DR replica shares the layout) | **delegation to `Microsoft.DBforPostgreSQL/flexibleServers`**, no other resources |
| utility `pe_subnet_name` | /27 | private endpoint network policies disabled (default) |
| server `server_subnet_name` | /28 | outbound internet as above |
| server `agents_subnet_name` | /24 minimum, /22 recommended for the prod agent pool | outbound internet as above |

Agents can be relocated to an entirely different VNet with the optional
`servers.<stage>.network.agents_vnet_name` /
`agents_vnet_resource_group_name` overrides; that VNet then needs the same
peering, flows and DNS treatment as an agents subnet (below).

## Required peering (hub-and-spoke per platform)

* **Each server VNet ⇄ its platform's utility VNet** — bidirectional VNet
  peering, REQUIRED. EM ⇄ Server control traffic, Server → database and every
  node → private endpoints all cross this peering.
* **Server VNets are never peered to each other.** That is the isolation
  guarantee of this topology: one stage has no network path to another stage's
  Server or agents; everything shared goes through the utility VNet.
* DR region, same rule: each DR server VNet ⇄ the DR utility VNet, and DR
  server VNets never peer to each other.
* If the `agents_vnet_name` override is used, the agents VNet must peer with
  **both** the utility VNet (LB, private endpoints, DNS) and its Server's VNet
  (agent ⇄ server traffic).

## Cross-VNet flows

These flows cross the server ⇄ utility peering and must be allowed end to end
(peering, NSGs, any firewall on the path):

| Flow | From → To | Ports |
|------|-----------|-------|
| EM ⇄ Server | utility em subnet ⇄ server subnet (both directions) | TCP 2369 (Configuration Agent) + Control-M dynamic ports |
| Server → database | server subnet → utility data subnet | TCP 5432 |
| Agents → Automation API (via LB) | agents subnet → utility em subnet | TCP 8443 |
| Every node → private endpoints | all em/server/agents subnets → utility pe subnet | TCP 443 (Key Vault, blob), TCP 2049 (NFS) |
| Agent ⇄ Server | agents subnet ⇄ server subnet (same VNet unless relocated) | TCP 7005-7006 |

**Control-M dynamic ports**: the CORBA naming service, GUI server and gateway
negotiate TCP ports at run time. Either keep EM ⇄ Server open on all TCP
between the em subnet and each server subnet (recommended — the peering is
point-to-point and both ends are platform-owned), or pin the ranges in
Control-M and mirror exactly those in the NSGs.

## NSG guidance per subnet

Write rules against the subnets' CIDRs (or Application Security Groups if you
manage them); "server subnets" below means the CIDRs of every
`servers.<stage>.network` server subnet on the platform. End every inbound
list with a deny-all. EM → PostgreSQL stays inside the utility VNet; it is
listed so a default-deny NSG does not break it.

### Utility em subnet (EM VMs, LB frontend)

| Priority | Direction | Source | Destination | Ports | Note |
|----------|-----------|--------|-------------|-------|------|
| 100 | Inbound | server subnets | em subnet | TCP * (or pinned ranges) | Server ⇄ EM: 2369 + dynamic ports |
| 110 | Inbound | agents subnets | em subnet | 8443 | Automation API via the LB |
| 120 | Inbound | `AzureLoadBalancer` | * | 8443 | health probes |
| 130 | Inbound | user / admin CIDRs | * | 443, 8443 | Control-M Web, Automation API, thick client |
| 140 | Inbound | Bastion / jump subnet | * | 22 | operator SSH |
| 4000 | Inbound | * | * | * | deny |
| — | Outbound | em subnet | server subnets | TCP * (or pinned) | EM → Server |
| — | Outbound | em subnet | data subnet | 5432 | EM database (intra-VNet) |
| — | Outbound | em subnet | pe subnet | 443, 2049 | Key Vault, blob, NFS |
| — | Outbound | em subnet | Internet | 443 | BMC support, Azure APIs; OS packages are baked into ctm-rhel9 |

### Server subnet (one NSG per server VNet)

| Priority | Direction | Source | Destination | Ports | Note |
|----------|-----------|--------|-------------|-------|------|
| 100 | Inbound | utility em subnet | server subnet | TCP * (or pinned) | EM ⇄ Server: 2369 + dynamic ports |
| 110 | Inbound | its agents subnet | server subnet | 7005-7006 | agent ⇄ server |
| 120 | Inbound | Bastion / jump subnet | * | 22 | operator SSH |
| 4000 | Inbound | * | * | * | deny |
| — | Outbound | server subnet | utility em subnet | TCP * (or pinned) | Server → EM |
| — | Outbound | server subnet | utility data subnet | 5432 | its PostgreSQL database |
| — | Outbound | server subnet | its agents subnet | 7005-7006 | server → agent |
| — | Outbound | server subnet | utility pe subnet | 443, 2049 | Key Vault, blob, NFS |
| — | Outbound | server subnet | Internet | 443 | as above |

### Agents subnet (one per server VNet)

| Priority | Direction | Source | Destination | Ports | Note |
|----------|-----------|--------|-------------|-------|------|
| 100 | Inbound | its server subnet | agents subnet | 7005-7006 | server → agent |
| 110 | Inbound | Bastion / jump subnet | * | 22 | operator SSH |
| 4000 | Inbound | * | * | * | deny |
| — | Outbound | agents subnet | its server subnet | 7005-7006 | agent → server |
| — | Outbound | agents subnet | utility em subnet | 8443 | Automation API via the LB |
| — | Outbound | agents subnet | utility pe subnet | 443, 2049 | Key Vault, blob, NFS |
| — | Outbound | agents subnet | Internet | 443 | plus whatever the jobs need |

Jobs that agents run may need additional outbound rules (SFTP, databases,
SaaS APIs) — those belong to the agent subnet's own NSG policy, never to a
rule that opens another stage's VNet.

### Utility data subnet (PostgreSQL, delegated)

Inbound TCP 5432 from the utility em subnet and from **every** server subnet
(each Server reaches its own database on the shared host), deny the rest.
The subnet is delegated to `Microsoft.DBforPostgreSQL/flexibleServers` and
must contain nothing else.

### Utility pe subnet (private endpoints)

Inbound TCP 443 and 2049 from the em subnet, every server subnet and every
agents subnet. Note that with private endpoint network policies disabled (the
default, and what the deployment expects) NSG rules on this subnet are not
enforced — the per-source control lives in the outbound rules above.

## DNS

* The platform creates one private DNS zone per region named
  `<platform>.ctm.internal` with **auto-registration links to every VNet of
  the platform** — the utility VNet, each server VNet, any relocated-agents
  VNet and anything listed in `network.extra_dns_vnets` (one
  registration-enabled link per VNet, link name = VNet name; Azure allows
  multiple auto-registration links on one zone). VM hostnames (`ctm-em-a`,
  `ctm-srv-prod-a`, `ctm-agent-xxxxxx`) therefore resolve across VNets
  without manual records; `ctm-web` and `ctm-db` are managed aliases.
* Private endpoints need the standard zones
  `privatelink.blob.core.windows.net`, `privatelink.file.core.windows.net`,
  `privatelink.vaultcore.azure.net` and a `<x>.postgres.database.azure.com`
  zone. Pass hub-managed zone ids through
  `network.existing_private_dns_zone_ids`; anything omitted is created in the
  platform resource group **and linked to every VNet of the platform**
  (registration off). Hub-managed zones you pass in are *not* linked by the
  deployment — make sure your hub already resolves them for these VNets.
* Use `network.extra_dns_vnets`
  (`[{vnet_name, vnet_resource_group_name}, ...]`) to link additional VNets —
  a jump/management VNet, for example — into the platform and privatelink
  zones.
* If any of the VNets use custom DNS servers (hub resolver / AD DNS), those
  servers must forward `*.ctm.internal`, the privatelink zones and
  `*.postgres.database.azure.com` to Azure DNS (`168.63.129.16`) or a DNS
  Private Resolver — otherwise the zone links above are invisible to the VMs.

## Platform-created private endpoints and DNS (inventory)

Exactly what the **foundation** phase drops into your networks (the only
things it creates network-side, in both options). Names use the prod platform
(`p01`); nonprod and DR follow the same rules (`ctm-nonprod-*`,
`ctm-prod-dr-*`, suffix per `unique_suffix`).

### Private endpoints (3 per platform region, all in the utility pe subnet)

| Private endpoint | Target resource | Group id | Private DNS zone wired via zone group |
|---|---|---|---|
| `ctm-prod-kv-pe` | Key Vault `ctm-prod-kv-p01` | `vault` | `privatelink.vaultcore.azure.net` |
| `ctm-prod-blob-pe` | Storage `ctmprodblobp01` (installers/backups) | `blob` | `privatelink.blob.core.windows.net` |
| `ctm-prod-files-pe` | Storage `ctmprodfilesp01` (NFS share) | `file` | `privatelink.file.core.windows.net` |

The DR region gets its own three (`ctm-prod-dr-kv-pe`, `ctm-prod-dr-blob-pe`,
`ctm-prod-dr-files-pe`) in the DR utility pe subnet. The PostgreSQL Flexible
Server uses VNet integration (the delegated data subnet), not a private
endpoint, so there is no fourth PE.

### Private DNS zones: created-or-linked

Per platform region, five zones are in play. A privatelink zone passed in via
`network.existing_private_dns_zone_ids` is used as-is and **not linked** (your
hub must already resolve it for these VNets); any zone *not* passed in is
created in the platform resource group and linked.

| Zone | Created when | Registration | Records managed |
|---|---|---|---|
| `prod.ctm.internal` (platform zone; same name exists in the DR region) | always | **on** (VM hostnames auto-register) | `ctm-web` A → LB frontend; `ctm-db` CNAME → PostgreSQL (DR: → replica) |
| `privatelink.blob.core.windows.net` | `blob` id not supplied | off | by the blob PE zone group |
| `privatelink.file.core.windows.net` | `file` id not supplied | off | by the files PE zone group |
| `privatelink.vaultcore.azure.net` | `vault` id not supplied | off | by the KV PE zone group |
| `ctmprodp01.postgres.database.azure.com` (nonprod `ctmnonprodn01.…`, DR `ctmproddrp01.…`) | `postgres` id not supplied | off | by the Flexible Server (VNet integration) |

### Zone-link matrix: every created zone × every platform VNet

Each zone the platform creates gets **one virtual-network link per VNet of
the region** (link name = VNet name): the utility VNet, each server VNet, any
relocated-agents VNet, plus everything in `network.extra_dns_vnets`. The
platform zone's links are registration-enabled; privatelink zone links are
not. With the shipped configs:

| Region | VNets linked into every created zone | Links per created zone |
|---|---|---|
| nonprod | `vnet-dev-utility`, `vnet-ctm-dev`, `vnet-ctm-qa` | 3 |
| prod primary | `vnet-prod-utility`, `vnet-ctm-uat`, `vnet-ctm-prod` | 3 |
| prod DR | `vnet-dr-utility`, `vnet-ctm-prod-dr` (DR-protected servers only) | 2 |

So a default prod primary region carries 5 zones × 3 VNets = 15 links (12 on
the four created privatelink/postgres zones + 3 registration-enabled links on
`prod.ctm.internal`). Hub-supplied zones reduce that: each supplied id drops
its zone's links entirely.

## Cross-region (DR)

* DR applies to the EM plus the Servers listed in `dr.server_networks` — in
  the shipped prod config that is **CTM_PROD only**. CTM_UAT is re-deployable
  and not business-critical, so it has no DR VNet, no Site Recovery items and
  no warm agent pool. (The shared PostgreSQL replica still carries every
  database, `ctm_uat` included — replication is server-level — so UAT *data*
  survives even though UAT compute is not replicated.)
* Site Recovery network mappings are created **per VNet pair**: the utility
  VNet → the DR utility VNet, and each DR-protected server VNet → its
  `dr.server_networks.<stage>` VNet. Failed-over NICs land in the matching
  subnet role (EM → DR utility em subnet; Server → its DR server subnet).
* The DR utility VNet needs the same three subnet roles as the primary utility
  VNet; each DR server VNet needs the server + agents subnets. NSG rules must
  mirror the primary, and the DR peering must mirror it too (each DR server
  VNet ⇄ the DR utility VNet only).
* Primary and DR VNets must not overlap if you ever peer them for failback
  copies; otherwise no connectivity between the regions is required
  (replication runs over Azure backbone services).
* Platform secrets, DNS aliases and hostnames are identical in both regions by
  design, so no application reconfiguration is needed on failover.

## Packer builds

The temporary build VM Packer creates (in its own throwaway resource group,
default networking) needs outbound internet to pull RHEL packages and Node.js;
it does not touch the platform VNets. If your subscription policy forbids
public IPs, set the azure-arm builder's
`virtual_network_name`/`virtual_network_subnet_name` to a build subnet with
outbound access.
