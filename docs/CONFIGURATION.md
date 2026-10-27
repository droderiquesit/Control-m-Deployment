# Configuration reference (`environments/<env>/config.yaml`)

Every tunable of a platform lives in one file per environment:
`environments/nonprod/config.yaml` and `environments/prod/config.yaml`.
`ctmdeploy` (`deploy/ctmdeploy/config.py`) loads, validates and derives from
it; Bicep receives only parameters computed from this file. **There are no
other variable files** — no tfvars, no parameter JSON. Change a value, review
with `python -m ctmdeploy whatif --env <env>`, deploy the phase listed in the
"Consumed by" column.

Types and defaults below are the ones enforced by
`deploy/ctmdeploy/config.py`; a missing key with a default simply takes the
default, a missing key without one fails the load with a path-qualified
error. Phases (`--phase`): `network → rg → foundation → secrets → compute →
access → dr → replication` (see `docs/SETUP.md`).

## Top level

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `subscription_id` | string | — (required) | a GUID; the placeholder `00000000-…` is rejected unless `--allow-placeholders` | the subscription every deployment and derived resource id targets | all phases |
| `environment` | string | — (required) | `nonprod`, `prod`; must match the directory name | the `ctm-<environment>` name prefix every resource name derives from | all phases |
| `location` | string | — (required) | any Azure region | primary region of the platform | `rg`, `foundation`, `compute`, `dr` |
| `unique_suffix` | string | — (required) | `^[a-z0-9]{3,6}$` (shipped: `n01` / `p01`) | the suffix in globally unique names: Key Vault `ctm-<env>-kv-<suffix>`, storage `ctm<env>blob<suffix>`, PostgreSQL `ctm-<env>-pg-<suffix>` | `foundation`, `dr` |
| `resource_lock` | bool | `false` | — | `CanNotDelete` locks on the platform resource groups | `rg` |
| `tags` | map string→string | `{}` | — | merged into the platform tags on every resource | all phases |
| `deployer_ip_cidrs` | list of CIDR strings | `[]` | — | public CIDRs allowed through the Key Vault and blob storage firewalls (CI runners, admins) | `foundation` |
| `key_vault_public_access` | bool | `true` | — | Key Vault `publicNetworkAccess` (still firewall-restricted to `deployer_ip_cidrs`); set `false` when seeding runs from inside the VNets | `foundation` |
| `ctm_version` | string | `"9.0.21.300"` | — | the media cache directory on the NFS share (`installers/<component>/<version>`) used by the node installer; does **not** change blob names — see `installer_blobs` | `compute` (cloud-init render / node.json) |
| `installer_blobs` | map | `{}` (= all defaults below) | keys limited to the six below (v4.1: the old `agent_installer`/`agent_silent` keys are rejected — the agent is baked into the ctm-agent image) | blob names (inside the `installers` container) the EM/Server nodes download at first boot; also what `ctmdeploy media upload` checks for completeness | `compute` (cloud-init / node.json), `media upload` |

`installer_blobs` defaults (override any subset):

| Key | Default blob name |
|-----|-------------------|
| `em_installer` | `em/DREM.9.0.21.300_Linux-x86_64.tar.Z` |
| `server_installer` | `server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z` |
| `em_silent_primary` | `em/silent-em-primary.xml` |
| `em_silent_secondary` | `em/silent-em-secondary.xml` |
| `server_silent_primary` | `server/silent-server-primary.xml` |
| `server_silent_secondary` | `server/silent-server-secondary.xml` |

## `network:` — where the platform deploys (always required)

