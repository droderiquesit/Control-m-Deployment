# Deployed resources (full inventory)

Everything the toolchain creates, by phase, derived from the Bicep templates
and the python data-plane steps. **Name** shows the concrete prod name
(environment `prod`, `unique_suffix: p01`) where it is derivable; nonprod and
DR names follow the same rules with `ctm-nonprod`/`n01` and the `ctm-prod-dr`
prefix. The three count columns are what the **shipped config files** produce
(`environments/nonprod/config.yaml` / `environments/prod/config.yaml`):
nonprod (no DR), prod primary region, prod DR region.

Resources marked **data-plane, python** are not ARM template resources: they
are created by `ctmdeploy` through SDK / REST data-plane calls and will not
appear in `whatif` output or deployment history.

Deployment names: `ctm-<env>-<phase>[-dr]` (e.g. `ctm-prod-foundation-dr`);
the optional network phase is `ctm-<env>-network`.

## Phase `network` — optional full network (`bicep/network.bicep`)

Runs **only** when `network_infrastructure.create: true` (default is
bring-your-own, where this phase is skipped and nothing below exists). One
subscription-scope deployment covers both regions. Counts assume the shipped
PoC vnets list with the flag flipped on.

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Resource group | `rg-network-prod` (nonprod: `rg-network-nonprod`; DR: `rg-network-prod-dr`) | subscription | deduped from `vnets[].resource_group_name`; location = first VNet naming it | 1 | 1 | 1 |
| Virtual network | `vnet-prod-utility`, `vnet-ctm-uat`, `vnet-ctm-prod` (nonprod: `vnet-dev-utility`, `vnet-ctm-dev`, `vnet-ctm-qa`; DR: `vnet-dr-utility`, `vnet-ctm-prod-dr`) | network RG | one per `vnets[]` entry | 3 | 3 | 2 |
| Subnet | per `vnets[].subnets[]`; `data` role carries the `Microsoft.DBforPostgreSQL/flexibleServers` delegation, `pe` role has `privateEndpointNetworkPolicies: Disabled` | inside its VNet | — | 7 | 7 | 5 |
| Network security group | `<vnet>-<subnet>-nsg`, e.g. `vnet-prod-utility-snet-ctm-em-nsg` | network RG | one per subnet; rules derived from the subnet `role` (see NETWORK-REQUIREMENTS.md Option A) | 7 | 7 | 5 |
| VNet peering | `<src>-to-<dst>`, e.g. `vnet-ctm-prod-to-vnet-prod-utility` and `vnet-prod-utility-to-vnet-ctm-prod` | each side's RG | two per spoke (both directions); spokes are never peered to each other | 4 | 4 | 2 |

## Phase `rg` — resource groups (`bicep/rg.bicep`)

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Resource group | `ctm-prod-rg` (nonprod: `ctm-nonprod-rg`) | subscription, primary region | — | 1 | 1 | — |
| Resource group | `ctm-prod-dr-rg` | subscription, DR region | `dr.enabled` | 0 | — | 1 |
| Management lock (CanNotDelete) | `ctm-prod-no-delete` / `ctm-prod-dr-no-delete` | on each platform RG | `resource_lock: true` | 0 | 1 | 1 |

## Phase `foundation` — per-region foundation (`bicep/foundation.bicep`)

