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

OS-level content (packages, tuning, Node.js) is **baked** into the ctm-rhel9
golden image, so OS drift is fixed by revving the image, not by touching
nodes:

1. Change `packer/scripts/*` if needed (a plain rebuild picks up newer RHEL
   package content anyway).
2. `python -m ctmdeploy image build --env prod --version <x.y.z>` — publishes
   an immutable gallery version replicated to both regions (or run the
   pipeline with `runImageBuild=true`).
3. Bump `image.image_version` in `environments/<env>/config.yaml`
   (keep `latest` in nonprod if you prefer; pin prod).
4. `python -m ctmdeploy whatif --env <env>` then
   `deploy --env <env> --phase compute`. EM/Server VMs are re-created on the
   new image and reattach their persistent data disks; agent scale set
   instances are replaced rather than patched (`az vmss update-instances`
   rolls them onto the new model).

## Secrets rotation

Passwords live in Key Vault and in the database/EM. Seeding is idempotent —
`ctmdeploy` never rotates an existing secret silently. To rotate:

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
the same `ctm-srv-qa-b-data` disk (attach, not create); bootstrap detects
`/ctm/<user>/.ctm_installed` and only recreates the systemd units. The same
flow rebuilds a node onto a new image version after a bump of
`image.image_version`.

## Destroying an environment

Prod has a `CanNotDelete` lock and Key Vault purge protection. Remove the lock
first (`resource_lock: false` in config, then
`python -m ctmdeploy deploy --env prod --phase rg`). There is no destroy
command in a stateless toolchain — delete the resource groups explicitly:
`az group delete -n ctm-<env>-rg` (and `ctm-<env>-dr-rg` where DR is enabled).
Soft-deleted Key Vaults and Recovery Services vaults with backup items must be
cleaned up manually.