The platform **selects** these networks; it never creates them (that is
`network_infrastructure:`, below). The utility VNet hosts the EM tier, LB,
database and private endpoints; every Control-M/Server names its own VNet
under `servers.<stage>.network`.

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `network.utility.vnet_name` | string | — (required) | an existing VNet | the utility VNet (EM, LB, PostgreSQL, private endpoints) | `foundation`, `compute` |
| `network.utility.vnet_resource_group_name` | string | — (required) | — | resource group of that VNet | `foundation`, `compute` |
| `network.utility.em_subnet_name` | string | — (required) | — | subnet for EM VMs + the internal LB frontend | `foundation`, `compute` |
| `network.utility.data_subnet_name` | string | — (required) | subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers` | subnet for the PostgreSQL Flexible Server | `foundation` |
| `network.utility.pe_subnet_name` | string | `""` | — | subnet for the Key Vault/blob/files private endpoints; empty falls back to the em subnet | `foundation` |
| `network.existing_private_dns_zone_ids` | map | `{}` | keys: `blob`, `file`, `vault`, `postgres` | hub-managed private DNS zone ids; any key omitted (or empty) makes the deployment create that zone and link it to every platform VNet | `foundation` |
| `network.extra_dns_vnets` | list of `{vnet_name, vnet_resource_group_name}` | `[]` | — | extra VNets (e.g. a jump VNet) linked into the platform and created privatelink DNS zones | `foundation` |

## `network_infrastructure:` — optional full network ("Option A")

With `create: false` (the default, bring-your-own) this block documents the
address plan — only its shape is validated — **and** still feeds the access
layer its subnet ids (the bastion/appgw/func subnets are resolved by *role*
from the utility VNet's entry, created or not). Flipping `create: true` makes
`ctmdeploy deploy --env <env> --phase network` provision everything in
`vnets` by deploying **`network/main.bicep`** — the standalone folder the
cloud team can deploy with the az CLI alone (`network/README.md`; keep
`network/params/<env>.bicepparam` in sync with this block) — and turns on
cross-validation: every VNet/subnet the placement blocks reference
(`network.utility`, every `servers.<stage>.network`, `dr.network.utility`,
`dr.server_networks.*`, `build_agents.network`) must resolve to a declared
entry with the right subnet role, address spaces must parse and not overlap
(across VNets and inside each VNet), and peerings must follow the role rules
below. See `docs/NETWORK-REQUIREMENTS.md` for the NSG rule sets each subnet
role derives.

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `create` | bool | `false` | — | `true` = the `network` phase deploys `network/main.bicep`; `false` = phase skipped, block stays the documented plan (+ access-layer subnet resolution) | `network`, `access` (subnet ids) |
| `user_access_cidrs` | list of CIDR strings | `[]` | valid CIDRs | NSG allow rule on **em** and **appgw** subnets: TCP 443, 8443 from these sources. Prod must keep **both** appgw subnet prefixes (primary + DR twin) — each gateway's backend connections to both regions' internal LBs ride this rule | `network` |
| `bastion_cidrs` | list of CIDR strings | `[]` | valid CIDRs | NSG allow rule on em/server/agents/build subnets: TCP 22. Keep the `AzureBastionSubnet` prefixes — Bastion sessions egress from them | `network` |
| `vnets[].name` | string | — (required) | unique across the list | VNet name (must match the placement blocks when `create: true`) | `network` |
| `vnets[].resource_group_name` | string | — (required) | — | resource group the VNet lands in (created, deduped across entries) | `network` |
| `vnets[].location` | string | — (required) | any Azure region | region of the VNet (and of its resource group, taken from the first VNet naming it) — DR VNets carry the DR region | `network` |
| `vnets[].role` | string | — (required) | `utility`, `spoke`, `build`, `external` | `utility` hosts em/data/pe (+ bastion/appgw/func) subnets; `spoke` hosts a server/agents pair; `build` hosts the ADO build agents; `external` is **resolution-only** — the name/resource group/address space of a VNet ANOTHER plan deploys (no subnets, no peer_to of its own; nothing is created), so cross-environment peerings resolve — the prod config declares the nonprod `vnet-dev-utility` this way for the build-VNet peering (same semantics as `network/params/*.bicepparam`) | `network` |
| `vnets[].peer_to` | string **or list** | `""` | spokes: exactly ONE declared `utility` VNet; utility: other `utility` VNets only (the prod ⇄ DR **global** pairing); build: one or more utility VNets — names from the *other* environment's plan are tolerated (cross-environment peering; `network/main.bicep` skips unresolved names and reports them in `unresolvedPeerings`) | bidirectional peerings (`<src>-to-<dst>`, both directions, declared on one side only) | `network` |
| `vnets[].address_space` | string | — (required) | valid CIDR, non-overlapping with every other entry | the VNet address space; also the source prefix in the derived NSG rules | `network` |
| `vnets[].subnets[].name` | string | — (required) | unique inside the VNet; a role-`bastion` subnet must be named exactly `AzureBastionSubnet` (validated at load) | subnet name | `network` |
| `vnets[].subnets[].prefix` | string | — (required) | valid CIDR inside the VNet, non-overlapping with sibling subnets | subnet prefix | `network` |
| `vnets[].subnets[].role` | string | — (required) | `em`, `data`, `pe`, `server`, `agents`, `bastion`, `appgw`, `func`, `build` | selects the NSG rule set; `data` gets the PostgreSQL delegation, `func` the `Microsoft.Web/serverFarms` delegation, `pe` gets `privateEndpointNetworkPolicies: Disabled`, `bastion` gets **no NSG** | `network`; `bastion`/`appgw`/`func` also resolve the `access` phase's subnet ids |

The shipped config files carry the complete PoC address plan with
`create: false`, so provisioning the PoC network is a one-flag change (see
the address plan table in `docs/NETWORK-REQUIREMENTS.md`).

## `access:` — Bastion and the private Application Gateway

Deployed by `ctmdeploy deploy --env <env> --phase access`
(`bicep/access.bicep`), after `compute` and before `dr`.

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `bastion.enabled` | bool | `false` | — | Azure Bastion Standard `ctm-<env>-bastion` (+ its required static public IP) in the utility VNet's `AzureBastionSubnet`; private SSH to every node and build agent, DR included via the global peering | `access` |
| `appgw.enabled` | bool | `false` | parsed everywhere, **honored on prod only** | the PRIVATE Application Gateway Standard_v2 `ctm-<env>-appgw` in `snet-ctm-appgw`: layer-4 TCP 443/8443 listeners, one backend pool holding the **primary and DR internal LB frontend IPs** (healthy-only routing = automatic primary/DR), plus the `ctm-global` A record in the platform DNS zone | `access` |
| `appgw.dr_instance` | bool | `false` | only meaningful when `dr.enabled` **and** `appgw.enabled` (inert otherwise; nonprod untouched). Validated at load: requires an `appgw`-role subnet in the DR utility VNet's `network_infrastructure` entry | a **DR TWIN** of the gateway (`ctm-<env>-dr-appgw`) in the DR utility VNet's `snet-ctm-appgw` — its own private frontend, the **same** two-LB backend pool — plus the `ctm-global` A record in the **DR copy** of the platform zone pointing at the twin, so the private entry point survives a primary-region loss (`ctmdeploy failover` re-asserts the record; the primary zone's record keeps pointing at the primary gateway) | `access`, `failover` |

## `kv_sync:` — Key Vault → Control-M secret sync

The Durable Function (`functions/kvsync/`) that mirrors Key Vault secrets
tagged `ctm-sync=true` into Control-M through the EM Automation API. The
`access` phase provisions the infrastructure (EP1 plan, `<name>-kvsync`
Function App, system-assigned identity with **Key Vault Secrets User**, VNet
integration into `snet-ctm-func`); `ctmdeploy kvsync deploy --env <env>`
pushes the code. Tagging convention and runtime details:
`functions/kvsync/README.md`, `docs/KEYVAULT.md`.

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `enabled` | bool | `false` | — | deploy the Function App infrastructure (and let `kvsync deploy` run). COST NOTE: EP1 keeps a warm instance billed around the clock; nonprod may set `false` | `access`, `kvsync deploy` |
| `schedule` | string | `"0 */15 * * * *"` | a **6-field** NCRONTAB expression (seconds first; validated at load) | the `SYNC_SCHEDULE` app setting — how often the sync orchestration runs | `access` |

