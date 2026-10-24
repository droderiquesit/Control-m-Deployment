# Disaster recovery runbook

Applies to platforms with `dr.enabled: true` (the prod platform, which hosts
`CTM_UAT` and `CTM_PROD`).

**DR scope is opt-in per Server**: the EM is always protected, and a Server
participates if and only if it has a `dr.server_networks.<stage>` entry. In
the shipped prod config **only `CTM_PROD` is DR-protected** — 4 Site Recovery
protected VMs (`ctm-em-a/b`, `ctm-srv-prod-a/b`) and one warm agent pool
(`ctm-prod-dr-agents-prod`). `CTM_UAT` is re-deployable and not
business-critical: no uat DR VNet, no uat ASR items, no uat warm agents. The
shared PostgreSQL replica still carries **every** database (`ctm_uat`
included) because replication is server-level, so UAT *data* survives a
regional failover even though UAT compute is not replicated.

| Objective | Target | Mechanism |
|-----------|--------|-----------|
| RPO database | seconds to low minutes | PostgreSQL cross-region read replica (async) |
| RPO EM/Server hosts | minutes | Site Recovery continuous disk replication, app-consistent snapshot hourly |
| RTO | 30-60 minutes | scripted promotion + ASR failover + DNS alias |

## Standing posture

* **Primary**: full platform. **DR**: foundation + replica + ASR replicas of
  the EM and the DR-protected Servers + warm agents per DR-protected Server
  (`dr.warm_agents`).
* **Site Recovery network mappings are per VNet pair**, mirroring the
  hub-and-spoke topology: the utility VNet maps to the DR utility VNet
  (`vnet-prod-utility` → `vnet-dr-utility`) and each DR-protected server VNet
  maps to its `dr.server_networks.<stage>` VNet (`vnet-ctm-prod` →
  `vnet-ctm-prod-dr`). Failed-over NICs land in the matching subnet role:
  EM VMs in the DR utility em subnet, Server VMs in their DR server subnet.
  Adding a Server to DR means adding its DR VNet + `dr.server_networks` entry
  and re-running `--phase dr` and `--phase replication`.
* Secrets, DNS aliases (`ctm-db`, `ctm-web`), hostnames and the NFS layout are
  identical in both regions. Both regions boot from the same ctm-rhel9 gallery
  image (versions are replicated to the DR region at publish time).
* **Private entry (`ctm-global`)**: with `access.appgw.dr_instance` (the
  shipped prod config) each region runs its own private Application Gateway —
  `ctm-prod-appgw` and the DR twin `ctm-prod-dr-appgw` — both balancing the
  same two internal LBs, and each regional copy of `prod.ctm.internal`
  carries a `ctm-global` record pointing at its **local** gateway. The DR
  entry therefore already works before any failover step and survives a
  primary-region loss.
* Installer media and EM/Server backup exports are object-replicated to the DR
  blob account; the DR Files share is empty until failover (backups restore
  from blob if required).

## Test failover (quarterly, no production impact)

```bash
python -m ctmdeploy failover --env prod --test
```

Site Recovery brings test copies of the protected VMs up in their mapped DR
VNets — EM in the DR utility VNet, `ctm-srv-prod-a/b` in `vnet-ctm-prod-dr`
(or in an isolated test network if you configure one in the vault). Validate boot,
`/ctm` mounts and that the services start; then **clean up the test failover**
in the Azure portal. The database replica is not touched.

## Real failover

1. Declare the incident; confirm the primary region (or the database) is
   unavailable and not just degraded. Promotion is one-way.
2. Run the scripted failover (it asks for confirmation; `--yes` skips it):
   ```bash
   python -m ctmdeploy failover --env prod
   ```
   It promotes the replica (forced, standalone), starts an unplanned ASR
   failover for the protected VMs — `ctm-em-a/b` and the DR-protected Servers'
   nodes (`ctm-srv-prod-a/b`; `CTM_UAT` is not replicated) — sets the DR
   `ctm-db` CNAME to the promoted server, **re-asserts `ctm-global`** at the
   DR gateway twin's private IP (read live from `ctm-prod-dr-appgw` via the
   SDK: an idempotent upsert in the DR zone copy, which the access phase
   already pointed at the twin, and a *best-effort* upsert in the primary
   zone copy — skipped with a warning when the primary region is
   unreachable), and scales each DR-protected Server's warm agent scale set
   (`ctm-prod-dr-agents-prod`) to its configured `agents.min` from
   `environments/prod/config.yaml`. It ends by printing the manual checklist
   (steps 3-7 below) and a reminder that **corporate DNS forwarders resolving
   `*.ctm.internal` through primary-region VNets may need their forwarder
   target switched** to a DR-region resolver.
