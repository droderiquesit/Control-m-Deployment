# Control-M on Azure — Bicep + Python + Packer

Infrastructure as code for a highly available, disaster-recoverable and
horizontally scalable **BMC Control-M** platform on Microsoft Azure, serving
the **dev, qa, uat and prod** stages from two platforms:

| Platform | Enterprise Manager | Control-M/Servers | Why |
|----------|--------------------|-------------------|-----|
| **nonprod** | 1 node (HA optional) | `CTM_DEV`, `CTM_QA` | cheap, isolated place to develop jobs and rehearse upgrades |
| **prod** | HA pair | `CTM_UAT`, `CTM_PROD` | UAT runs on production infrastructure (same EM, database host, DR), so what is accepted is what goes live |

Each Control-M/Server has its own VM pair, its own database on the shared
PostgreSQL host, its own credentials and its own agent scale set / host group.
Job definitions move dev → qa → uat → prod with the Automation API
(`ctm deploy`), not by re-deploying infrastructure.

The placement of every tier is config-driven: the EM and all shared services
(LB, database, private endpoints) live in a per-platform **utility VNet**,
while every Control-M/Server runs in its **own VNet** with its agent scale
set beside it (hub-and-spoke per platform; server VNets peer only with the
utility VNet, never with each other, so stages are isolated at the network
layer). The DR region mirrors this with a DR utility VNet plus one DR VNet
per DR-protected Server. Networking comes in two flavors
(`docs/NETWORK-REQUIREMENTS.md`): **bring-your-own** (the default — VNets,
subnets, peering and NSGs pre-exist, the toolchain only selects them) or
**Option A**, where flipping `network_infrastructure.create: true` makes a
separate `--phase network` deployment (`bicep/network.bicep`) provision the
full PoC network — resource groups, VNets, subnets, one NSG per subnet and
the spoke ⇄ utility peerings — from the address plan shipped in the config
files.

**How the software gets onto the machines**: the Packer image is the
**operating system only** — BMC software is never baked in. EM and
Control-M/Server are silently installed at provision time by cloud-init from
media in the `installers` blob container, and agents provision themselves
through the EM Automation API. The installer media (BMC EPD archives +
silent XMLs) is stored as **Universal Packages in an Azure Artifacts feed**;
the pipeline's media stage (or `ctmdeploy media upload`) moves it into each
platform's blob container — primary and DR (`docs/AZURE-DEVOPS.md` §5).

## Toolchain

| Tool | Role |
|------|------|
| **Bicep** (`bicep/`) | every Azure resource, compiled and submitted as ARM deployments |
| **Python** (`deploy/`, package `ctmdeploy`) | the orchestrator: loads `environments/<env>/config.yaml`, generates/seeds Key Vault secrets, renders cloud-init, runs the phased deployments, enables Site Recovery replication, drives DR failover |
| **Packer** (`packer/`) | builds the **ctm-rhel9** golden image (RHEL 9 + Control-M OS prep — the **OS only**, no BMC software) and publishes it to an Azure Compute Gallery; every VM and scale-set instance boots from it, then installs EM/Server from blob media (agents: via the EM Automation API) at first boot |

**Deployments are stateless.** Bicep/ARM deployments compare the template to
what exists in Azure on every run — there is no state file, no state backend
and no bootstrap step. `ctmdeploy whatif` shows the changes a run would make;
the apply re-runs the same deployments.

```
bicep/
  rg.bicep                  subscription scope: resource groups + optional locks
  network.bicep             subscription scope, OPTIONAL: full network (Option A) —
                            RGs, VNets, per-subnet NSGs, spoke<->utility peering
  foundation.bicep          one region's foundation (role primary|dr)
  compute.bicep             EM/Server VMs + agent VMSS + backup + VM alerts
  dr.bicep                  PG replica, ASR vault, blob object replication, Traffic Manager
  images.bicep              Compute Gallery + ctm-rhel9 image definition
  modules/                  network, vnet, vnet-peering, dns, security, storage,
                            database, load-balancer, node, agent-pool,
                            observability, backup, ...
environments/
  nonprod/config.yaml       single source of truth per platform (python reads it;
  prod/config.yaml          bicep gets params derived from it)
cloud-init/                 boot-time templates (__TOKEN__ placeholders) for EM,
                            Server and Agent nodes — environment-specific work only
packer/
  ctm-rhel9.pkr.hcl         golden image build (marketplace RHEL 9 -> Compute Gallery)
  scripts/                  baked OS prep: packages, tuning, Node.js 20, deprovision
                            (OS only — BMC software is never baked in)
deploy/
  pyproject.toml            pip install -e deploy/
  ctmdeploy/                cli, config, azure, secrets, render, deploy, media,
                            replication, failover
  tests/                    pure-python tests (config, network/media, rendering)
pipelines/                  Azure DevOps stage/step templates and variables
azure-pipelines.yml         ADO pipeline (Checkmarx One + Delinea DSV + gated applies
                            + media stage: Azure Artifacts feed -> installers blob)
.github/workflows/deploy.yml  equivalent GitHub Actions pipeline
scripts/silent-install/     placeholder contract for BMC silent-install XML files
docs/                       setup, configuration, resources, architecture, network,
                            key vault, Azure DevOps, DR runbook, operations
Makefile                    make whatif/deploy/image/failover/fmt/lint/test
```