## `build_agents:` — Azure DevOps scale-set build agents (prod config)

A uniform VMSS `ctm-build-agents` (Ubuntu 22.04, Manual upgrade, **no
autoscale** — the ADO elastic pool owns capacity) in its own resource group,
deployed by the `access` phase. Its VNet peers to the nonprod, prod and DR
utility VNets so one pool deploys into all three platforms. Post-step
(documented, never granted by templates): register the VMSS as an ADO
elastic pool; ADO's service principal needs Virtual Machine Contributor on
the resource group (`docs/AZURE-DEVOPS.md` §6, `docs/IDENTITY-PERMISSIONS.md`).

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `enabled` | bool | `false` | — | deploy the build-agent resource group + VMSS + its dedicated user-assigned identity `ctm-build-id` | `access` |
| `resource_group` | string | `ctm-build-rg` | — | the dedicated resource group (created by the access phase) | `access` |
| `vm_size` | string | `Standard_D4s_v5` | — | agent VM size | `access` |
| `instances` | int | `2` | ≥ 1 when enabled | baseline capacity sent on deploys; the ADO elastic pool scales from there | `access` |
| `network.vnet_name` | string | — (required when enabled) | the build VNet (`vnet-ctm-build`) | where the scale set lands | `access` |
| `network.vnet_resource_group_name` | string | — (required when enabled) | — | resource group of that VNet | `access` |
| `network.subnet_name` | string | — (required when enabled) | — | the build subnet (`snet-ctm-build`) | `access` |

