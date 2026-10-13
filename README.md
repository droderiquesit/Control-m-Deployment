# Control-M on Azure — Terraform

Infrastructure as Code for a highly available, disaster-recoverable and
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

The platform is deployed **into networks you already own**: every environment
points at an existing VNet and subnets. Terraform creates the Control-M tiers,
database, storage, secrets, monitoring, backup and DR plumbing — nothing in the
network team's domain.

```
environments/            one root per platform (same code, different tfvars)
  nonprod | prod         main.tf  variables.tf  terraform.tfvars  backend.hcl
modules/
  platform/              composition for ONE region (primary or dr role)
    templates/           cloud-init bootstrap for EM, Server and Agent nodes
  network/               lookup of the existing VNet/subnets (+ missing private DNS zones)
  security/              managed identity, Key Vault, secrets, private endpoint
  storage/               blob (installers/backups) + Premium Files NFS share
  database/              PostgreSQL Flexible Server (zone-redundant HA)
  load-balancer/         Standard LB for Control-M Web / Automation API
  node/                  zonal VM with persistent data disk (EM / Server)
  agent-pool/            VM Scale Set + autoscale + graceful deregistration
  dns/                   platform private DNS zone (ctm-web, ctm-db aliases)
  observability/         Log Analytics, DCR, action group, alerts
  backup/                Recovery Services vault, VM + file share policies
  dr/                    DB cross-region replica, Site Recovery, blob replication, Traffic Manager
bootstrap/               remote state (resource group + storage account)
azure-pipelines.yml      Azure DevOps pipeline (Checkmarx One + Delinea DSV + gated applies)
pipelines/               pipeline stage/step templates and variables
.github/workflows/       equivalent GitHub Actions pipeline
scripts/
  silent-install/        placeholder contract for BMC silent-install XML files
  dr-failover.sh         scripted regional failover
docs/                    architecture, network requirements, DR runbook, operations
```

## Architecture (per platform)

| Tier | Resource | HA | Scale |
|------|----------|----|-------|
| Control-M/Enterprise Manager | 2 zonal VMs (`ctm-em-a` zone 1, `ctm-em-b` zone 2), persistent data disk, shared DB | EM primary/secondary; LB health probe follows the active node | vertical (VM size) |
| Control-M/Server (one per stage) | 2 zonal VMs per Server (`ctm-srv-<stage>-a/b`), persistent data disk, own database | Server primary/secondary via Configuration Agent failover | vertical; add a stage by adding a map entry |
| Control-M/Agents (one pool per Server) | VM Scale Set across 3 zones, registered to the Server's host group | zone balanced, self-healing | autoscale on CPU, min/max per stage, Spot optional |
| Database | Azure Database for PostgreSQL Flexible Server | **ZoneRedundant** standby, automatic failover | SKU/storage per env, auto-grow |
| Shared filesystem | Azure Files Premium, **NFS 4.1**, ZRS | zone redundant | quota per env |
| Media/backups | Blob storage, RA-GZRS | geo redundant | — |
| Entry point | Standard Load Balancer (zone-redundant frontend) `ctm-web.<env>.ctm.internal` → 8443 | probes `/ControlM/` on both EM nodes | — |
| Secrets | Key Vault (RBAC, private endpoint), VM managed identity | — | — |
| Monitoring | Log Analytics + Azure Monitor Agent, metric alerts, Service Health | — | — |
| Backup | Recovery Services vault (GRS + cross-region restore in prod), daily VM + file share | — | — |
| DR (prod platform) | second region: PostgreSQL cross-region replica, **Site Recovery** replication of EM/Server VMs, replicated installers, warm agent scale set, optional Traffic Manager | — | — |

Node identity is stable: hostnames, persistent `/ctm` data disks and the
`ctm-db` / `ctm-web` DNS aliases are identical in the primary and DR regions,
so a failed-over node starts Control-M unchanged against the promoted database.
See `docs/ARCHITECTURE.md` for the diagram and `docs/DR-RUNBOOK.md` for failover.

