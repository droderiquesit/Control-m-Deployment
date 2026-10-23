# Network requirements (bring-your-own VNets)

The platform never creates or modifies a VNet, subnet, NSG, peering, route
table or firewall — it only **selects** them by name from
`environments/<env>/config.yaml` and creates private-DNS zone links onto them.
Everything below must exist before the deployment runs (`ctmdeploy deploy`).

Placement is per component and config-driven: the Enterprise Manager and all
shared services live in a per-platform **utility VNet**; every Control-M/Server
runs in its **own VNet** with its agent scale set beside it; the DR region
mirrors the layout with a DR utility VNet plus one DR VNet per DR-protected
Server.

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