## `image:` — golden images the VMs/VMSS boot from (v4.1 two-image model)

Nested shape: `base` is the **OS-only** `ctm-rhel9` image (EM/Server VMs —
BMC software is never baked into it) and `agent` is the `ctm-agent` image
with the **Control-M/Agent baked in, unconfigured** (the agent scale sets;
boot only configures/starts/registers it). See `packer/README.md`. The
pre-v4.1 flat keys `image_definition`/`image_version` are **rejected** with
a pointer to this shape.

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `gallery_resource_group` | string | `ctm-images-rg` | — | resource group of the Compute Gallery | `compute`, `image gallery` |
| `gallery_name` | string | `ctmgallery` | — | Compute Gallery name | `compute`, `image gallery` |
| `base.definition` | string | `ctm-rhel9` | — | gallery image definition of the EM/Server (OS-only) image | `compute` (`imageId`), `image gallery/build` |
| `base.version` | string | `latest` | `latest` or a published semver such as `"1.0.0"` | the exact base image version resolved into the VM `imageReference`; pin in prod | `compute` |
| `agent.definition` | string | `ctm-agent` | — | gallery image definition of the agent image (Control-M/Agent baked in) | `compute` (`agentImageId`), `image gallery/build` |
| `agent.version` | string | `latest` | `latest` or a published semver | the agent VMSS image version — bumping it + a `compute` deploy rolls the VMSS model (new instances boot it; recycle old ones to converge) | `compute` |

## `em:` — Enterprise Manager tier

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `vm_size` | string | — (required) | any VM size | EM VM size (`ctm-em-a`/`ctm-em-b`) | `compute` |
| `data_disk_gb` | int | `256` | — | persistent `/ctm` data disk size per EM VM | `compute` |
| `disk_type` | string | `Premium_LRS` | `Premium_LRS`, `Premium_ZRS` | OS + data disk SKU (ZRS disks survive a zone loss) | `compute` |
| `enable_secondary` | bool | `true` | — | deploy `ctm-em-b` (EM HA pair) or a single EM node | `compute` |
| `zones` | list of strings | `["1", "2"]` | zone numbers | zones of the primary / secondary EM VM | `compute` |
| `web_port` | int | `8443` | — | Control-M Web / Automation API port (LB backend port and probe) | `foundation` (LB), `compute` (cloud-init) |
| `os_user` | string | `emuser` | — | EM installation OS account | `compute` (cloud-init) |
| `db_name` | string | `emdb` | — | EM database name on the shared PostgreSQL host | `foundation` (DB create), `compute` (cloud-init) |
| `db_user` | string | `emuser` | — | EM database role | `compute` (cloud-init) |