## Platform matrix

| | nonprod | prod |
|---|---|---|
| Control-M/Servers | `CTM_DEV` (single VM), `CTM_QA` (HA pair) | `CTM_UAT` (HA pair), `CTM_PROD` (HA pair) |
| EM | single D4s_v5 | HA pair D16s_v5, ZRS disks |
| Server VM sizes | D4s_v5 | uat D8s_v5, prod D16s_v5, ZRS disks |
| PostgreSQL (one host, one DB per Server + EM) | D2ds_v5, zone-redundant, 7d | D8ds_v5, zone-redundant, geo-backup, 35d |
| Agents (min/max) | dev 1/2 Spot, qa 1/4 Spot | uat 2/6, prod 3/20 |
| Backup vault | ZRS, 14d | GRS + cross-region restore, 35d / 52w |
| DR region | — | replica + Site Recovery + 1 warm agent per Server |
| Locks / purge protection | — | yes |
| EM/Server VMs | 4 | 6 |

Everything above is in `environments/<platform>/terraform.tfvars`; the code is identical.
Moving a stage to its own platform later is a tfvars change (new root with one Server).

## Prerequisites

1. **Network** that already exists in each region (see `docs/NETWORK-REQUIREMENTS.md`):
   an app subnet, a subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers`,
   and (optionally) a private-endpoint subnet. Hub-managed private DNS zones can be
   passed in; missing ones are created in the platform resource group.
2. **BMC media** (EPD download) for Control-M/EM and Control-M/Server, plus the four
   silent-install XML files described in `scripts/silent-install/README.md`.
3. **Azure identity** running Terraform with `Contributor` + `User Access Administrator`
   (role assignments) on the subscription, and `Key Vault Secrets Officer` is granted
   automatically to the deployer.
4. Terraform ≥ 1.6, Azure CLI, (optional) tflint / pre-commit.

## Deploy

```bash
# 0. one-time state backend
terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply -var subscription_id=<sub> -var state_storage_account_name=<unique>
#    then put the storage account name into environments/*/backend.hcl

# 1. edit environments/nonprod/terraform.tfvars: subscription, network names, deployer IPs, emails
make init  ENV=nonprod
make plan  ENV=nonprod
make apply ENV=nonprod       # creates everything except the VM installs (they wait for media)

# 2. upload media + silent XML into the installers container printed by `terraform output`
az storage blob upload-batch --auth-mode login \
  --account-name <blob account> --destination installers --source ./media

# 3. the EM/Server VMs are already booting and retry the download; follow progress with
az serial-console connect ... # or: ssh via Bastion, then tail -f /var/log/ctm-bootstrap.log
```

Bootstrap order is enforced on the nodes themselves: the secondary EM waits for
the primary's web server, the secondary Server waits for the primary's
Configuration Agent, and agents wait for the Automation API behind the LB.

Promotion between environments is a pull request that changes only tfvars.
The **Azure DevOps pipeline** (`azure-pipelines.yml`, setup in
`docs/AZURE-DEVOPS.md`) runs fmt/validate/tflint, a Checkmarx One scan (SAST,
SCA, IaC), a plan per platform, then applies nonprod → prod on `main` behind
Azure DevOps environment approvals. Platform passwords come from
Delinea DevOps Secrets Vault and override the Terraform-generated ones. An
equivalent GitHub Actions workflow lives in `.github/workflows/terraform.yml`.

## What you still own after `apply`

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
  `modules/platform/templates/agent.sh.tftpl`.
* **Validate CLI syntax** of the `ctm provision` / `ctm config` commands in the
  agent template against `ctm provision -h` for your Control-M release.
* **DR drills** (`scripts/dr-failover.sh <env> --test`) at least quarterly.

## Operations

`docs/OPERATIONS.md` covers scaling, patching, rotating secrets, backups and
restoring. `docs/DR-RUNBOOK.md` covers regional failover and failback.
