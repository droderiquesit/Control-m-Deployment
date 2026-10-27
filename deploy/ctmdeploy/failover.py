# ---------------------------------------------------------------------------
# DR failover (primary region -> DR region), the SDK port of the retired
# scripts/dr-failover.sh: promote the PG replica, ASR unplanned failover of
# the EM/Server VMs, re-point ctm-db in the DR private zone, re-assert
# ctm-global at the DR gateway twin (access.appgw.dr_instance), scale the DR
# agent pools to each Server's agents.min read from config.yaml (no tfvars).
# ---------------------------------------------------------------------------
"""``ctmdeploy failover --env prod [--test] [--yes]``.

Steps (1-3 and 5 identical to the old runbook script):
  1. Promote the PostgreSQL cross-region replica to a standalone server.
  2. Fail over EM/Server VMs with Azure Site Recovery (latest recovery point).
  3. Point ctm-db.<env>.ctm.internal in the DR private zone at the promoted DB.
  4. Re-assert ctm-global at the DR gateway twin's private IP (only when
     ``access.appgw.dr_instance``): an idempotent upsert in the DR copy of the
     platform zone — the access phase already created that record — and a
     BEST-EFFORT upsert in the primary zone copy (skipped with a warning when
     the primary region is unreachable). The twin's IP is read LIVE from the
     gateway via the SDK (a GET against the DR-region resource, so it works
     during a primary-region loss) rather than derived from config — the live
     frontend is authoritative even if the bicep IP derivation ever changes.
  5. Scale each DR agent scale set to the Server's production minimum.
``--test`` runs a Site Recovery test failover only (no DB promotion, no DNS
change). Azure SDK imports are guarded inside the functions that need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ctmdeploy.config import EnvironmentConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

CONFIRM_PROMPT = "This promotes the DR database and breaks replication. Continue? [yes/NO] "

CHECKLIST = """
Failover issued. Next (see docs/DR-RUNBOOK.md):
  * wait for the ASR jobs, then 'systemctl status controlm-em controlm-server' on the failed-over VMs
  * commit the failover in Site Recovery once validated
  * re-point Traffic Manager / users to the DR load balancer if not automatic
  * run 'ctmdeploy deploy --env {env}' later to reconcile the platform (the replica is now standalone)