## `servers.<stage>:` — one Control-M/Server per entry

The map key (`dev`, `qa`, `uat`, `prod`, …) is the *stage key*: it derives the
database `ctm_<stage>`, the Key Vault secret `ctm-db-password-<stage>`, the VM
names `ctm-srv-<stage>-a/b`, the agent pool `ctm-<env>-agents-<stage>` and the
default host group `<stage>-agents`. Adding a stage = adding an entry
(plus its VNet).

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `name` | string | `CTM_<STAGE>` | — | the logical Control-M/Server name registered in EM | `compute` (cloud-init, tags) |
| `vm_size` | string | — (required) | — | Server VM size | `compute` |
| `data_disk_gb` | int | `256` | — | persistent data disk per Server VM | `compute` |
| `disk_type` | string | `Premium_LRS` | `Premium_LRS`, `Premium_ZRS` | disk SKU | `compute` |
| `enable_secondary` | bool | `true` | — | deploy `ctm-srv-<stage>-b` (Server HA pair) | `compute` |
| `zones` | list of strings | `["1", "2"]` | zone numbers | zones of the primary / secondary Server VM | `compute` |
| `os_user` | string | `controlm` | — | Server installation OS account | `compute` (cloud-init) |
| `network.vnet_name` | string | — (required) | an existing VNet | this Server's own VNet (isolation boundary) | `foundation` (DNS links), `compute` |
| `network.vnet_resource_group_name` | string | — (required) | — | resource group of that VNet | `foundation`, `compute` |
| `network.server_subnet_name` | string | — (required) | — | subnet of the Server VM pair | `compute` |
| `network.agents_subnet_name` | string | `""` | — | subnet of the agent VMSS; empty = the server subnet | `compute` |
| `network.agents_vnet_name` | string | `""` | — | relocate the agents to another VNet entirely (needs the peering/DNS treatment in `docs/NETWORK-REQUIREMENTS.md`) | `foundation`, `compute` |
| `network.agents_vnet_resource_group_name` | string | `""` | requires `agents_vnet_name` | resource group of the relocated agents VNet | `foundation`, `compute` |
| `agents.vm_size` | string | — (required) | — | agent instance size | `compute` |
| `agents.min` | int | — (required) | `min <= default <= max` | autoscale floor | `compute` |
| `agents.max` | int | — (required) | — | autoscale ceiling | `compute` |
| `agents.default` | int | — (required) | — | initial/default instance count (autoscale owns it afterwards) | `compute` |
| `agents.zones` | list of strings | `["1", "2", "3"]` | zone numbers | zones the scale set balances across | `compute` |
| `agents.use_spot` | bool | `false` | — | run agents as Spot VMs (evictable; they deregister gracefully) | `compute` |
| `agents.hostgroup` | string | `""` → `<stage>-agents` | — | Control-M host group the agents join | `compute` (cloud-init) |
| `agents.provision_image` | string | `Agent.Linux` | — | carried in node.json for compatibility; v4.1 bakes the Agent into the ctm-agent golden image — nothing is provisioned or installed at boot | `compute` (node.json) |
| `agents.agent_port` | int | `7006` | — | agent listener (server → agent) port — written into the baked agent's `CONFIG.dat` at boot by the node installer (the image itself is built with the `silent-agent.xml` placeholder contract) | `compute` (node.json / CONFIG.dat) |
| `agents.os_user` | string | `ctmagent` | — | agent OS account | `compute` (cloud-init) |
| `agents.scale_out_cpu` | int | `70` | percent | average CPU (5 min) that adds an instance | `compute` (autoscale) |
| `agents.scale_in_cpu` | int | `25` | percent | average CPU (15 min) that removes an instance | `compute` (autoscale) |

