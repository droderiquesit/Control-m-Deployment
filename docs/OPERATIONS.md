# Operations

All platform changes flow the same way: edit
`environments/<env>/config.yaml`, review with
`python -m ctmdeploy whatif --env <env>`, then
`python -m ctmdeploy deploy --env <env> --phase <phase>` (or merge to `main`
and let the pipeline run). Deployments are stateless ARM operations — safe to
re-run, nothing to lock or migrate (see the Idempotency section in the README
for the exact re-run guarantees).

## Scaling

| Need | Change | Effect |
|------|--------|--------|
| More job throughput for a stage | `servers.<stage>.agents.max` / `.min` / `.scale_out_cpu` | autoscale bounds; no downtime (`--phase compute`) |
| Different agent size | `servers.<stage>.agents.vm_size` | scale set model updates; roll instances (`az vmss update-instances`) |
| Bigger EM / Server | `em.vm_size` / `servers.<stage>.vm_size` | VM resize = reboot, do secondary first then fail over |
| New stage on a platform | add a `servers.<stage>` entry incl. its `network:` block (a new VNet with server + agents subnets, peered to the utility VNet — see `docs/NETWORK-REQUIREMENTS.md`) | new Server VM pair, database, secret, agent pool in its own VNet; register in EM afterwards |
| Relocate a stage's agents | `servers.<stage>.network.agents_subnet_name` or `agents_vnet_name`/`agents_vnet_resource_group_name` | drain/deregister the agents, delete the old scale set (`az vmss delete` — Azure cannot move a VMSS between VNets and Incremental mode never deletes), then `--phase compute` creates it in the new subnet |
| Database compute | `database.sku_name` | scale with brief failover to standby (zone-redundant HA) |
| Database storage | `database.storage_mb` | online grow (auto-grow is on) |
| Shared share | `storage.shared_share_quota_gb` | online |
| Secret-sync cadence | `kv_sync.schedule` (6-field NCRONTAB) | `--phase access` updates the app setting; next tick uses it |
| Bigger/more build agents | `build_agents.vm_size` / `instances` | `--phase access`; capacity itself is owned by the ADO elastic pool — never resize the VMSS by hand |

> **PostgreSQL zones after an HA failover.** After an Azure HA failover the
> primary and standby swap availability zones. `ctmdeploy` reads the live
> server's current zone / standby zone before each deploy and passes them as
> parameters, so a re-deploy (and `whatif`) accepts the swapped layout instead
> of fighting it — no zone drift, no surprise failover back. Zones `1`/`2`
> are only used on first creation. If you *want* to swap the roles back, do it
> with a forced HA failover in Azure (portal / `az postgres flexible-server`),
> not through the deployment.

## Patching and image revisions

EM/Server VMs use `patch_mode = AutomaticByPlatform`; schedule a maintenance
configuration in Azure Update Manager for the EM/Server pair so that the
secondary patches first and a Control-M failover happens before the primary.

OS-level content (packages, tuning, Node.js) is **baked** into the two
golden images (v4.1: `ctm-rhel9` for EM/Server, `ctm-agent` — which also
embeds the Control-M/Agent — for the scale sets), so OS drift is fixed by
revving the images, not by touching nodes:

1. Change `packer/scripts/*` if needed (a plain rebuild picks up newer RHEL
   package content anyway). An **agent software upgrade** is the same flow
   with a new `control-m-agent` package version feeding the agent build.
2. `python -m ctmdeploy image build --env prod --version <x.y.z>` (base) and
   `… --image agent --media-path <dir> --version <x.y.z>` (agent) — each
   publishes an immutable gallery version replicated to both regions (or run
   the pipeline with `runImageBuild=true` + `imageKind`).
3. Bump `image.base.version` / `image.agent.version` in
   `environments/<env>/config.yaml` (keep `latest` in nonprod if you prefer;
   pin prod).
4. `python -m ctmdeploy whatif --env <env>` then
   `deploy --env <env> --phase compute`. EM/Server VMs are re-created on the
   new base image and reattach their persistent data disks. **Agent image
   revisions roll via the VMSS model update**: the deployment updates each
   scale set's model to the new `ctm-agent` version, new/scaled-out
   instances boot it immediately, and existing instances keep the old one
   until recycled — `az vmss update-instances --instance-ids '*'` (or
   scale-in/out; agents deregister gracefully) converges the fleet.

## Operator access (Bastion + ctm-global)

* **SSH**: no VM has a public IP; sessions go through Azure Bastion
  (`access.bastion.enabled`, the access phase):

  ```bash
  az keyvault secret show --vault-name ctm-<env>-kv-<suffix> \
    --name ssh-private-key --query value -o tsv > ctmops.pem && chmod 600 ctmops.pem
  az network bastion ssh --name ctm-<env>-bastion -g ctm-<env>-rg \
    --target-ip-address <private IP> --auth-type ssh-key --username ctmops \
    --ssh-key ./ctmops.pem
  ```

  DR nodes and the build agents are reachable through the same Bastion (the
  prod ⇄ DR global peering and the build peerings carry the session; the
  NSGs admit SSH only from `bastion_cidrs`).
* **Control-M Web / Automation API**: `ctm-web.<env>.ctm.internal` is the
  region-local entry; on prod, **`ctm-global.prod.ctm.internal`** (the
  private Application Gateway) follows whichever region's EM answers the TCP
  health probe — use it as the client-facing default so a DR failover needs
  no client change. The gateway is layer-4: TLS stays end-to-end, so
  certificate work happens on the EM web tier only.

## Key Vault → Control-M sync (kvsync)

