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
subnets, peering and NSGs pre-exist, the toolchain only selects them) or the
standalone **`network/` folder** — self-contained Bicep the **cloud team**
deploys with nothing but the az CLI (`network/README.md`), covering resource
groups, VNets, subnets (bastion/appgw/func included), one NSG per subnet and
every peering: spoke ⇄ utility, the prod ⇄ DR **global** utility pairing and
the build VNet into all three utilities. The platform pipeline can deploy
the same plan itself (`network_infrastructure.create: true` →
`--phase network`).

An **access layer** completes the platform (`--phase access`): **Azure
Bastion** for private SSH to every node (no VM ever has a public IP; DR
reachable over the global peering), a **private Application Gateway** (prod)
whose layer-4 TCP frontend publishes **`ctm-global`** — one alias balanced
across the primary and DR internal load balancers, following whichever EM is
healthy, with a **DR twin** of the gateway in the DR utility VNet
(`access.appgw.dr_instance`) so the private entry point itself survives a
primary-region loss — a **Key Vault → Control-M sync Durable Function** (`kv_sync`,
mirrors `ctm-sync`-tagged secrets into Control-M through the Automation
API), and the **`ctm-build-agents`** scale set: Azure DevOps elastic-pool
build agents that deploy into nonprod, prod and DR.

**How the software gets onto the machines** (v4.1 two-image model): the
**base** Packer image (`ctm-rhel9`) is the **operating system only** — BMC
software is never baked into it — while the **agent** image (`ctm-agent`)
additionally embeds the **Control-M/Agent, silent-installed but
unconfigured**. A thin cloud-init shim fetches **one idempotent, stdlib-only
python installer** (`node-scripts/ctm_node_install.py`) from blob and runs
it: EM and Control-M/Server are silently installed from media in the
`installers` blob container, and the agent scale-set instances only
**configure, start and register** their baked-in agent through the EM
Automation API REST. The EM/Server installer media (EPD archives + silent
XMLs) is stored as **Universal Packages in an Azure Artifacts feed**; the
pipeline's media stage (or `ctmdeploy media upload`) moves it into each
platform's blob container — primary and DR — while the `control-m-agent`
package feeds the **Packer agent-image build** instead
(`docs/AZURE-DEVOPS.md` §5).

## Toolchain

| Tool | Role |
|------|------|
| **Bicep** (`bicep/`) | every Azure resource, compiled and submitted as ARM deployments |
| **Python** (`deploy/`, package `ctmdeploy`) | the orchestrator: loads `environments/<env>/config.yaml`, generates/seeds Key Vault secrets, renders cloud-init, runs the phased deployments, enables Site Recovery replication, drives DR failover |
| **Packer** (`packer/`) | builds the two golden images and publishes them to an Azure Compute Gallery: **ctm-rhel9** (RHEL 9 + Control-M OS prep — the **OS only**, no BMC software; EM/Server VMs install from blob media at first boot) and **ctm-agent** (the same OS prep + the **Control-M/Agent baked in, unconfigured**; the agent scale sets boot it and only configure/start/register at boot) |

**Deployments are stateless.** Bicep/ARM deployments compare the template to
what exists in Azure on every run — there is no state file, no state backend
and no bootstrap step. `ctmdeploy whatif` shows the changes a run would make;
the apply re-runs the same deployments.

