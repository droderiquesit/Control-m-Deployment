# Operations

## Scaling

| Need | Change | Effect |
|------|--------|--------|
| More job throughput for a stage | `servers.<stage>.agents.max` / `.min` / `.scale_out_cpu` | autoscale bounds; no downtime |
| Different agent size | `servers.<stage>.agents.vm_size` | scale set model updates; roll instances (`az vmss update-instances`) |
| Bigger EM / Server | `em.vm_size` / `servers.<stage>.vm_size` | VM resize = reboot, do secondary first then fail over |
| New stage on a platform | add a `servers.<stage>` entry | new Server VM pair, database, secret, agent pool; register in EM afterwards |
| Database compute | `database.sku_name` | scale with brief failover to standby (zone-redundant HA) |
| Database storage | `database.storage_mb` | online grow (auto-grow is on) |
| Shared share | `storage.shared_share_quota_gb` | online |

## Patching

EM/Server VMs use `patch_mode = AutomaticByPlatform`; schedule a maintenance
configuration in Azure Update Manager for the EM/Server pair so that the
secondary patches first and a Control-M failover happens before the primary.
Agent scale set instances are replaced rather than patched: bump the image
version (or pin `image.version`), then `az vmss update-instances`.

## Secrets rotation

Passwords live in Key Vault and in the database/EM. To rotate:

1. `terraform taint random_password.<name>` and `terraform apply` writes the new
   value to every region's Key Vault.
2. Change the password in PostgreSQL (`ALTER ROLE`) and in Control-M
   (`ctm_menu` / `em` password utilities) on the primary nodes.
3. Restart the secondary nodes so they pick up the new secret at next bootstrap
   of the service (`systemctl restart controlm-*`).

The SSH private key is the `ssh-private-key` secret; rotate by tainting
`tls_private_key.ssh` (requires VM redeploy or manual authorized_keys update).

## Backups and restore

* **Database**: automated backups (7-35 days per platform) + geo-redundant copies in
  uat/prod. Point-in-time restore creates a new server; update the `ctm-db`
  CNAME (`modules/dns`) or swap the server name and apply.
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
install, `terraform taint 'module.platform.module.server_node["ctm-srv-qa-b"].azurerm_linux_virtual_machine.this'`
and apply: the new VM attaches the same disk, bootstrap detects
`/ctm/<user>/.ctm_installed` and only recreates the systemd units.

## Destroying an environment

Prod has a `CanNotDelete` lock and Key Vault purge protection. Remove the lock
(`resource_lock = false`, apply), then `make destroy ENV=prod`. Soft-deleted
Key Vaults and Recovery Services vaults with backup items must be cleaned up
manually.
