# network/ — Control-M platform network (cloud-team owned)

Self-contained Bicep for every network the Control-M platform references:
resource groups, VNets, subnets (one NSG per subnet), delegations and all
VNet peerings — including the cross-region (global) utility pairing and the
build-agent VNet. It needs **nothing but the az CLI**: peer address spaces
are resolved inside `main.bicep` from the `vnets` parameter itself, so there
is no python/orchestrator dependency.

The same plan exists in two places, deliberately:

| Who deploys | Input | Command |
| --- | --- | --- |
| Cloud team (this folder) | `params/<env>.bicepparam` | `az deployment sub create ...` (below) |
| Platform pipeline | `environments/<env>/config.yaml` → `network_infrastructure` (when `create: true`) | `python -m ctmdeploy deploy --env <env> --phase network` |

Keep `params/<env>.bicepparam` and the `network_infrastructure` block of
`environments/<env>/config.yaml` in sync — they describe the same network.
(One deliberate difference: `params/prod.bicepparam` additionally declares
the nonprod utility VNet as a `role: external` resolution-only entry, so the
cross-plan build ↔ vnet-dev-utility peering is emitted; the config, which
only holds one environment, cannot — `main.bicep` skips peer names it cannot
resolve and reports them in its `unresolvedPeerings` output.)
Bring-your-own networking stays the platform default (`create: false`); when
the cloud team owns the network, deploy from this folder and leave the flag
off.

## Quickstart

```bash
# 1. NONPROD first (the prod plan peers the build VNet into vnet-dev-utility,
#    so the nonprod utility VNet must already exist):
az deployment sub create -l eastus2 \
  -f network/main.bicep -p network/params/nonprod.bicepparam

# 2. PROD + DR + build VNet (one deployment, both regions):
az deployment sub create -l eastus2 \
  -f network/main.bicep -p network/params/prod.bicepparam

# Preview a change first:
az deployment sub what-if -l eastus2 \
  -f network/main.bicep -p network/params/prod.bicepparam
```

Deployments are idempotent (deterministic names only): re-running converges,
it never duplicates. Required permissions: create resource groups at
subscription scope plus full `Microsoft.Network` on them — the built-in
**Network Contributor** role on the subscription (or on the `rg-network-*`
groups once they exist) plus resource-group create; see
`docs/IDENTITY-PERMISSIONS.md`. The platform's own deployer identity
deliberately CANNOT write VNets/subnets/NSGs/peerings — the network is yours.

## What it creates

Per `vnets` entry (except `role: external`, see below):

- the **resource group** (deduped; region of the first VNet naming it),
- the **VNet** with its address space,
- one **NSG per subnet**, named `<vnet>-<subnet>-nsg`, with role-derived
  rules (table below) — except the bastion subnet, which carries no NSG
  (Azure Bastion manages its own data plane),
- subnet **delegations**: `data` → `Microsoft.DBforPostgreSQL/flexibleServers`,
  `func` → `Microsoft.Web/serverFarms`; `pe` subnets get
  `privateEndpointNetworkPolicies: Disabled`,