```
bicep/
  rg.bicep                  subscription scope: resource groups + optional locks
  foundation.bicep          one region's foundation (role primary|dr)
  compute.bicep             EM/Server VMs + agent VMSS + backup + VM alerts
  access.bicep              subscription scope: Bastion, private Application Gateway
                            (ctm-global), kvsync Function infra, ADO build agents
  dr.bicep                  PG replica, ASR vault, blob object replication, Traffic Manager
  images.bicep              Compute Gallery + ctm-rhel9/ctm-agent image definitions
  modules/                  network, dns, security, storage, database, load-balancer,
                            node, agent-pool, bastion, appgw, function-kvsync,
                            build-agents, observability, backup, ...
network/                    CLOUD-TEAM-OWNED full network: main.bicep + vnet/peering
  params/*.bicepparam       modules + per-env params — az CLI only, no python
identity/
  deployer-role.bicep       custom role "Control-M Platform Deployer" (everything
                            except VNet/subnet/NSG/peering writes) + README
environments/
  nonprod/config.yaml       single source of truth per platform (python reads it;
  prod/config.yaml          bicep gets params derived from it)
cloud-init/                 thin boot shims (__TOKEN__ placeholders): OS plumbing +
                            node.json, then fetch & run the python node installer
node-scripts/
  ctm_node_install.py       ONE idempotent stdlib-only installer for EM/Server/Agent
                            (stage machine, safe to re-run; uploaded to blob)
functions/
  kvsync/                   Key Vault -> Control-M secret sync (Durable Function;
                            tagged ctm-sync=true secrets -> EM Automation API)
packer/
  ctm-rhel9.pkr.hcl         BASE golden image (marketplace RHEL 9 -> Compute Gallery;
                            OS only — never any BMC software)
  ctm-agent.pkr.hcl         AGENT golden image (same OS prep + Control-M/Agent
                            silent-installed from the control-m-agent package,
                            unconfigured — configured/registered at boot)
  scripts/                  baked OS prep: packages, tuning, Node.js 20,
                            30-agent-install.sh (agent image only), deprovision
deploy/
  pyproject.toml            pip install -e deploy/
  ctmdeploy/                cli, config, azure, secrets, render, deploy, media,
                            kvsync, replication, failover
  tests/                    pure-python tests (config, network/media, rendering,
                            installer access)
pipelines/                  Azure DevOps stage/step templates and variables
azure-pipelines.yml         ADO pipeline (Checkmarx One + Delinea DSV + gated applies
                            + media stage: Azure Artifacts feed -> installers blob;
                            agentPoolName flips every stage onto scale-set agents)
.github/workflows/deploy.yml  equivalent GitHub Actions pipeline
scripts/silent-install/     placeholder contract for BMC silent-install XML files
docs/                       setup, configuration, resources, architecture, network,
                            identity permissions, key vault, Azure DevOps,
                            DR runbook, operations
Makefile                    make whatif/deploy/image/failover/fmt/lint/test
```

## Documentation index

| Doc | What it answers |
|-----|-----------------|
| `docs/SETUP.md` | **start here** — the numbered end-to-end setup (prereqs → config → deployer identity → network → media feed → image → deploy → media → verify → pipeline/agent pool → DR drill) |
| `docs/CONFIGURATION.md` | every `config.yaml` variable: type, default, allowed values, what it changes, which phase consumes it |
| `docs/RESOURCES.md` | the full inventory of deployed resources per phase, with concrete names, conditions and per-environment counts (python data-plane items marked) |
| `docs/ARCHITECTURE.md` | diagrams and design decisions, including the image/install model |
| `docs/NETWORK-REQUIREMENTS.md` | the full network requirements list: the cloud-team `network/` folder, PoC address plan, BYO contract, flow/port matrix, private-endpoint and DNS zone/link inventory |
| `docs/IDENTITY-PERMISSIONS.md` | per-phase permission matrix: the deployer custom role (`identity/`), data-plane roles, the cloud-team and Azure DevOps identities, built-in fallback |
| `docs/KEYVAULT.md` | what is in each Key Vault, who consumes each secret and when, rotation, RBAC/firewall posture, the kvsync tagging convention |
| `docs/AZURE-DEVOPS.md` | pipeline setup: WIF, Delinea DSV, Checkmarx One, environments/approvals, the Azure Artifacts media feed, the scale-set agent pool |
| `docs/OPERATIONS.md` | scaling, patching, image revisions, secret rotation, kvsync/Bastion/build-agent operations, backup/restore |
| `docs/DR-RUNBOOK.md` | regional failover and failback |
| `network/README.md` | the cloud-team network folder: quickstart, plan, subnet-role table |
| `functions/kvsync/README.md` | the Key Vault → Control-M sync function |
| `packer/README.md` | golden image lifecycle: base ctm-rhel9 (OS only — never BMC software) + ctm-agent (agent baked in) |

## Architecture (per platform)

