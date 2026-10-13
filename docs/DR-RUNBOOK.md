# Disaster recovery runbook

Applies to environments with `dr.enabled = true` (uat, prod).

| Objective | Target | Mechanism |
|-----------|--------|-----------|
| RPO database | seconds to low minutes | PostgreSQL cross-region read replica (async) |
| RPO EM/Server hosts | minutes | Site Recovery continuous disk replication, app-consistent snapshot hourly |
| RTO | 30-60 minutes | scripted promotion + ASR failover + DNS alias |

## Standing posture

* **Primary**: full platform. **DR**: foundation + replica + ASR replicas +
  warm agents (`dr.warm_agents`, 0 in uat, 1 in prod).
* Secrets, DNS aliases (`ctm-db`, `ctm-web`), hostnames and the NFS layout are
  identical in both regions.
* Installer media and EM/Server backup exports are object-replicated to the DR
  blob account; the DR Files share is empty until failover (backups restore
  from blob if required).

## Test failover (quarterly, no production impact)

```bash
./scripts/dr-failover.sh prod --test
```

Site Recovery brings test copies of the four VMs up in the DR VNet (in an
isolated test network if you configure one in the vault). Validate boot,
`/ctm` mounts and that the services start; then **clean up the test failover**
in the Azure portal. The database replica is not touched.

## Real failover

1. Declare the incident; confirm the primary region (or the database) is
   unavailable and not just degraded. Promotion is one-way.
2. Run the scripted failover:
   ```bash
   ./scripts/dr-failover.sh prod
   ```
   It promotes the replica (`forced`, standalone), starts an unplanned ASR
   failover for `ctm-em-a/b` and `ctm-srv-a/b`, sets the DR `ctm-db` CNAME to
   the promoted server and scales the DR agent scale set to the production
   minimum.
3. When the ASR jobs finish, log in to `ctm-em-a` (DR) and check
   `systemctl status controlm-em` and on `ctm-srv-a` `controlm-server`. The
   services start automatically; the `ctm-db` alias resolves to the promoted
   database so no Control-M reconfiguration is needed.
4. In Control-M Configuration Manager verify the Server is connected and the
   DR agents registered into the host group. Agents that were running in the
   primary region are gone; re-run or resubmit jobs per business policy.
5. Switch users: Traffic Manager does this automatically for public endpoints;
   for internal access update the corporate DNS record for Control-M Web to the
   DR load balancer IP (`terraform output -json | jq .dr`).
6. **Commit** the failover in Site Recovery once validated.
7. Re-point backups: enable the Recovery Services vault in the DR platform
   (`backup.enabled` for the DR role) if the outage will be long.

## Failback

1. When the primary region is healthy, run `terraform apply` for the
   environment. The promoted replica is standalone; Terraform recreates the
   primary database only if you remove the old server. Recommended path: create
   a new read replica *from DR back to primary* (`az postgres flexible-server
   replica create`), wait for sync, then promote it during a maintenance window.
2. Re-protect the VMs in Site Recovery in the reverse direction and fail back
   during the same window.
3. Restore the `ctm-db` CNAME in the primary zone and switch Traffic Manager /
   corporate DNS back.
4. Update `terraform.tfvars`/state to reflect the new primary and apply, so
   drift is zero again.

## Things that are NOT automatic

* Promotion of the replica (deliberately manual — it is irreversible).
* Agent re-registration for agents that were in the primary region.
* Jobs in flight at the time of failure (Control-M will show them per the
  Server's recovery rules once the Server starts against the promoted DB).