Deployed twice on prod: `ctm-prod-foundation` into `ctm-prod-rg` (role
`primary`) and `ctm-prod-foundation-dr` into `ctm-prod-dr-rg` (role `dr`,
same template without the database). DR-column names carry the `ctm-prod-dr`
prefix (identity `ctm-prod-dr-nodes-id`, vault `ctm-prod-dr-kv-p01`, …).

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Private DNS zone (privatelink) | `privatelink.blob.core.windows.net`, `privatelink.file.core.windows.net`, `privatelink.vaultcore.azure.net` | platform RG | per key: only when not supplied via `network.existing_private_dns_zone_ids` | 3 | 3 | 3 |
| Private DNS zone (postgres) | `ctmprodp01.postgres.database.azure.com` (nonprod: `ctmnonprodn01.…`; DR: `ctmproddrp01.…`) | platform RG | only when `postgres` zone id not supplied | 1 | 1 | 1 |
| Privatelink zone VNet link | one per created zone × every platform VNet (`allVnets`: utility + each server VNet + agent overrides + `extra_dns_vnets`); link name = VNet name, registration off | on each created zone | zones created here only — supplied hub zones are **not** linked | 12 | 12 | 8 |
| User-assigned managed identity | `ctm-prod-nodes-id` | platform RG | — | 1 | 1 | 1 |
| Key Vault | `ctm-prod-kv-p01` (RBAC mode, soft delete 30d; purge protection **prod only**; firewall default-deny + `deployer_ip_cidrs`) | platform RG | — | 1 | 1 | 1 |
| Role assignment | Key Vault Secrets User → node identity (deterministic GUID name) | Key Vault scope | — | 1 | 1 | 1 |
| Role assignment | Key Vault Secrets Officer → deployer principal | Key Vault scope | `deployerObjectId` passed (`--deployer-object-id` / `CTM_DEPLOYER_OBJECT_ID`) | 1 | 1 | 1 |
| Private endpoint (+ DNS zone group) | `ctm-prod-kv-pe` | platform RG → utility pe subnet | pe subnet configured | 1 | 1 | 1 |
| Storage account (blob, StorageV2, OAuth-only) | `ctmprodblobp01` (nonprod: `ctmnonprodblobn01`; DR: `ctmproddrblobp01`) | platform RG | — | 1 | 1 | 1 |
| Blob service settings | versioning + change feed on (object-replication prerequisite), 30d blob/container delete retention | on the blob account | — | 1 | 1 | 1 |
| Blob container | `installers`, `backups` | blob account | — | 2 | 2 | 2 |
| Lifecycle management policy | `backups/` tier to Cool 30d → Archive 90d → delete 365d; versions deleted after 60d | blob account | — | 1 | 1 | 1 |
| Role assignment | Storage Blob Data Reader → node identity (whole blob account: installer downloads) | blob account scope | — | 1 | 1 | 1 |
| Role assignment | Storage Blob Data Contributor → node identity (backup uploads) | `backups` container scope | — | 1 | 1 | 1 |
| Private endpoint (+ DNS zone group) | `ctm-prod-blob-pe` | platform RG → utility pe subnet | — | 1 | 1 | 1 |
| Storage account (Premium FileStorage) | `ctmprodfilesp01` (public network access disabled) | platform RG | — | 1 | 1 | 1 |
| NFS 4.1 file share | `ctm-shared` (quota `storage.shared_share_quota_gb`) | files account | — | 1 | 1 | 1 |
| Private endpoint (+ DNS zone group) | `ctm-prod-files-pe` | platform RG → utility pe subnet | — | 1 | 1 | 1 |
| PostgreSQL Flexible Server | `ctm-prod-pg-p01` (delegated data subnet, private access only, ZoneRedundant HA, Sunday 03:00 maintenance) | platform RG | role `primary` only (`databaseCreate`) | 1 | 1 | 0 |
| PostgreSQL configuration | `max_connections=500`, `log_min_duration_statement=5000`, `azure.extensions=PG_STAT_STATEMENTS`, `idle_in_transaction_session_timeout=600000` | on the server | with the server | 4 | 4 | 0 |
| PostgreSQL database | `emdb` + `ctm_<stage>` per Server (prod: `emdb`, `ctm_uat`, `ctm_prod`; nonprod: `emdb`, `ctm_dev`, `ctm_qa`) — UTF8 / en_US.utf8 | on the server | with the server | 3 | 3 | 0 |
| Load balancer (Standard, internal) | `ctm-prod-lb` (frontend in the utility em subnet, pool `em-web`, HTTPS probe `/ControlM/`, rules 443 + `em.web_port` → `em.web_port`) | platform RG | — | 1 | 1 | 1 |
| Public IP | `ctm-prod-lb-pip` | platform RG | `load_balancer.internal: false` only | 0 | 0 | 0 |
| Private DNS zone (platform) | `prod.ctm.internal` (nonprod: `nonprod.ctm.internal`; **same name** in the DR region, a second zone resource in the DR RG) | platform RG | — | 1 | 1 | 1 |
| Platform zone VNet link | one registration-enabled link per platform VNet (link name = VNet name) | on the platform zone | — | 3 | 3 | 2 |
| DNS A record | `ctm-web` → LB frontend IP (TTL 60) | platform zone | — | 1 | 1 | 1 |
| DNS CNAME record | `ctm-db` → PostgreSQL FQDN (DR region: → replica FQDN `ctm-prod-dr-pg-p01.postgres.database.azure.com`) | platform zone | database exists or `databaseHostOverride` set | 1 | 1 | 1 |
| Log Analytics workspace | `ctm-prod-law` (PerGB2018, `observability.log_retention_days`) | platform RG | — | 1 | 1 | 1 |
| VMInsights solution | `VMInsights(ctm-prod-law)` | platform RG | — | 1 | 1 | 1 |
| Data collection rule (Linux) | `ctm-prod-dcr-linux` (syslog + perf counters → workspace) | platform RG | — | 1 | 1 | 1 |
| Action group | `ctm-prod-alerts` (email receivers from `observability.alert_emails`) | platform RG (global) | — | 1 | 1 | 1 |
| Metric alert | `ctm-prod-em-web-unhealthy` (LB `DipAvailability` < 50, sev 0) | on the LB | — | 1 | 1 | 1 |
| Metric alert | `ctm-prod-pg-{cpu,storage,memory,connections,availability}` | on the PG server | primary role only | 5 | 5 | 0 |
| Activity log alert | `ctm-prod-service-health` (Service Health incidents for the region) | subscription-scoped, in platform RG | — | 1 | 1 | 1 |