"""

FORWARDER_NOTE = """\
    note: corporate DNS forwarders that resolve *.ctm.internal through
    primary-region VNets (or a primary-region DNS Private Resolver) may need
    their forwarder target switched to a DR-region resolver before on-prem
    clients see ctm-global resolve to the DR gateway."""


class FailoverError(RuntimeError):
    """The failover could not be started."""


def _asr_client(cfg: EnvironmentConfig, credential: "TokenCredential") -> Any:
    from azure.mgmt.recoveryservicessiterecovery import SiteRecoveryManagementClient

    return SiteRecoveryManagementClient(
        credential,
        cfg.subscription_id,
        resource_group_name=cfg.dr_resource_group_name,
        resource_name=cfg.asr_vault_name,
    )


def _protected_item_names(client: Any, cfg: EnvironmentConfig) -> list[str]:
    """Names of the replicated EM/Server VMs, read from the vault itself."""
    items = client.replication_protected_items.list_by_replication_protection_containers(
        cfg.asr_fabric_primary, cfg.asr_container_primary
    )
    return [item.name for item in items]


def _recovery_network_id(cfg: EnvironmentConfig, item_name: str) -> str | None:
    """The DR VNet a protected item fails over into (v2 placement).

    Server nodes -> their ``dr.server_networks.<stage>`` VNet; everything
    else (the EM pair) -> the DR utility VNet.
    """
    if cfg.dr.network is None:
        return None
    node = item_name.removesuffix("-replication")
    for key, server in cfg.servers.items():
        if node in (server.primary_host, server.secondary_host):
            net = cfg.dr.server_networks.get(key)
            if net is None:
                return None
            return cfg.vnet_id(net.vnet_resource_group_name, net.vnet_name)
    utility = cfg.dr.network.utility
    return cfg.vnet_id(utility.vnet_resource_group_name, utility.vnet_name)


def _test_failover(client: Any, cfg: EnvironmentConfig) -> None:
    """Site Recovery test failover of every protected item (continue on error).

    Each item is tested into its own component's DR VNet (per-VNet mappings).
    """
    from azure.mgmt.recoveryservicessiterecovery import models

    names = _protected_item_names(client, cfg)
    if not names:
        print("    no replication-protected items found; run --phase replication first")
        return
    print("== TEST: running Site Recovery test failover only (no DB promotion, no DNS change)")
    for name in names:
        try:
            client.replication_protected_items.begin_test_failover(
                cfg.asr_fabric_primary,
                cfg.asr_container_primary,
                name,
                models.TestFailoverInput(
                    properties=models.TestFailoverInputProperties(
                        failover_direction="PrimaryToRecovery",
                        network_id=_recovery_network_id(cfg, name),
                        provider_specific_details=models.A2ATestFailoverInput(),
                    )
                ),
            ).result()
            print(f"    test failover issued: {name}")
        except Exception as exc:  # noqa: BLE001 - port of the script's `|| true`
            print(f"    test failover failed for {name} (continuing): {exc}")


def _promote_replica(cfg: EnvironmentConfig, credential: "TokenCredential") -> str:
    """Promote the PG replica to standalone (forced) and return its FQDN."""
    from azure.mgmt.postgresqlflexibleservers import PostgreSQLManagementClient
    from azure.mgmt.postgresqlflexibleservers import models as pg_models

    client = PostgreSQLManagementClient(credential, cfg.subscription_id)
    print("== 1/5 promoting PostgreSQL replica")
    client.servers.begin_update(
        cfg.dr_resource_group_name,
        cfg.replica_name,
        pg_models.ServerForUpdate(
            replica=pg_models.Replica(promote_mode="standalone", promote_option="forced")
        ),
    ).result()
    server = client.servers.get(cfg.dr_resource_group_name, cfg.replica_name)
    return server.fully_qualified_domain_name or cfg.replica_fqdn


def _unplanned_failover(client: Any, cfg: EnvironmentConfig) -> None:
    from azure.mgmt.recoveryservicessiterecovery import models

    names = _protected_item_names(client, cfg)
    if not names:
        raise FailoverError(
            "no replication-protected items found in "
            f"{cfg.asr_container_primary}; run `ctmdeploy deploy --phase replication` first"
        )
    print("== 2/5 Site Recovery unplanned failover of EM/Server VMs")
    pollers = []
    for name in names:
        pollers.append(
            (
                name,
                client.replication_protected_items.begin_unplanned_failover(
                    cfg.asr_fabric_primary,
                    cfg.asr_container_primary,
                    name,
                    models.UnplannedFailoverInput(
                        properties=models.UnplannedFailoverInputProperties(
                            failover_direction="PrimaryToRecovery",
                            source_site_operations="NotRequired",
                            provider_specific_details=models.A2AUnplannedFailoverInput(),
                        )
                    ),
                ),
            )
        )
    for name, poller in pollers:
        poller.result()
        print(f"    failed over: {name}")


def _upsert_db_cname(cfg: EnvironmentConfig, credential: "TokenCredential", fqdn: str) -> None:
    from azure.mgmt.privatedns import PrivateDnsManagementClient
    from azure.mgmt.privatedns import models as dns_models

    print(f"== 3/5 ensuring ctm-db.{cfg.private_dns_zone_name} -> {fqdn} in the DR private zone")
    client = PrivateDnsManagementClient(credential, cfg.subscription_id)
    client.record_sets.create_or_update(
        cfg.dr_resource_group_name,
        cfg.private_dns_zone_name,
        "CNAME",
        "ctm-db",
        dns_models.RecordSet(ttl=60, cname_record=dns_models.CnameRecord(cname=fqdn)),
    )


def _dr_appgw_private_ip(cfg: EnvironmentConfig, credential: "TokenCredential") -> str:
    """Private frontend IP of the DR gateway twin, read LIVE via the SDK.

    Robust path: the GET is an ARM read of a DR-region resource, so it keeps
    working while the primary region is down, and the live frontend IP is
    authoritative — unlike re-deriving cidrHost(prefix, 3) from the config
    plan, it cannot drift from what the gateway actually serves. Returns ''
    when the gateway does not exist (the access phase has not deployed the
    twin yet).
    """
    from azure.core.exceptions import ResourceNotFoundError
    from azure.mgmt.network import NetworkManagementClient

    client = NetworkManagementClient(credential, cfg.subscription_id)
    try:
        appgw = client.application_gateways.get(cfg.dr_resource_group_name, cfg.dr_appgw_name)
    except ResourceNotFoundError:
        return ""
    for frontend in appgw.frontend_ip_configurations or []:
        ip = getattr(frontend, "private_ip_address", None)
        if ip:
            return str(ip)
    return ""


def _upsert_ctm_global(cfg: EnvironmentConfig, credential: "TokenCredential") -> None:
    """Step 4/5: assert ctm-global -> the DR gateway twin's private IP.

    The access phase already created the record in the DR zone copy (it
    points at the twin from day one), so the DR upsert is an idempotent
    re-assert; the PRIMARY zone copy is updated best-effort only — clients
    still resolving through primary-region VNet links get moved to the DR
    gateway too, but an unreachable primary region must never fail the
    failover. Skipped (with a note) when the twin is not configured/deployed.
    """
    print("== 4/5 re-pointing ctm-global at the DR gateway twin")
    if not (
        cfg.environment == "prod"
        and cfg.access.appgw_enabled
        and cfg.access.appgw_dr_instance
    ):
        print(
            "    skipped: access.appgw.dr_instance is not enabled — no DR gateway "
            "twin; ctm-global keeps relying on the primary gateway's health probe"
        )
        return
    ip = _dr_appgw_private_ip(cfg, credential)
    if not ip:
        print(
            f"    skipped: DR gateway {cfg.dr_appgw_name} not found in "
            f"{cfg.dr_resource_group_name} (deploy `--phase access` to create the twin)"
        )
        return

    from azure.mgmt.privatedns import PrivateDnsManagementClient
    from azure.mgmt.privatedns import models as dns_models

    client = PrivateDnsManagementClient(credential, cfg.subscription_id)
    record = dns_models.RecordSet(
        ttl=60, a_records=[dns_models.ARecord(ipv4_address=ip)]
    )
    # DR zone copy: the authoritative entry for every DR-linked VNet
    # (idempotent — the access phase created this exact record).
    client.record_sets.create_or_update(
        cfg.dr_resource_group_name, cfg.private_dns_zone_name, "A", "ctm-global", record
    )
    print(f"    DR zone copy: ctm-global.{cfg.private_dns_zone_name} -> {ip}")
    # Primary zone copy: BEST-EFFORT (its resource group lives in the lost
    # region; private DNS is a global service but the write may still fail).
    try:
        client.record_sets.create_or_update(
            cfg.resource_group_name, cfg.private_dns_zone_name, "A", "ctm-global", record
        )
        print(f"    primary zone copy: ctm-global.{cfg.private_dns_zone_name} -> {ip}")
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        print(
            "    warning: could not update ctm-global in the PRIMARY zone copy "
            f"(continuing — expected while the primary region is unreachable): {exc}"
        )
    print(FORWARDER_NOTE)


def _scale_dr_agent_pools(cfg: EnvironmentConfig, credential: "TokenCredential") -> None:
    """Scale ctm-<env>-dr-agents-<stage> to agents.min from config (default 1).

    Only the DR-PARTICIPATING servers (``dr.server_networks``, the opt-in
    set) have warm pools in the DR region; the others are skipped.
    """
    from azure.mgmt.compute import ComputeManagementClient

    print("== 5/5 scaling DR agent pools (one per DR-protected Control-M/Server)")
    client = ComputeManagementClient(credential, cfg.subscription_id)
    for stage in cfg.dr_servers():
        server = cfg.servers[stage]
        capacity = server.agents.min if server.agents.min is not None else 1
        vmss_name = cfg.dr_agent_pool_name(stage)
        print(f"   {stage} -> {capacity}")
        vmss = client.virtual_machine_scale_sets.get(cfg.dr_resource_group_name, vmss_name)
        vmss.sku.capacity = capacity
        client.virtual_machine_scale_sets.begin_update(
            cfg.dr_resource_group_name,
            vmss_name,
            {"sku": {"name": vmss.sku.name, "tier": vmss.sku.tier, "capacity": capacity}},
        ).result()


def run(
    cfg: EnvironmentConfig,
    *,
    test: bool = False,
    yes: bool = False,
    credential: "TokenCredential | None" = None,
) -> int:
    """Execute the DR failover (or a test failover). Returns an exit code."""
    if not cfg.dr.enabled:
        raise FailoverError(f"dr.enabled is false for {cfg.environment}; nothing to fail over to")
    if not cfg.dr.enable_site_recovery:
        raise FailoverError("dr.enable_site_recovery is false; VMs are not replicated")

    if credential is None:
        from ctmdeploy.azure import get_credential

        credential = get_credential()

    print(
        f"== Control-M DR failover: env={cfg.environment} dr_rg={cfg.dr_resource_group_name} "
        f"replica={cfg.replica_name} mode={'test' if test else 'real'}"
    )
    asr = _asr_client(cfg, credential)

    if test:
        _test_failover(asr, cfg)
        return 0

    if not yes:
        answer = input(CONFIRM_PROMPT)
        if answer.strip() != "yes":
            print("aborted")
            return 1

    fqdn = _promote_replica(cfg, credential)
    _unplanned_failover(asr, cfg)
    _upsert_db_cname(cfg, credential, fqdn)
    _upsert_ctm_global(cfg, credential)
    _scale_dr_agent_pools(cfg, credential)
    print(CHECKLIST.format(env=cfg.environment))
    return 0