## Documentation index

| Doc | What it answers |
|-----|-----------------|
| `docs/SETUP.md` | **start here** — the numbered end-to-end setup (prereqs → config → network → media feed → image → deploy → media → verify → pipeline → DR drill) |
| `docs/CONFIGURATION.md` | every `config.yaml` variable: type, default, allowed values, what it changes, which phase consumes it |
| `docs/RESOURCES.md` | the full inventory of deployed resources per phase, with concrete names, conditions and per-environment counts (python data-plane items marked) |
| `docs/ARCHITECTURE.md` | diagrams and design decisions, including the image/install model |
| `docs/NETWORK-REQUIREMENTS.md` | Option A (repo-created network + PoC address plan) and the BYO contract; private-endpoint and DNS zone/link inventory |
| `docs/KEYVAULT.md` | what is in each Key Vault, who consumes each secret and when, rotation, RBAC/firewall posture |
| `docs/AZURE-DEVOPS.md` | pipeline setup: WIF, Delinea DSV, Checkmarx One, environments/approvals, the Azure Artifacts media feed |
| `docs/OPERATIONS.md` | scaling, patching, image revisions, secret rotation, backup/restore |
| `docs/DR-RUNBOOK.md` | regional failover and failback |
| `packer/README.md` | golden image lifecycle (OS only — no BMC software) |

## Architecture (per platform)

| Tier | Resource | HA | Scale |
|------|----------|----|-------|
| Network placement | utility VNet (`network.utility`): EM, LB, PostgreSQL, private endpoints; one VNet per Server (`servers.<stage>.network`): Server VMs + agent VMSS | server VNets peer to the utility VNet only — never to each other (stage isolation) | config-driven per component; agents relocatable via `agents_vnet_name` |
| Control-M/Enterprise Manager | 2 zonal VMs (`ctm-em-a` zone 1, `ctm-em-b` zone 2) in the utility VNet's em subnet, persistent data disk, shared DB | EM primary/secondary; LB health probe follows the active node | vertical (VM size) |
| Control-M/Server (one per stage) | 2 zonal VMs per Server (`ctm-srv-<stage>-a/b`) in the stage's own VNet, persistent data disk, own database | Server primary/secondary via Configuration Agent failover | vertical; add a stage by adding a map entry |
| Control-M/Agents (one pool per Server) | VM Scale Set across 3 zones in the stage's VNet, registered to the Server's host group | zone balanced, self-healing | autoscale on CPU, min/max per stage, Spot optional |
| Database | Azure Database for PostgreSQL Flexible Server | **ZoneRedundant** standby, automatic failover | SKU/storage per env, auto-grow |
| Shared filesystem | Azure Files Premium, **NFS 4.1**, ZRS | zone redundant | quota per env |
| Media/backups | Blob storage, RA-GZRS | geo redundant | — |
| Entry point | Standard Load Balancer (zone-redundant frontend) `ctm-web.<env>.ctm.internal` → 8443 | probes `/ControlM/` on both EM nodes | — |
| Secrets | Key Vault (RBAC, private endpoint), VM managed identity | — | — |
| Monitoring | Log Analytics + Azure Monitor Agent, metric alerts, Service Health | — | — |
| Backup | Recovery Services vault (GRS + cross-region restore in prod), daily VM + file share | — | — |
| DR (prod platform) | second region: PostgreSQL cross-region replica, **Site Recovery** replication of the EM VMs + the VMs of Servers opted in via `dr.server_networks` (shipped config: CTM_PROD only), replicated installers, warm agent scale set per DR-protected Server, optional Traffic Manager | — | — |
| Golden image | **ctm-rhel9** in Compute Gallery `ctmgallery` (`ctm-images-rg`), versions replicated to both regions | — | rebuild with Packer, bump `image.image_version` |