## `database:` — PostgreSQL Flexible Server (one host per platform)

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `sku_name` | string | — (required) | `B_`/`GP_`/`MO_`-prefixed, e.g. `GP_Standard_D8ds_v5` | server compute tier + size | `foundation` |
| `storage_mb` | int | `131072` | multiples of 1024 | provisioned storage (auto-grow stays on) | `foundation` |
| `postgres_version` | string | `"16"` | a version supported by your Control-M release | PostgreSQL major version | `foundation` |
| `high_availability_mode` | string | `ZoneRedundant` | `Disabled`, `SameZone`, `ZoneRedundant` | standby replica mode | `foundation` |
| `backup_retention_days` | int | `14` | 7–35 | automated backup retention | `foundation` |
| `geo_redundant_backup` | bool | `false` | — | replicate automated backups to the paired region | `foundation` |
| `admin_user` | string | `ctmadmin` | — | administrator login (password = Key Vault `db-admin-password`) | `foundation`, `compute` (cloud-init) |
| `port` | int | `5432` | — | port written into the silent-install XMLs | `compute` (cloud-init) |

## `storage:` — blob (installers/backups) + Premium Files NFS

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `blob_replication_type` | string | `RAGZRS` | `LRS`, `ZRS`, `GRS`, `RAGRS`, `GZRS`, `RAGZRS` | blob account redundancy | `foundation` |
| `files_replication_type` | string | `ZRS` | `LRS`, `ZRS` | Premium Files redundancy (ZRS needs a supported region) | `foundation` |
| `shared_share_quota_gb` | int | `256` | — | size of the `ctm-shared` NFS 4.1 share | `foundation` |

## `load_balancer:` — Control-M Web entry point

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `internal` | bool | `true` | — | private zone-redundant frontend in the em subnet vs a public Standard LB with a static public IP | `foundation` |
| `public_dns_label` | string | `""` | — | DNS label on the public IP (public mode only; used by Traffic Manager) | `foundation` |

(The LB web port is `em.web_port`; the orchestrator injects it.)

## `backup:` — Recovery Services vault (VM + file share)

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `enabled` | bool | `true` | — | deploy the vault + policies + protected items (always off in the DR region) | `compute` |
| `storage_mode_type` | string | `GeoRedundant` | `LocallyRedundant`, `ZoneRedundant`, `GeoRedundant` | vault redundancy | `compute` |
| `cross_region_restore` | bool | `false` | GeoRedundant only | restore VM backups into the paired region | `compute` |
| `daily_retention_days` | int | `30` | — | daily recovery point retention | `compute` |
| `weekly_retention_weeks` | int | `12` | — | Sunday-weekly retention | `compute` |
| `include_files_share` | bool | `true` | — | also protect the `ctm-shared` NFS share | `compute` |

## `observability:` — Log Analytics + alerts

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `log_retention_days` | int | `30` | — | Log Analytics retention | `foundation` |
| `alert_emails` | list of strings | `[]` | email addresses | action group recipients for every platform alert | `foundation` |

## `dr:` — disaster recovery (prod platform)

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `enabled` | bool | `false` | — | the whole DR footprint: DR RG, DR foundation, DR compute (warm pools), the `dr` and `replication` phases, DR Key Vault seeding, media upload to the DR container | `rg`, `foundation`, `secrets`, `compute`, `dr`, `replication`, `media upload` |
| `location` | string | `""` | required when enabled | DR region | `rg`, `foundation`, `compute`, `dr` |
| `network` | mapping | — | required when enabled; same shape as `network:` (utility + DNS keys) | the DR utility VNet (failed-over EM target, replica data subnet, DR PEs, DR LB) | `foundation`, `dr`, `replication` |
| `server_networks.<stage>` | mapping | `{}` | non-empty subset of the `servers` keys when enabled; same shape as `servers.<stage>.network` | **the DR opt-in set**: a Server is DR-protected iff it has an entry here (DR VNet, ASR items, warm agents); shipped prod config: `prod` only | `foundation`, `compute`, `replication` |
| `warm_agents` | int | `0` | — | default instance count of each DR-protected stage's warm agent pool (min 0, max unchanged) | `compute` (DR) |
| `replica_sku_name` | string | `""` → `database.sku_name` | `B_`/`GP_`/`MO_`-prefixed | SKU of the cross-region PostgreSQL read replica (keep ≥ primary) | `dr` |
| `enable_site_recovery` | bool | `true` | — | ASR vault + cache account (`dr` phase) and the fabric/mapping/protected-item enablement (`replication` phase) | `dr`, `replication` |
| `traffic_manager` | bool | `false` | needs public LBs on both sides | Traffic Manager priority-failover profile for a public web endpoint | `dr` |