| Tier | Resource | HA | Scale |
|------|----------|----|-------|
| Network placement | utility VNet (`network.utility`): EM, LB, PostgreSQL, private endpoints; one VNet per Server (`servers.<stage>.network`): Server VMs + agent VMSS | server VNets peer to the utility VNet only — never to each other (stage isolation) | config-driven per component; agents relocatable via `agents_vnet_name` |
| Control-M/Enterprise Manager | 2 zonal VMs (`ctm-em-a` zone 1, `ctm-em-b` zone 2) in the utility VNet's em subnet, persistent data disk, shared DB | EM primary/secondary; LB health probe follows the active node | vertical (VM size) |
| Control-M/Server (one per stage) | 2 zonal VMs per Server (`ctm-srv-<stage>-a/b`) in the stage's own VNet, persistent data disk, own database | Server primary/secondary via Configuration Agent failover | vertical; add a stage by adding a map entry |
| Control-M/Agents (one pool per Server) | VM Scale Set across 3 zones in the stage's VNet, booting the **ctm-agent** image (agent baked in; configured + registered at boot), registered to the Server's host group | zone balanced, self-healing | autoscale on CPU, min/max per stage, Spot optional; agent upgrades roll via image-version bumps (VMSS model update) |
| Database | Azure Database for PostgreSQL Flexible Server | **ZoneRedundant** standby, automatic failover | SKU/storage per env, auto-grow |
| Shared filesystem | Azure Files Premium, **NFS 4.1**, ZRS | zone redundant | quota per env |
| Media/backups | Blob storage, RA-GZRS | geo redundant | — |
| Entry point | Standard Load Balancer (zone-redundant frontend) `ctm-web.<env>.ctm.internal` → 8443 | probes `/ControlM/` on both EM nodes | — |
| Global entry point (prod) | private Application Gateway (L4 TCP 443/8443) `ctm-global.prod.ctm.internal` — backend pool = primary **and** DR internal LBs; a DR twin (`ctm-prod-dr-appgw`, `access.appgw.dr_instance`) serves the same alias from the DR zone copy so the entry survives a primary-region loss | TCP health probe keeps only answering regions in rotation (automatic primary/DR) | autoscale 1–2 per gateway |
| Operator access | Azure Bastion (Standard) in the utility VNet — SSH to every node and build agent, DR included via the global peering; no VM has a public IP | — | — |
| Secret sync | `ctm-<env>-kvsync` Durable Function (EP1, VNet-integrated): Key Vault secrets tagged `ctm-sync=true` → Control-M secret store via the Automation API | — | schedule per `kv_sync.schedule` |
| Build agents (prod) | `ctm-build-agents` VMSS in `vnet-ctm-build` (peered to all three utility VNets) — Azure DevOps elastic pool | — | capacity owned by the ADO pool |
| Secrets | Key Vault (RBAC, private endpoint), VM managed identity | — | — |
| Monitoring | Log Analytics + Azure Monitor Agent, metric alerts, Service Health | — | — |
| Backup | Recovery Services vault (GRS + cross-region restore in prod), daily VM + file share | — | — |
| DR (prod platform) | second region: PostgreSQL cross-region replica, **Site Recovery** replication of the EM VMs + the VMs of Servers opted in via `dr.server_networks` (shipped config: CTM_PROD only), replicated installers, warm agent scale set per DR-protected Server, optional Traffic Manager | — | — |
| Golden images | **ctm-rhel9** (base, OS only) + **ctm-agent** (agent baked in) in Compute Gallery `ctmgallery` (`ctm-images-rg`), versions replicated to both regions | — | rebuild with Packer, bump `image.base.version` / `image.agent.version` (agent revisions roll via VMSS model update) |

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