- **bidirectional peering** for every name in `peer_to` (declare a peering on
  ONE side only; both directions are emitted automatically, each into its
  VNet's resource group).

### Address plan (PoC)

**nonprod** (`params/nonprod.bicepparam`), all eastus2, `rg-network-nonprod`:

| VNet | Role | Space | Subnets |
| --- | --- | --- | --- |
| vnet-dev-utility | utility | 10.60.0.0/22 | snet-ctm-em 10.60.0.0/24 (em) · snet-ctm-postgres 10.60.1.0/27 (data) · snet-ctm-pe 10.60.1.64/26 (pe) · AzureBastionSubnet 10.60.2.0/26 (bastion) · snet-ctm-func 10.60.2.64/26 (func) |
| vnet-ctm-dev | spoke → vnet-dev-utility | 10.60.8.0/24 | snet-ctm-server 10.60.8.0/26 (server) · snet-ctm-agents 10.60.8.64/26 (agents) |
| vnet-ctm-qa | spoke → vnet-dev-utility | 10.60.9.0/24 | snet-ctm-server 10.60.9.0/26 (server) · snet-ctm-agents 10.60.9.64/26 (agents) |

**prod** (`params/prod.bicepparam`), eastus2 + centralus (DR):

| VNet | RG / region | Role | Space | Subnets |
| --- | --- | --- | --- | --- |
| vnet-prod-utility | rg-network-prod / eastus2 | utility, peer_to [vnet-dr-utility] | 10.70.0.0/22 | em 10.70.0.0/24 · data 10.70.1.0/27 · pe 10.70.1.64/26 · AzureBastionSubnet 10.70.2.0/26 · snet-ctm-appgw 10.70.2.64/26 (appgw) · snet-ctm-func 10.70.2.128/26 (func) |
| vnet-ctm-uat | rg-network-prod / eastus2 | spoke → vnet-prod-utility | 10.70.8.0/24 | server 10.70.8.0/26 · agents 10.70.8.64/26 |
| vnet-ctm-prod | rg-network-prod / eastus2 | spoke → vnet-prod-utility | 10.70.9.0/24 | server 10.70.9.0/26 · agents 10.70.9.64/26 |
| vnet-dr-utility | rg-network-prod-dr / centralus | utility | 10.80.0.0/22 | em 10.80.0.0/24 · data 10.80.1.0/27 · pe 10.80.1.64/26 |
| vnet-ctm-prod-dr | rg-network-prod-dr / centralus | spoke → vnet-dr-utility | 10.80.9.0/24 | server 10.80.9.0/26 · agents 10.80.9.64/26 |
| vnet-ctm-build | rg-network-prod / eastus2 | build, peer_to [vnet-dev-utility, vnet-prod-utility, vnet-dr-utility] | 10.90.0.0/24 | snet-ctm-build 10.90.0.0/26 (build) |
| vnet-dev-utility | rg-network-nonprod / eastus2 | **external** | 10.60.0.0/22 | — (created by the nonprod plan) |

Address spaces are non-overlapping across all platforms and regions —
required for the DR/failback and build peerings.

### Peerings emitted

- spoke ↔ its utility VNet, per region (spokes are **never** peered to each
  other — that isolation is the point of the topology).
- vnet-prod-utility ↔ vnet-dr-utility (**global**): lets the Application
  Gateway reach the DR internal LB (8443) and Azure Bastion reach the DR
  utility VNet.
- vnet-ctm-build ↔ all three utility VNets, so Azure DevOps scale-set build
  agents deploy into nonprod, prod AND DR. The build ↔ vnet-dev-utility pair
  crosses the two plans: the prod deployment emits BOTH directions, including
  the one that lands in `rg-network-nonprod` (that is what the `external`
  entry is for — deploy nonprod first).

### `role: external` entries

An external entry creates nothing; it only declares the name, resource group
and address space of a VNet **another plan deploys**, so `peer_to` can
reference it and its address space can feed NSG rules. The remote-side
peering is still written into its resource group by this deployment.

A `peer_to` name with **no** matching entry (not even an external one) is
skipped — no peering is emitted and its address space feeds no NSG rule —
and listed in the deployment's `unresolvedPeerings` output. Check that output
is empty after every deployment; if it names a VNet, add an external entry
for it and re-deploy.

## Subnet role → what the platform places there

| Role | NSG inbound (all end with deny-all at 4096; outbound open) | What lands there (deployed by the platform pipeline, not this folder) | config.yaml reference |
| --- | --- | --- | --- |
| em | 443/8443 from `userAccessCidrs`; all TCP from spoke spaces (Server→EM dynamic CORBA ports — documented tradeoff); LB probes; 22 from `bastionCidrs`; intra-VNet | EM VMs + internal LB frontend (primary and DR regions); appgw backend target | `network.utility.em_subnet_name`, `dr.network.utility` |
| data | 5432 from utility + every spoke | PostgreSQL Flexible Server (delegated) | `network.utility.data_subnet_name` |
| pe | 443/2049 from utility + every spoke | Private endpoints: Key Vault, blob, files | `network.utility.pe_subnet_name` |
| server | all TCP from the peered utility space; 7005-7006 + intra from own VNet; 22 from `bastionCidrs` | Control-M/Server VM(s) | `servers.*.network.server_subnet_name` |
| agents | 7005-7006 + intra from own VNet; 22 from `bastionCidrs` | Control-M agent VM scale set | `servers.*.network.agents_subnet_name` |
| bastion | NO NSG; subnet is force-named `AzureBastionSubnet` (/26 minimum) | Azure Bastion host (`access.bastion.enabled`) | — |
| appgw | 65200-65535 from GatewayManager; 443/8443 from `userAccessCidrs` + VirtualNetwork; LB probes | Application Gateway Standard_v2, private-only frontend (`ctm-global`), balancing the primary and DR internal LBs (`access.appgw.enabled`, prod) | — |
| func | intra-VNet only (the subnet just originates traffic) | Key Vault → Control-M sync Function App VNet integration (`kv_sync.enabled`); delegated to Microsoft.Web/serverFarms | — |
| build | 22 from `bastionCidrs` + the peered utility spaces only | Azure DevOps scale-set build agents VMSS (`build_agents`, prod config) | `build_agents.network` |

### The two CIDR parameters — read before editing

- `bastionCidrs` MUST keep the `AzureBastionSubnet` prefixes
  (nonprod `10.60.2.0/26`, prod `10.70.2.0/26`): Bastion sessions egress from
  that subnet, and the agents/build subnets allow SSH **only** from these
  CIDRs (the prod prefix also covers Bastion → DR, over the global peering).
  Append jump-host/operator CIDRs as needed.
- `userAccessCidrs` in prod MUST keep the appgw subnet prefix
  (`10.70.2.64/26`): the gateway's backend connections to the EM internal
  LBs of BOTH regions ride the em-subnet 443/8443 rule. Append the operator
  networks that should reach Control-M Web / Automation API (directly or via
  the gateway frontend).

## What stays yours (not created here, not created by the platform)

- On-prem/WAN connectivity: ExpressRoute/VPN gateways, their subnets, routes.
- DNS forwarding from corporate DNS into the platform's private DNS zones
  (the zones themselves and their VNet links are created by the platform's
  foundation phase).
- Any firewall/NVA and user-defined routes (`allowForwardedTraffic` is
  already enabled on every peering, so an NVA in a utility VNet keeps
  working).
- Tightening beyond the shipped NSGs (e.g. pinning the Control-M CORBA port
  ranges to narrow the all-TCP utility↔server rules — see
  `docs/NETWORK-REQUIREMENTS.md`).

What the platform pipeline deploys INTO these subnets (VMs, LBs, Bastion
host, Application Gateway, Function App, private endpoints, DNS zones) is
inventoried in `docs/RESOURCES.md`; the full flow/port matrix is in
`docs/NETWORK-REQUIREMENTS.md`.