## Phase `secrets` — Key Vault seeding (**data-plane, python**)

No ARM resources. `ctmdeploy` seeds the six platform secrets into **every**
region's vault (create-if-absent; explicit `CTM_PLATFORM_SECRETS` overrides
update). Full inventory, consumers and rotation: `docs/KEYVAULT.md`.

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Key Vault secret (**data-plane, python**) | `db-admin-password`, `em-db-password`, `em-admin-password`, `ssh-private-key`, `ctm-db-password-<stage>` per Server (prod: `-uat`, `-prod`; nonprod: `-dev`, `-qa`) | `ctm-prod-kv-p01` / `ctm-prod-dr-kv-p01` | DR vault seeded only when `dr.enabled` | 6 | 6 | 6 |

## Phase `compute` — VMs, scale sets, backup (`bicep/compute.bicep`)

Primary: `ctm-prod-compute` (full fleet). DR: `ctm-prod-compute-dr` — **no
VMs** (DR VMs arrive via Site Recovery), only the warm agent pools of the
DR-protected Servers; VM backup is forced off in DR.

Prod nodes: `ctm-em-a`, `ctm-em-b`, `ctm-srv-uat-a/b`, `ctm-srv-prod-a/b`
(6). Nonprod nodes: `ctm-em-a`, `ctm-srv-dev-a`, `ctm-srv-qa-a/b` (4;
`em.enable_secondary` and `servers.dev.enable_secondary` are false).

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Network interface | `<node>-nic` (accelerated networking; EM NICs join the LB pool `em-web`) | platform RG → each node's `subnetId` | per node | 4 | 6 | 0 |
| Managed disk (data) | `<node>-data` (`data_disk_gb`, `disk_type`; ZRS disks are regional, LRS pinned to the zone) | platform RG | per node | 4 | 6 | 0 |
| Virtual machine (+ OS disk `<node>-osdisk`) | `<node>`, e.g. `ctm-srv-prod-a` — zonal, boots the gallery image, user-assigned identity, `customData` = rendered cloud-init (sent only when the VM is new) | platform RG | per node | 4 | 6 | 0 |
| VM extension | `AzureMonitorLinuxAgent` (managed-identity auth) | per VM | per node | 4 | 6 | 0 |
| DCR association | `<node>-dcra` → `ctm-<env>-dcr-linux` | per VM | per node | 4 | 6 | 0 |
| VM scale set (Uniform) | `ctm-prod-agents-uat`, `ctm-prod-agents-prod` (DR: `ctm-prod-dr-agents-prod`, min 0 / default `dr.warm_agents`) — zones per config, Spot optional, terminate scheduled events, embedded AMA extension | platform RG → each pool's `subnetId` | one per Server; DR: DR-protected Servers only | 2 | 2 | 1 |
| Autoscale setting | `<vmss>-autoscale` (CPU out 5 min / in 15 min, bounds from `agents.min/max`) | platform RG | per pool | 2 | 2 | 1 |
| DCR association | `<vmss>-dcra` | per VMSS | per pool | 2 | 2 | 1 |
| Recovery Services vault | `ctm-prod-rsv` (redundancy `backup.storage_mode_type`, cross-region restore per config) | platform RG | `backup.enabled`; never in DR | 1 | 1 | 0 |
| Backup policy (VM, V2) | `ctm-prod-vm-daily` (23:00 UTC, instant restore 5d, daily `daily_retention_days` + Sunday-weekly `weekly_retention_weeks`) | vault | with the vault | 1 | 1 | 0 |
| Protected item (VM) | one per EM/Server VM | vault | with the vault | 4 | 6 | 0 |
| Protection container (storage) | registration of the files account | vault | `backup.include_files_share` | 1 | 1 | 0 |
| Backup policy (file share) | `ctm-prod-files-daily` | vault | `backup.include_files_share` | 1 | 1 | 0 |
| Protected item (file share) | `AzureFileShare;ctm-shared` | vault | `backup.include_files_share` | 1 | 1 | 0 |
| Metric alert | `ctm-prod-<node>-unavailable` (`VmAvailabilityMetric` < 1, sev 1) | on each VM | `vmAvailabilityAlerts` (always passed true) | 4 | 6 | 0 |