1. **Networks** in each region (the full requirements list:
   `docs/NETWORK-REQUIREMENTS.md`): either bring your own — a utility VNet
   (`network.utility`) with em/data/pe subnets plus the access-layer subnets
   (`AzureBastionSubnet`, `snet-ctm-appgw` on prod, `snet-ctm-func`), **one
   VNet per Control-M/Server** (`servers.<stage>.network`) peered to the
   utility VNet, the DR equivalents (`dr.network.utility`,
   `dr.server_networks`), the prod ⇄ DR **global** utility peering and the
   build VNet — **or** have the cloud team deploy exactly that from the
   standalone **`network/` folder** with the az CLI alone
   (`network/README.md`); the pipeline can also deploy the same plan via
   `network_infrastructure.create: true` → `--phase network`. Hub-managed
   private DNS zones can be passed in; missing ones are created in the
   platform resource group and linked to every platform VNet.
2. **BMC media** (EPD download) for Control-M/EM, Control-M/Server **and
   Control-M/Agent**, plus the five silent-install XML files described in
   `scripts/silent-install/README.md` — published as Universal Packages to
   the `ctm-media` Azure Artifacts feed (`docs/AZURE-DEVOPS.md` §5), or
   staged in a local directory for a manual `ctmdeploy media upload`.
3. **Azure identity** running the deployment with the **Control-M Platform
   Deployer** custom role (`identity/deployer-role.bicep` — everything except
   network writes) plus the data-plane pair Key Vault Secrets Officer /
   Storage Blob Data Contributor, or the broader built-in fallback
   `Contributor` + `User Access Administrator`. Per-phase matrix:
   `docs/IDENTITY-PERMISSIONS.md`. The network deployment itself needs the
   cloud team's identity (Network Contributor on `rg-network-*`).
4. **Tools**: Python ≥ 3.11, the Bicep CLI, Azure CLI (login only), Packer ≥ 1.10
   for image builds; (optional) pre-commit.
   `pip install -e deploy/` installs `ctmdeploy`. The **nodes** need nothing:
   the installer is stdlib-only python on the image's system python3.

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
#    phases, in order: network -> rg -> foundation -> secrets -> compute ->
#    access -> dr -> replication (network only when
#    network_infrastructure.create; dr/replication only where dr.enabled; any
#    phase can be re-run alone). compute uploads the node installer to blob
#    first; access adds Bastion, ctm-global (prod; + its DR gateway twin when
#    access.appgw.dr_instance), kvsync infra, build agents.
python -m ctmdeploy kvsync deploy --env nonprod   # push the sync function code

# 4. publish media + silent XMLs into the installers container(s):
#    pipeline (publishMedia=true), or from a staged directory:
python -m ctmdeploy media upload --env nonprod --path ./media
#    (prod uploads to the primary AND DR containers; warns about missing blobs)

# 5. the VMs are already booting and retry the download; follow with
az network bastion ssh --name ctm-nonprod-bastion -g ctm-nonprod-rg \
  --target-ip-address <node IP> --auth-type ssh-key --username ctmops --ssh-key ./ctmops.pem
tail -f /var/log/ctm-bootstrap.log   # idempotent installer; re-run any time:
                                     # systemctl start ctm-install.service
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
* **The node install itself** is idempotent, not just the deployments: the
  stdlib-only installer is a stage machine (`/var/lib/ctm/install-state.json`
  + real-state markers such as `.ctm_installed` on the persistent disk and
  `.extracted` on the shared media cache, with an flock around extraction),
  so re-running it — or rebooting mid-install — converges
  (`systemctl start ctm-install.service`).

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
* **Certificates**: EM ships a self-signed certificate; replace it, then set
  the kvsync app setting `CTM_VERIFY_TLS=true` and remove the flagged
  unverified-TLS contexts in `node-scripts/ctm_node_install.py` (the agent
  registration calls — the same posture as the old `-k`).
* **Validate the Automation API endpoints** the installer and the kvsync
  function call (`/session/login`, host-group add/deregister, `run/secret`)
  against your Control-M release — paths follow 9.0.21.
* **DR drills** (`python -m ctmdeploy failover --env prod --test`) at least
  quarterly.

## Operations

`docs/OPERATIONS.md` covers scaling, patching, image revisions, rotating
secrets, backups and restoring. `docs/KEYVAULT.md` covers the secret
inventory, consumers and rotation. `docs/DR-RUNBOOK.md` covers regional
failover and failback. `packer/README.md` covers the golden image lifecycle
(OS only — BMC software is installed at provision time).