## `media:` — installer media source (Azure Artifacts)

The BMC EPD archives + silent-install XMLs live as **Universal Packages** in
an Azure Artifacts feed, never in git and never in the image. The pipeline
(`publishMedia=true`) downloads the four pinned packages and runs
`ctmdeploy media upload`, which pushes them into the `installers` blob
container of each platform (primary + DR) alongside the node installer
script. This block is validated at load time and documents the pins; the
pipeline reads its copy of the same pins from
`pipelines/variables/common.yml` — **keep the two in sync** (setup and
layout rules: `docs/AZURE-DEVOPS.md` §5).

| Key | Type | Default | Allowed values | What it changes | Consumed by |
|-----|------|---------|----------------|-----------------|-------------|
| `azure_artifacts` | mapping or absent | absent | — | absent = no feed declared (manual blob upload only) | config validation |
| `azure_artifacts.organization` | string | — (required when present) | `https://dev.azure.com/<org>` | the Azure DevOps organization of the feed | docs/pipeline mirror |
| `azure_artifacts.project` | string | — (required) | — | the project holding the feed | docs/pipeline mirror |
| `azure_artifacts.feed` | string | — (required) | — | feed name (shipped: `ctm-media`) | docs/pipeline mirror |
| `azure_artifacts.packages.em` | `{name, version}` | — (required) | exactly the keys `em`, `server`, `agent`, `silent_xml` | universal package holding the EM EPD archive | docs/pipeline mirror |
| `azure_artifacts.packages.server` | `{name, version}` | — (required) | — | package holding the Server EPD archive | docs/pipeline mirror |
| `azure_artifacts.packages.agent` | `{name, version}` | — (required) | — | package holding the Agent EPD archive — consumed by the pipeline IMAGE stage (`imageKind=agent` → `ctmdeploy image build --image agent`), never uploaded to blob | docs/pipeline mirror |
| `azure_artifacts.packages.silent_xml` | `{name, version}` | — (required) | — | package holding the five silent-install XMLs: the EM/Server pairs go to blob, `silent-agent.xml` feeds the Packer agent-image build | docs/pipeline mirror |

## Derived names (not configurable)

Everything else is computed from `environment` + `unique_suffix` and the
stage keys; `docs/RESOURCES.md` lists every resource with its concrete name.
The fixed constants (defined in `deploy/ctmdeploy/config.py`): EM hostnames
`ctm-em-a`/`ctm-em-b`, Server hostnames `ctm-srv-<stage>-a/b`, data mount
`/ctm`, Automation API user `emuser`, containers `installers`/`backups`, NFS
share `ctm-shared`, platform DNS zone `<environment>.ctm.internal` with the
`ctm-web` and `ctm-db` aliases (plus `ctm-global` → the Application Gateway
on prod). v4 additions: the node installer blob
`scripts/ctm_node_install.py` (uploaded by the compute phase and `media
upload`), the kvsync Function App `ctm-<env>-kvsync` (plan
`ctm-<env>-kvsync-plan`, storage `<compact>func<hash>`), the EM Automation
API base `https://ctm-web.<env>.ctm.internal:<web_port>/automation-api`
(`CTM_API_BASE`), Bastion `ctm-<env>-bastion`, gateway `ctm-<env>-appgw`
(DR twin `ctm-<env>-dr-appgw` when `access.appgw.dr_instance`),
build scale set `ctm-build-agents` with identity `ctm-build-id`.