## Phase `dr` — cross-region DR plumbing (`bicep/dr.bicep`, prod only)

Subscription-scope deployment `ctm-prod-dr`; runs only when `dr.enabled`.

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| PostgreSQL Flexible Server (read replica) | `ctm-prod-dr-pg-p01` (createMode Replica of `ctm-prod-pg-p01`; carries **every** database incl. `ctm_uat`) | `ctm-prod-dr-rg` | `deployReplica` — gated **off** by the orchestrator once the replica has been promoted | 0 | — | 1 |
| Recovery Services vault (ASR) | `ctm-prod-asr-rsv` (vault only; fabrics/items are the `replication` phase) | `ctm-prod-dr-rg` | `dr.enable_site_recovery` | 0 | — | 1 |
| Storage account (ASR cache) | `ctmprodasrcache<6-char hash>` (Standard_LRS; exact suffix from `uniqueString(primaryRg)`) | `ctm-prod-rg` (**source region** — ASR stages replication traffic here) | `dr.enable_site_recovery` | 0 | 1 | — |
| Object replication policy (destination) | service-minted policy id; rules `installers`→`installers`, `backups`→`backups` (copies everything incl. pre-existing blobs) | on `ctmproddrblobp01` | — (ids read back live on re-deploys so the policy converges) | 0 | — | 1 |
| Object replication policy (source) | same policy id, echoed | on `ctmprodblobp01` | — | 0 | 1 | — |
| Traffic Manager profile (+ `primary`/`dr` endpoints) | `ctm-prod-tm` | `ctm-prod-rg` (global) | `dr.traffic_manager` **and** public LBs on both sides — off in the shipped config | 0 | 0 | 0 |

## Phase `replication` — Site Recovery enablement (**data-plane, python**)