* **Add a job secret**: tag it (`az keyvault secret set ... --tags
  ctm-sync=true [ctm-name=<CTM name>]`); it appears in Control-M within one
  `kv_sync.schedule` tick (default 15 min). Rotation = set a new value, wait
  one tick. Deleting in Key Vault does **not** delete in Control-M.
* **Change the cadence**: edit `kv_sync.schedule`, run `--phase access`.
* **Ship function-code changes**: `python -m ctmdeploy kvsync deploy --env
  <env>` (idempotent zip-deploy; infra untouched).
* **Watch it**: Application Insights is not wired by default — use the
  Function App's log stream / `report` activity summaries (adds/updates/
  failures; never values). Failures usually mean the EM API is down or
  `em-admin-password` rotated in EM but not in the vault.
* **Cost**: the EP1 plan bills a warm instance around the clock; disable
  `kv_sync.enabled` in nonprod if the rehearsal is not worth it.

## Build agents (Azure DevOps elastic pool)

* Capacity is owned by the elastic pool (Organization settings → Agent
  pools); the deployment ships Manual upgrade/no autoscale on purpose. The
  pool recreates instances from the current VMSS model. Note that re-running
  `--phase access` re-sends `build_agents.instances` as `sku.capacity` and
  resets the live instance count to that baseline (the pool then re-scales);
  avoid re-runs during build-heavy windows.
* **Tooling updates** (python/bicep pins in the cloud-init of
  `bicep/modules/build-agents.bicep`): deploy `--phase access`, then let the
  pool recycle instances (or reimage them) — existing instances keep the old
  bootstrap.
* Switching the pipeline on/off the pool is one variable:
  `agentPoolName` in `pipelines/variables/common.yml`
  (`docs/AZURE-DEVOPS.md` §6).

## Secrets rotation

Passwords live in Key Vault and in the database/EM. Seeding is idempotent —
`ctmdeploy` never rotates an existing secret silently. (Job secrets tagged
`ctm-sync` are simpler: set the new value in Key Vault and the kvsync
function propagates it — see above.) To rotate the platform secrets:

1. Write the new value into **every region's** Key Vault. Either directly
   (`az keyvault secret set --vault-name <kv> --name <secret> --value ...` in
   primary and DR) or through the toolchain:
   `CTM_PLATFORM_SECRETS='{"<secret-name>":"<new value>"}' python -m ctmdeploy secrets --env <env>`
   (explicit overrides are applied even when the secret already exists). When
   the pipeline owns secrets, change the value in Delinea DSV instead — the
   next run seeds it the same way.
2. Change the password in PostgreSQL (`ALTER ROLE`) and in Control-M
   (`ctm_menu` / `em` password utilities) on the primary nodes.
3. Restart the secondary nodes so they pick up the new secret at next bootstrap
   of the service (`systemctl restart controlm-*`).

The SSH private key is the `ssh-private-key` secret; rotate it by seeding a
new PEM the same way (requires VM redeploy or manual `authorized_keys`
update, since the public key is baked into the VM configuration at deploy).

## Backups and restore

* **Database**: automated backups (7-35 days per platform) + geo-redundant copies in
  uat/prod. Point-in-time restore creates a new server; update the `ctm-db`
  CNAME (dns module / `az network private-dns record-set cname set-record`)
  or swap the server name in config and deploy.
* **VM**: nightly Recovery Services vault backups of EM/Server. Restore disk or
  full VM into the same subnet; hostnames are preserved.
* **Control-M metadata**: `/usr/local/sbin/ctm-em-backup.sh` runs nightly on EM
  nodes, writes to the NFS share and copies to the `backups` blob container
  (object-replicated to DR).
* **Files share**: daily share snapshots via the vault.

## Observability

* Logs: `/var/log/ctm-bootstrap.log` on every node, forwarded as syslog to Log
  Analytics (`Syslog` table, `ProcessName == "ctm-bootstrap"`).
* Alerts: LB probe availability (Control-M Web down), PostgreSQL CPU, storage,
  memory, failed connections, availability, VM availability for each node,
  Azure Service Health for the region. Recipients in `observability.alert_emails`.
* Add Control-M specific checks with the Automation API (`ctm run status`) as
  Azure Monitor custom log collection if you need job-level SLOs.

## Rebuilding a node

The persistent data disk holds the installation. To replace a VM but keep the
install, delete only the VM resource and redeploy the compute phase:

```bash
az vm delete -g ctm-<env>-rg -n ctm-srv-qa-b --yes
python -m ctmdeploy deploy --env <env> --phase compute
```

The deployment re-creates the VM from the current gallery image and attaches
the same `ctm-srv-qa-b-data` disk (attach, not create); the node installer
detects `/ctm/<user>/.ctm_installed` and only recreates the systemd units —
no secrets are re-read, nothing is reinstalled. The same flow rebuilds a node
onto a new image version after a bump of `image.base.version`.

A node that got stuck mid-install needs no rebuild at all: the installer is
an idempotent stage machine (`/var/lib/ctm/install-state.json` + real-state
checks), so fix the cause (missing media blob, closed port) and re-run it —
`systemctl start ctm-install.service` — then watch
`/var/log/ctm-bootstrap.log`.

## Destroying an environment

Prod has a `CanNotDelete` lock and Key Vault purge protection. Remove the lock
first (`resource_lock: false` in config, then
`python -m ctmdeploy deploy --env prod --phase rg`). There is no destroy
command in a stateless toolchain — delete the resource groups explicitly:
`az group delete -n ctm-<env>-rg` (and `ctm-<env>-dr-rg` where DR is
enabled, plus `ctm-build-rg` on prod — remove the ADO elastic pool first so
Azure DevOps stops recreating instances).
Soft-deleted Key Vaults and Recovery Services vaults with backup items must be
cleaned up manually.