Node identity is stable: hostnames, persistent `/ctm` data disks and the
`ctm-db` / `ctm-web` DNS aliases are identical in the primary and DR regions,
so a failed-over node starts Control-M unchanged against the promoted database.
See `docs/ARCHITECTURE.md` for the diagrams (including how VM images are
built, stored and consumed) and `docs/DR-RUNBOOK.md` for failover.

## Platform matrix

| | nonprod | prod |
|---|---|---|
| Control-M/Servers | `CTM_DEV` (single VM), `CTM_QA` (HA pair) | `CTM_UAT` (HA pair), `CTM_PROD` (HA pair) |
| EM | single D4s_v5 | HA pair D16s_v5, ZRS disks |
| Server VM sizes | D4s_v5 | uat D8s_v5, prod D16s_v5, ZRS disks |
| PostgreSQL (one host, one DB per Server + EM) | D2ds_v5, zone-redundant, 7d | D8ds_v5, zone-redundant, geo-backup, 35d |
| Agents (min/max) | dev 1/2 Spot, qa 1/4 Spot | uat 2/6, prod 3/20 |
| Backup vault | ZRS, 14d | GRS + cross-region restore, 35d / 52w |
| DR region | — | replica + Site Recovery (EM + `CTM_PROD`; `CTM_UAT` opted out) + 1 warm agent for prod |
| Locks / purge protection | — | yes |
| EM/Server VMs | 4 | 6 |

Everything above is in `environments/<platform>/config.yaml`; the code is
identical. Moving a stage to its own platform later is a config change (new
environment with one Server).

## Prerequisites

1. **Networks** in each region (see `docs/NETWORK-REQUIREMENTS.md`): either
   bring your own — a utility VNet (`network.utility`) with an em subnet, a
   subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers` and a
   private-endpoint subnet, plus **one VNet per Control-M/Server**
   (`servers.<stage>.network`) with a server subnet and (optionally separate)
   agents subnet, each peered to the utility VNet, and the DR equivalents
   (`dr.network.utility`, `dr.server_networks`) where DR is enabled — **or**
   flip `network_infrastructure.create: true` and let
   `ctmdeploy deploy --phase network` provision all of it from the address
   plan shipped in the config files (Option A). Hub-managed private DNS zones
   can be passed in; missing ones are created in the platform resource group
   and linked to every platform VNet.
2. **BMC media** (EPD download) for Control-M/EM and Control-M/Server, plus the four
   silent-install XML files described in `scripts/silent-install/README.md` —
   published as Universal Packages to the `ctm-media` Azure Artifacts feed
   (`docs/AZURE-DEVOPS.md` §5), or staged in a local directory for a manual
   `ctmdeploy media upload`.
3. **Azure identity** running the deployment with `Contributor` + `User Access
   Administrator` (role assignments) on the subscription; `Key Vault Secrets
   Officer` is granted automatically to the deployer object id you pass.
4. **Tools**: Python ≥ 3.11, the Bicep CLI, Azure CLI (login only), Packer ≥ 1.10
   for image builds; (optional) pre-commit.
   `pip install -e deploy/` installs `ctmdeploy`.

## Deploy

The complete, numbered walkthrough (including the Azure Artifacts feed and
the pipeline) is **`docs/SETUP.md`**. The short version:

```bash
# 0. install the orchestrator
pip install -e "deploy/[dev]"

# 1. golden image first: gallery (one-time, idempotent) + packer build
az login
python -m ctmdeploy image gallery --env prod       # ctm-images-rg / ctmgallery / ctm-rhel9
python -m ctmdeploy image build  --env prod --version 1.0.0
#    the image is the OS only — BMC software is installed at provision time

# 2. seed the config: edit environments/nonprod/config.yaml
#    (subscription id, network names, deployer IPs, alert emails, image version;
#     optionally network_infrastructure.create: true to have the network built)

# 3. what-if, then deploy the phases
python -m ctmdeploy whatif --env nonprod
python -m ctmdeploy deploy --env nonprod --phase all
#    phases, in order: network -> rg -> foundation -> secrets -> compute -> dr -> replication
#    (network only when network_infrastructure.create; dr/replication only where
#     dr.enabled; any phase can be re-run alone)