3. When the ASR jobs finish, log in to `ctm-em-a` (DR) and check
   `systemctl status controlm-em`, and `controlm-server` on `ctm-srv-prod-a`.
   The services start automatically; the `ctm-db` alias resolves to the
   promoted database so no Control-M reconfiguration is needed.
4. In Control-M Configuration Manager verify `CTM_PROD` is connected and the
   DR agents registered into its host groups. `CTM_UAT` shows disconnected —
   its compute was not replicated (see "UAT during a DR event" below). Agents
   that were running in the primary region are gone; re-run or resubmit jobs
   per business policy.
5. Switch users: clients on `ctm-global.prod.ctm.internal` whose VNets
   resolve through the **DR** zone copy need no change — the DR gateway twin
   answers and routes to the failed-over EM. Check corporate DNS forwarders:
   any that resolve `*.ctm.internal` through primary-region VNets (or a
   primary-region DNS Private Resolver) must have their forwarder target
   switched to a DR-region resolver. Traffic Manager handles public endpoints
   automatically; for internal access still pinned to `ctm-web`, update the
   corporate DNS record for Control-M Web to the DR load balancer IP:
   ```bash
   az deployment group show -g ctm-prod-dr-rg -n ctm-prod-foundation-dr \
     --query properties.outputs.lbFrontendIp.value -o tsv
   ```
6. **Commit** the failover in Site Recovery once validated.
7. Re-point backups: enable the Recovery Services vault in the DR platform
   (`backup.enabled` for the DR role) if the outage will be long.

## UAT during a DR event

`CTM_UAT` has no replicated compute, by design. Its data (`ctm_uat` database,
object-replicated installers and backup exports) is in the DR region, so
nothing is lost. If the outage is long enough that acceptance testing must
resume before failback, rebuild UAT compute in the DR region: in the config
revision that makes the DR region the acting primary (the same change failback
step 4 describes), point `servers.uat.network` at a uat VNet in the DR region
(peered to `vnet-dr-utility`) and deploy the compute phase — the fresh
`ctm-srv-uat-a/b` nodes install and start against the promoted database, which
already holds the UAT data. Otherwise simply wait for failback. (To make UAT
*replicated* going forward instead, give it a DR VNet and a
`dr.server_networks.uat` entry, then re-run `--phase dr` and
`--phase replication`.)

## Failback

1. When the primary region is healthy, re-run
   `python -m ctmdeploy whatif --env prod` to see what the deployment would
   change. The promoted replica is standalone; the deployment recreates the
   primary database only if the old server is gone. Recommended path: create
   a new read replica *from DR back to primary* (`az postgres flexible-server
   replica create`), wait for sync, then promote it during a maintenance window.
2. Re-protect the VMs in Site Recovery in the reverse direction
   (`ctmdeploy deploy --env prod --phase replication` re-creates protection
   once direction is restored) and fail back during the same window.
3. Restore the `ctm-db` CNAME in the primary zone and switch Traffic Manager /
   corporate DNS (forwarder targets included) back. If the failover's
   best-effort write reached the primary zone copy, its `ctm-global` record
   now points at the DR twin — re-running `--phase access` (included in step
   4's `--phase all`) re-asserts it back to the primary gateway.
4. Update `environments/prod/config.yaml` to reflect the new primary and run
   `python -m ctmdeploy deploy --env prod --phase all`, so drift is zero again.

## Things that are NOT automatic

* Promotion of the replica (deliberately manual — it is irreversible).
* Agent re-registration for agents that were in the primary region.
* Anything UAT: its compute is not DR-protected (only its data is, via the
  shared replica); rebuilding it in the DR region is a config + deploy step.
* Jobs in flight at the time of failure (Control-M will show them per the
  Server's recovery rules once the Server starts against the promoted DB).