SDK calls against the `ctm-prod-asr-rsv` vault (no ARM deployment; idempotent
— existing objects are skipped). Only the EM pair and the Servers in the
`dr.server_networks` opt-in set are protected: with the shipped prod config
that is `ctm-em-a/b` + `ctm-srv-prod-a/b`; **`ctm-srv-uat-a/b` get nothing**.

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| ASR fabric (**data-plane, python**) | `ctm-prod-fabric-primary`, `ctm-prod-fabric-dr` | ASR vault | `dr.enabled && dr.enable_site_recovery` | 0 | — | 2 |
| ASR protection container (**data-plane, python**) | `ctm-prod-container-primary`, `ctm-prod-container-dr` | per fabric | — | 0 | — | 2 |
| ASR replication policy (**data-plane, python**) | `ctm-prod-asr-policy` (24h recovery points, 60 min app-consistent, 5 min crash-consistent) | ASR vault | — | 0 | — | 1 |
| ASR container mapping (**data-plane, python**) | `ctm-prod-container-mapping` | primary container | — | 0 | — | 1 |
| ASR network mapping (**data-plane, python**) | `ctm-prod-network-mapping-utility` (`vnet-prod-utility` → `vnet-dr-utility`), `ctm-prod-network-mapping-prod` (`vnet-ctm-prod` → `vnet-ctm-prod-dr`) — one per VNet pair | primary fabric | per DR-protected VNet pair | 0 | — | 2 |
| ASR replication-protected item (**data-plane, python**) | `<node>-replication` for `ctm-em-a`, `ctm-em-b`, `ctm-srv-prod-a`, `ctm-srv-prod-b` — OS + data disk via the cache account, Premium_LRS targets, zone + per-component DR subnet (EM → DR utility em subnet, Server → its DR server subnet) | primary container | EM always; Servers in `dr.server_networks` only | 0 | — | 4 |
| ASR protected-item NIC update (**data-plane, python**) | EM items only: recovery NIC joins the DR LB backend pool (applied once initial sync has NIC details; re-run `--phase replication --wait`) | on the EM items | — | 0 | — | 2 |

## `ctmdeploy media upload` — installer media (**data-plane, python**)

Blob uploads into each platform's `installers` container (primary + DR when
`dr.enabled`), run by the pipeline media stage (`publishMedia=true`) or
manually. Blob names must match `installer_blobs`; layout/mapping rules in
`docs/AZURE-DEVOPS.md` §5.

| Resource type | Name | Scope / RG | Condition | nonprod | prod primary | prod DR |
|---|---|---|---|---|---|---|
| Blob (**data-plane, python**) | `em/DREM.9.0.21.300_Linux-x86_64.tar.Z`, `em/silent-em-primary.xml`, `em/silent-em-secondary.xml`, `server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z`, `server/silent-server-primary.xml`, `server/silent-server-secondary.xml` | `installers` container of each blob account | DR container only when `dr.enabled` (object replication also copies primary→DR) | 6 | 6 | 6 |

## Golden image (shared across environments)

`python -m ctmdeploy image gallery` ensures the resource group and deploys
`bicep/images.bicep` (deployment `ctm-images`); Packer publishes the
versions. These are shared by nonprod and prod — one gallery per
subscription, not per environment. The image contains the **OS only**; BMC
software is installed at provision time (see `packer/README.md`).

| Resource type | Name | Scope / RG | Condition | Count |
|---|---|---|---|---|
| Resource group | `ctm-images-rg` | subscription, primary region | created by `ctmdeploy image gallery` (python `ensure_resource_group`) | 1 |
| Compute Gallery | `ctmgallery` | `ctm-images-rg` | — | 1 |
| Gallery image definition | `ctm-rhel9` (Linux, Gen2, x64) | gallery | — | 1 |
| Gallery image version (**published by Packer**, never Bicep) | `<x.y.z>`, replicated to eastus2 + centralus | image definition | one per `ctmdeploy image build --version` | per build |
| Temporary build RG + VM (**Packer, transient**) | Packer-managed names | subscription | exists only during a build | 0 after build |