# 4. publish media + silent XMLs into the installers container(s):
#    pipeline (publishMedia=true), or from a staged directory:
python -m ctmdeploy media upload --env nonprod --path ./media
#    (prod uploads to the primary AND DR containers; warns about missing blobs)

# 5. the EM/Server VMs are already booting and retry the download; follow with
az serial-console connect ... # or: ssh via Bastion, then tail -f /var/log/ctm-bootstrap.log
```

There is no state backend and no bootstrap step: every deployment is a
stateless ARM operation, repeatable and idempotent. `ctmdeploy whatif` is the
review tool; the Key Vault secrets are generated (or taken from
`CTM_PLATFORM_SECRETS` overrides) and seeded idempotently — existing secrets
are never rotated silently.

Bootstrap order is enforced on the nodes themselves: the secondary EM waits for
the primary's web server, the secondary Server waits for the primary's
Configuration Agent, and agents wait for the Automation API behind the LB.

Promotion between environments is a pull request that changes only
`environments/<platform>/config.yaml`. The **Azure DevOps pipeline**
(`azure-pipelines.yml`, setup in `docs/AZURE-DEVOPS.md`) runs bicep build/lint
and the python tests, a Checkmarx One scan (SAST, SCA, IaC), a what-if per
platform, then applies nonprod → prod on `main` behind Azure DevOps
environment approvals. Platform passwords come from Delinea DevOps Secrets
Vault and override the generated ones. An equivalent GitHub Actions workflow
lives in `.github/workflows/deploy.yml`. A manual pipeline run with
`runImageBuild=true` publishes a new golden-image version with Packer.

## Idempotency

`ctmdeploy deploy --env <env> --phase all` is **safe to re-run** at any time:
every deployment is a stateless ARM operation in Incremental mode, and the
orchestrator guards the few properties ARM cannot converge on its own:

* **VM `customData`** is immutable after create — it is sent only for VMs that
  do not exist yet, so re-deploys never try to change it. (VMSS `customData`
  *is* updatable and is always sent; it applies to new instances.)
* **VMSS capacity** is set only when the scale set is first created; afterwards
  autoscale owns the instance count and re-deploys omit it.
* **Role assignments** use deterministic GUID names, so re-deploys converge
  instead of colliding.
* **DR replica**: a promoted (no longer replica) server is detected and not
  re-created; **object replication** policy ids are read back and re-used;
  the **PostgreSQL HA zone layout** is read from the live server, so a zone
  swap after an HA failover is not fought.
* **Secrets** are seeded only when absent (or explicitly overridden) — never
  rotated silently.

The one thing re-running does **not** do is remove drift: Incremental
deployments never delete resources, so anything renamed in config or created
out-of-band must be removed manually (`az resource delete` / portal) — or via
a future opt-in to Azure Deployment Stacks. `ctmdeploy whatif` shows exactly
what a run would change.

## What you still own after deploy

* **Register each Control-M/Server in its EM** (Control-M Configuration
  Manager → Add Control-M/Server: `CTM_DEV` on `ctm-srv-dev-a`, `CTM_QA` on
  `ctm-srv-qa-a` / `ctm-srv-qa-b`, and on prod `CTM_UAT`, `CTM_PROD`) and
  enable automatic failover for the HA pairs. Agents register themselves to
  their Server through the Automation API once the Server exists.
* **Promote job definitions** between stages with the Automation API
  (`ctm deploy` against the stage's Server) or Workload Change Manager; the
  infrastructure pipeline only promotes platform changes.
* **Certificates**: EM ships a self-signed certificate; replace it and remove
  the `-k` / `NODE_TLS_REJECT_UNAUTHORIZED=0` shortcuts in
  `cloud-init/agent.sh.tmpl`.
* **Validate CLI syntax** of the `ctm provision` / `ctm config` commands in the
  agent template against `ctm provision -h` for your Control-M release.
* **DR drills** (`python -m ctmdeploy failover --env prod --test`) at least
  quarterly.

## Operations

`docs/OPERATIONS.md` covers scaling, patching, image revisions, rotating
secrets, backups and restoring. `docs/KEYVAULT.md` covers the secret
inventory, consumers and rotation. `docs/DR-RUNBOOK.md` covers regional
failover and failback. `packer/README.md` covers the golden image lifecycle
(OS only — BMC software is installed at provision time).
