# ---------------------------------------------------------------------------
# Azure Site Recovery enablement (A2A) via the SiteRecovery SDK: fabrics,
# protection containers, replication policy, container/network mappings and
# one replication-protected item per EM/Server VM. Idempotent — existing
# objects are skipped. Azure SDK imports are guarded inside the functions.
# ---------------------------------------------------------------------------
"""Enable cross-region VM replication for the Control-M platform.

Ported from the old ``modules/dr`` Site Recovery resources: fabrics
``ctm-<env>-fabric-primary/-dr``, containers ``ctm-<env>-container-primary/
-dr``, A2A policy ``ctm-<env>-asr-policy`` (24h recovery points, 60m
app-consistent snapshots), a container and a network mapping, and per-node
protected items replicating the OS + data disk through the cache storage
account to Premium_LRS targets; EM nodes also join the DR LB backend pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping

from ctmdeploy.config import EnvironmentConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

#: TF: recovery_point_retention_in_minutes = 24h * 60.
RECOVERY_POINT_RETENTION_MINUTES = 24 * 60
#: TF: application_consistent_snapshot_frequency_in_minutes.
APP_CONSISTENT_SNAPSHOT_MINUTES = 60
#: ASR default; the TF provider does not expose it.
CRASH_CONSISTENT_FREQUENCY_MINUTES = 5
#: Replica/target disk tier for failed-over nodes (TF managed_disk blocks).
TARGET_DISK_TYPE = "Premium_LRS"
#: Azure-to-Azure mappings hang off the well-known source network name.
AZURE_NETWORK_NAME = "azureNetwork"


class ReplicationError(RuntimeError):
    """Site Recovery enablement failed."""


def _require(node: Mapping[str, Any], key: str) -> str:
    value = node.get(key)
    if not value:
        raise ReplicationError(f"compute output node {node.get('name', '?')!r} is missing {key!r}")
    return str(value)


def _client(cfg: EnvironmentConfig, credential: "TokenCredential") -> Any:
    from azure.mgmt.recoveryservicessiterecovery import SiteRecoveryManagementClient

    return SiteRecoveryManagementClient(
        credential,
        cfg.subscription_id,
        resource_group_name=cfg.dr_resource_group_name,
        resource_name=cfg.asr_vault_name,
    )


def _exists(getter, *args) -> Any | None:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return getter(*args)
    except ResourceNotFoundError:
        return None


def ensure(
    cfg: EnvironmentConfig,
    nodes: Iterable[Mapping[str, Any]],
    *,
    primary_vnet_id: str,
    dr_vnet_id: str,
    cache_storage_account_id: str,
    dr_lb_backend_pool_id: str = "",
    credential: "TokenCredential | None" = None,
    wait: bool = False,
) -> None:
    """Idempotently enable A2A replication for every EM/Server node.

    ``nodes`` is the ``nodes`` output of the compute deployment (items with
    name, vmId, nicId, osDiskId, dataDiskId, zone, component). Pass
    ``wait=True`` to block on the long-running enable-protection jobs;
    otherwise they are started and left to complete in Azure.
    """
    if not (cfg.dr.enabled and cfg.dr.enable_site_recovery):
        print("    replication: site recovery is not enabled for this environment")
        return
    if cfg.dr.network is None:
        raise ReplicationError("dr.network must be configured to enable replication")

    if credential is None:
        from ctmdeploy.azure import get_credential

        credential = get_credential()

    from azure.mgmt.recoveryservicessiterecovery import models

    client = _client(cfg, credential)
    dr_resource_group_id = (
        f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.dr_resource_group_name}"
    )

    # -- 1. fabrics (one per region) ----------------------------------------
    for fabric_name, location in (
        (cfg.asr_fabric_primary, cfg.location),
        (cfg.asr_fabric_dr, cfg.dr.location),
    ):
        if _exists(client.replication_fabrics.get, fabric_name) is None:
            print(f"    creating fabric {fabric_name} ({location})")
            client.replication_fabrics.begin_create(
                fabric_name,
                models.FabricCreationInput(
                    properties=models.FabricCreationInputProperties(
                        custom_details=models.AzureFabricCreationInput(location=location)
                    )
                ),
            ).result()
        else:
            print(f"    fabric {fabric_name}: exists")

    # -- 2. protection containers --------------------------------------------
    containers = (
        (cfg.asr_fabric_primary, cfg.asr_container_primary),
        (cfg.asr_fabric_dr, cfg.asr_container_dr),
    )
    for fabric_name, container_name in containers:
        if _exists(client.replication_protection_containers.get, fabric_name, container_name) is None:
            print(f"    creating protection container {container_name}")
            client.replication_protection_containers.begin_create(
                fabric_name,
                container_name,
                models.CreateProtectionContainerInput(
                    properties=models.CreateProtectionContainerInputProperties(
                        provider_specific_input=[models.A2AContainerCreationInput()]
                    )
                ),
            ).result()
        else:
            print(f"    protection container {container_name}: exists")

    # -- 3. replication policy ------------------------------------------------
    policy = _exists(client.replication_policies.get, cfg.asr_policy_name)
    if policy is None:
        print(f"    creating replication policy {cfg.asr_policy_name}")
        policy = client.replication_policies.begin_create(
            cfg.asr_policy_name,
            models.CreatePolicyInput(
                properties=models.CreatePolicyInputProperties(
                    provider_specific_input=models.A2APolicyCreationInput(
                        recovery_point_history=RECOVERY_POINT_RETENTION_MINUTES,
                        app_consistent_frequency_in_minutes=APP_CONSISTENT_SNAPSHOT_MINUTES,
                        crash_consistent_frequency_in_minutes=CRASH_CONSISTENT_FREQUENCY_MINUTES,
                        multi_vm_sync_status="Disable",
                    )
                )
            ),
        ).result()
    else:
        print(f"    replication policy {cfg.asr_policy_name}: exists")

    # -- 4. container mapping --------------------------------------------------
    mapping_name = f"{cfg.name}-container-mapping"
    dr_container = client.replication_protection_containers.get(
        cfg.asr_fabric_dr, cfg.asr_container_dr
    )
    existing_mapping = _exists(
        client.replication_protection_container_mappings.get,
        cfg.asr_fabric_primary,
        cfg.asr_container_primary,
        mapping_name,
    )
    if existing_mapping is None:
        print(f"    creating container mapping {mapping_name}")
        client.replication_protection_container_mappings.begin_create(
            cfg.asr_fabric_primary,
            cfg.asr_container_primary,
            mapping_name,
            models.CreateProtectionContainerMappingInput(
                properties=models.CreateProtectionContainerMappingInputProperties(
                    target_protection_container_id=dr_container.id,
                    policy_id=policy.id,
                )
            ),
        ).result()
    else:
        print(f"    container mapping {mapping_name}: exists")

    # -- 5. network mapping (primary vnet -> dr vnet) ---------------------------
    network_mapping_name = f"{cfg.name}-network-mapping"
    existing_network_mapping = _exists(
        client.replication_network_mappings.get,
        cfg.asr_fabric_primary,
        AZURE_NETWORK_NAME,
        network_mapping_name,
    )
    if existing_network_mapping is None:
        print(f"    creating network mapping {network_mapping_name}")
        client.replication_network_mappings.begin_create(
            cfg.asr_fabric_primary,
            AZURE_NETWORK_NAME,
            network_mapping_name,
            models.CreateNetworkMappingInput(
                properties=models.CreateNetworkMappingInputProperties(
                    recovery_fabric_name=cfg.asr_fabric_dr,
                    recovery_network_id=dr_vnet_id,
                    fabric_specific_details=models.AzureToAzureCreateNetworkMappingInput(
                        primary_network_id=primary_vnet_id
                    ),
                )
            ),
        ).result()
    else:
        print(f"    network mapping {network_mapping_name}: exists")

    # -- 6. protected items (one per EM/Server VM) ------------------------------
    app_subnet = cfg.dr.network.app_subnet_name
    pollers: list[tuple[str, Any]] = []
    em_nodes: list[Mapping[str, Any]] = []
    for node in nodes:
        name = _require(node, "name")
        item_name = f"{name}-replication"
        if node.get("component") == "em":
            em_nodes.append(node)
        existing = _exists(
            client.replication_protected_items.get,
            cfg.asr_fabric_primary,
            cfg.asr_container_primary,
            item_name,
        )
        if existing is not None:
            print(f"    protected item {item_name}: exists")
            continue
        print(f"    enabling replication for {name}")
        disks = [
            models.A2AVmManagedDiskInputDetails(
                disk_id=_require(node, disk_key),
                primary_staging_azure_storage_account_id=cache_storage_account_id,
                recovery_resource_group_id=dr_resource_group_id,
                recovery_target_disk_account_type=TARGET_DISK_TYPE,
                recovery_replica_disk_account_type=TARGET_DISK_TYPE,
            )
            for disk_key in ("osDiskId", "dataDiskId")
        ]
        poller = client.replication_protected_items.begin_create(
            cfg.asr_fabric_primary,
            cfg.asr_container_primary,
            item_name,
            models.EnableProtectionInput(
                properties=models.EnableProtectionInputProperties(
                    policy_id=policy.id,
                    provider_specific_details=models.A2AEnableProtectionInput(
                        fabric_object_id=_require(node, "vmId"),
                        recovery_container_id=dr_container.id,
                        recovery_resource_group_id=dr_resource_group_id,
                        recovery_availability_zone=str(node.get("zone", "")) or None,
                        recovery_azure_network_id=dr_vnet_id,
                        recovery_subnet_name=app_subnet,
                        vm_managed_disks=disks,
                    ),
                )
            ),
        )
        pollers.append((item_name, poller))

    if pollers and not wait:
        print(
            "    replication jobs started for: "
            + ", ".join(name for name, _ in pollers)
            + " (re-run with --wait to block until protected)"
        )
    for item_name, poller in pollers:
        if wait:
            poller.result()
            print(f"    protected item {item_name}: protection enabled")

    # -- 7. EM nodes: recovery NIC joins the DR LB backend pool -----------------
    if dr_lb_backend_pool_id and em_nodes:
        if pollers and not wait:
            print(
                "    EM LB pool assignment deferred: items are still enabling; "
                "re-run `ctmdeploy deploy --phase replication --wait` to apply it"
            )
        else:
            for node in em_nodes:
                _ensure_em_lb_pool(
                    client, models, cfg, node, app_subnet, dr_vnet_id, dr_lb_backend_pool_id
                )


def _ensure_em_lb_pool(
    client: Any,
    models: Any,
    cfg: EnvironmentConfig,
    node: Mapping[str, Any],
    app_subnet: str,
    dr_vnet_id: str,
    pool_id: str,
) -> None:
    """Point the failed-over EM NIC at the DR load balancer backend pool.

    TF equivalent: network_interface.recovery_load_balancer_backend_address_
    pool_ids on azurerm_site_recovery_replicated_vm for component == 'em'.
    Best-effort: NIC details only exist once initial replication has synced.
    """
    item_name = f"{_require(node, 'name')}-replication"
    try:
        item = client.replication_protected_items.get(
            cfg.asr_fabric_primary, cfg.asr_container_primary, item_name
        )
        details = getattr(item.properties, "provider_specific_details", None)
        vm_nics = list(getattr(details, "vm_nics", None) or [])
        if not vm_nics:
            print(f"    {item_name}: NIC details not synced yet; LB pool assignment deferred")
            return
        nic_inputs = []
        for nic in vm_nics:
            ip_configs = [
                models.IPConfigInputDetails(
                    ip_config_name=getattr(ip_config, "name", None) or "primary",
                    is_primary=True,
                    recovery_subnet_name=app_subnet,
                    recovery_lb_backend_address_pool_ids=[pool_id],
                )
                for ip_config in (getattr(nic, "ip_configs", None) or [None])[:1]
            ]
            nic_inputs.append(
                models.VMNicInputDetails(
                    nic_id=getattr(nic, "nic_id", None) or _require(node, "nicId"),
                    ip_configs=ip_configs,
                    selection_type="SelectedByUser",
                )
            )
        client.replication_protected_items.begin_update(
            cfg.asr_fabric_primary,
            cfg.asr_container_primary,
            item_name,
            models.UpdateReplicationProtectedItemInput(
                properties=models.UpdateReplicationProtectedItemInputProperties(
                    selected_recovery_azure_network_id=dr_vnet_id,
                    vm_nics=nic_inputs,
                    provider_specific_details=models.A2AUpdateReplicationProtectedItemInput(),
                )
            ),
        ).result()
        print(f"    {item_name}: recovery NIC joined the DR LB backend pool")
    except Exception as exc:  # noqa: BLE001 - best-effort post-sync step
        print(f"    {item_name}: could not set LB pool yet ({exc}); re-run replication later")
