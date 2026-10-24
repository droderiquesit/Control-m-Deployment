# ---------------------------------------------------------------------------
# Pure-python tests for ctmdeploy.config against the REAL environment files
# (environments/<env>/config.yaml, v2 network schema: per-platform utility
# VNet + one VNet per Control-M/Server) plus synthetic-YAML validation tests.
# No Azure SDK required: pyyaml only.
# Runnable via `python3 -m pytest` or plain `python3 deploy/tests/test_config.py`.
# ---------------------------------------------------------------------------
"""Config parsing, v2 placement, derived naming and plan derivation tests."""

from __future__ import annotations

import copy
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any

import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from ctmdeploy import config as cfgmod  # noqa: E402
from ctmdeploy import replication  # noqa: E402  (pure helpers only; SDK is lazy)


def _skip(message: str) -> None:
    raise unittest.SkipTest(message)


def _config_path(env: str) -> Path:
    return REPO_ROOT / "environments" / env / "config.yaml"


def _load(env: str) -> cfgmod.EnvironmentConfig:
    path = _config_path(env)
    if not path.is_file():
        _skip(f"{path} does not exist yet (written by the bicep-v2 agent)")
    return cfgmod.load_config(env, REPO_ROOT, allow_placeholders=True)


def _subnet(cfg: cfgmod.EnvironmentConfig, rg: str, vnet: str, subnet: str) -> str:
    return (
        f"/subscriptions/{cfg.subscription_id}/resourceGroups/{rg}"
        f"/providers/Microsoft.Network/virtualNetworks/{vnet}/subnets/{subnet}"
    )


# ---------------------------------------------------------------------------
# Pure helpers (no config file needed)
# ---------------------------------------------------------------------------


def test_subnet_and_vnet_id_builders() -> None:
    sub = "00000000-0000-0000-0000-000000000000"
    assert cfgmod.vnet_id(sub, "rg-net", "vnet-x") == (
        f"/subscriptions/{sub}/resourceGroups/rg-net"
        "/providers/Microsoft.Network/virtualNetworks/vnet-x"
    )
    assert cfgmod.subnet_id(sub, "rg-net", "vnet-x", "snet-y") == (
        f"/subscriptions/{sub}/resourceGroups/rg-net"
        "/providers/Microsoft.Network/virtualNetworks/vnet-x/subnets/snet-y"
    )


def test_server_network_agent_defaults_and_overrides() -> None:
    net = cfgmod.ServerNetworkConfig(
        vnet_name="vnet-ctm-dev",
        vnet_resource_group_name="rg-network-nonprod",
        server_subnet_name="snet-ctm-server",
    )
    # Defaults: agents share the server's VNet and subnet.
    assert net.agents_subnet == "snet-ctm-server"
    assert net.agents_vnet == "vnet-ctm-dev"
    assert net.agents_vnet_resource_group == "rg-network-nonprod"
    assert net.agents_ref == net.ref

    relocated = cfgmod.ServerNetworkConfig(
        vnet_name="vnet-ctm-dev",
        vnet_resource_group_name="rg-network-nonprod",
        server_subnet_name="snet-ctm-server",
        agents_subnet_name="snet-ctm-agents",
        agents_vnet_name="vnet-agents-shared",
        agents_vnet_resource_group_name="rg-network-shared",
    )
    assert relocated.agents_subnet == "snet-ctm-agents"
    assert relocated.agents_vnet == "vnet-agents-shared"
    assert relocated.agents_vnet_resource_group == "rg-network-shared"


# ---------------------------------------------------------------------------
# Synthetic-YAML validation tests (v2 schema rules)
# ---------------------------------------------------------------------------


def _base_config(environment: str = "nonprod") -> dict[str, Any]:
    """A minimal valid v2 config dict for synthetic validation tests."""
    return {
        "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
        "environment": environment,
        "location": "eastus2",
        "unique_suffix": "n01" if environment == "nonprod" else "p01",
        "tags": {},
        "network": {
            "utility": {
                "vnet_name": "vnet-util",
                "vnet_resource_group_name": "rg-net",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
                "pe_subnet_name": "snet-pe",
            },
            "existing_private_dns_zone_ids": {},
        },
        "em": {"vm_size": "Standard_D4s_v5"},
        "servers": {
            "dev": {
                "vm_size": "Standard_D4s_v5",
                "agents": {
                    "vm_size": "Standard_D2s_v5",
                    "min": 1,
                    "max": 2,
                    "default": 1,
                },
                "network": {
                    "vnet_name": "vnet-dev",
                    "vnet_resource_group_name": "rg-net",
                    "server_subnet_name": "snet-server",
                },
            },
        },
        "database": {"sku_name": "GP_Standard_D2ds_v5"},
        "dr": {"enabled": False},
    }


def _load_dict(data: dict[str, Any]) -> cfgmod.EnvironmentConfig:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(data, handle, sort_keys=False)  # keep config order (server order matters)
        path = handle.name
    try:
        return cfgmod.load_config_file(path, allow_placeholders=True)
    finally:
        Path(path).unlink(missing_ok=True)


def _expect_config_error(data: dict[str, Any], *needles: str) -> None:
    try:
        _load_dict(data)
    except cfgmod.ConfigError as exc:
        message = str(exc)
        for needle in needles:
            assert needle in message, f"{needle!r} not in error: {message}"
    else:
        raise AssertionError(f"config was accepted; expected an error about {needles}")


def test_minimal_v2_config_loads() -> None:
    cfg = _load_dict(_base_config())
    assert cfg.network.utility.vnet_name == "vnet-util"
    dev = cfg.servers["dev"]
    assert dev.network.vnet_name == "vnet-dev"
    assert dev.network.agents_subnet == "snet-server"  # default = server subnet


def test_network_utility_is_required() -> None:
    data = _base_config()
    data["network"] = {  # old v1 shape must be rejected
        "vnet_name": "vnet-old",
        "vnet_resource_group_name": "rg-net",
        "app_subnet_name": "snet-app",
        "data_subnet_name": "snet-data",
    }
    _expect_config_error(data, "network.utility")


def test_server_network_is_required() -> None:
    data = _base_config()
    del data["servers"]["dev"]["network"]
    _expect_config_error(data, "servers.dev.network")


def test_agents_vnet_rg_requires_agents_vnet_name() -> None:
    data = _base_config()
    data["servers"]["dev"]["network"]["agents_vnet_resource_group_name"] = "rg-other"
    _expect_config_error(data, "agents_vnet_name")


def _dr_prod_config() -> dict[str, Any]:
    data = _base_config("prod")
    data["servers"] = {
        key: copy.deepcopy(data["servers"]["dev"]) for key in ("uat", "prod")
    }
    data["servers"]["uat"]["network"]["vnet_name"] = "vnet-uat"
    data["servers"]["prod"]["network"]["vnet_name"] = "vnet-prod"
    data["dr"] = {
        "enabled": True,
        "location": "centralus",
        "network": {
            "utility": {
                "vnet_name": "vnet-util-dr",
                "vnet_resource_group_name": "rg-net-dr",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
                "pe_subnet_name": "snet-pe",
            },
        },
        "server_networks": {
            "uat": {
                "vnet_name": "vnet-uat-dr",
                "vnet_resource_group_name": "rg-net-dr",
                "server_subnet_name": "snet-server",
            },
            "prod": {
                "vnet_name": "vnet-prod-dr",
                "vnet_resource_group_name": "rg-net-dr",
                "server_subnet_name": "snet-server",
            },
        },
        "warm_agents": 1,
        "replica_sku_name": "GP_Standard_D2ds_v5",
    }
    return data


def test_dr_server_networks_is_an_opt_in_subset() -> None:
    cfg = _load_dict(_dr_prod_config())  # complete mapping loads fine
    assert set(cfg.dr.server_networks) == {"uat", "prod"}
    assert cfg.dr_servers() == ["uat", "prod"]

    # OPT-IN: a strict subset is valid — servers absent from the set are
    # simply not DR-protected (no warm pool, no DR VNet, no ASR mapping).
    data = _dr_prod_config()
    del data["dr"]["server_networks"]["uat"]
    cfg = _load_dict(data)
    assert set(cfg.dr.server_networks) == {"prod"}
    assert cfg.dr_servers() == ["prod"]
    pools = cfgmod.agent_pools(cfg, "dr")
    assert [p.stage for p in pools] == ["prod"]
    assert cfg.all_vnets("dr") == [
        {"name": "vnet-util-dr", "resourceGroupName": "rg-net-dr"},
        {"name": "vnet-prod-dr", "resourceGroupName": "rg-net-dr"},
    ]
    assert {key for key, _, _ in replication.vnet_pairs(cfg)} == {"utility", "prod"}

    # But the set must be NON-EMPTY when dr.enabled is true.
    data = _dr_prod_config()
    del data["dr"]["server_networks"]
    _expect_config_error(data, "dr.server_networks", "at least one")


def test_dr_server_networks_rejects_unknown_keys() -> None:
    data = _dr_prod_config()
    data["dr"]["server_networks"]["qa"] = {
        "vnet_name": "vnet-qa-dr",
        "vnet_resource_group_name": "rg-net-dr",
        "server_subnet_name": "snet-server",
    }
    _expect_config_error(data, "unknown server keys", "qa")


def test_all_vnets_dedupes_and_orders() -> None:
    cfg = _load_dict(_dr_prod_config())
    assert cfg.all_vnets("primary") == [
        {"name": "vnet-util", "resourceGroupName": "rg-net"},
        {"name": "vnet-uat", "resourceGroupName": "rg-net"},
        {"name": "vnet-prod", "resourceGroupName": "rg-net"},
    ]
    assert cfg.all_vnets("dr") == [
        {"name": "vnet-util-dr", "resourceGroupName": "rg-net-dr"},
        {"name": "vnet-uat-dr", "resourceGroupName": "rg-net-dr"},
        {"name": "vnet-prod-dr", "resourceGroupName": "rg-net-dr"},
    ]
    links = cfg.dns_vnet_links("primary")
    assert [link["name"] for link in links] == ["vnet-util", "vnet-uat", "vnet-prod"]
    assert links[0]["vnetId"].endswith(
        "/resourceGroups/rg-net/providers/Microsoft.Network/virtualNetworks/vnet-util"
    )


def test_extra_dns_vnets_join_all_vnets() -> None:
    data = _base_config()
    data["network"]["extra_dns_vnets"] = [
        {"vnet_name": "vnet-hub", "vnet_resource_group_name": "rg-hub"},
        {"vnet_name": "vnet-dev", "vnet_resource_group_name": "rg-net"},  # duplicate
    ]
    cfg = _load_dict(data)
    assert cfg.all_vnets("primary") == [
        {"name": "vnet-util", "resourceGroupName": "rg-net"},
        {"name": "vnet-dev", "resourceGroupName": "rg-net"},
        {"name": "vnet-hub", "resourceGroupName": "rg-hub"},
    ]


def test_replication_vnet_pairs_and_targets_synthetic() -> None:
    cfg = _load_dict(_dr_prod_config())
    pairs = {key: (src, dst) for key, src, dst in replication.vnet_pairs(cfg)}
    assert set(pairs) == {"utility", "uat", "prod"}
    assert pairs["utility"][0].endswith("/virtualNetworks/vnet-util")
    assert pairs["utility"][1].endswith("/virtualNetworks/vnet-util-dr")
    assert pairs["prod"][0].endswith("/virtualNetworks/vnet-prod")
    assert pairs["prod"][1].endswith("/virtualNetworks/vnet-prod-dr")

    em_vnet, em_subnet = replication.recovery_target(cfg, {"name": "ctm-em-a", "component": "em"})
    assert em_vnet.endswith("/virtualNetworks/vnet-util-dr") and em_subnet == "snet-em"
    srv_vnet, srv_subnet = replication.recovery_target(
        cfg, {"name": "ctm-srv-uat-b", "component": "server"}
    )
    assert srv_vnet.endswith("/virtualNetworks/vnet-uat-dr") and srv_subnet == "snet-server"


# ---------------------------------------------------------------------------
# nonprod (values pinned to the v2 addendum PoC layout)
# ---------------------------------------------------------------------------


def test_nonprod_core_settings() -> None:
    cfg = _load("nonprod")
    assert cfg.environment == "nonprod"
    assert cfg.location == "eastus2"
    assert cfg.unique_suffix == "n01"
    assert cfg.resource_lock is False
    assert cfg.tags == {"costCenter": "scheduling", "owner": "platform-team"}
    assert cfg.deployer_ip_cidrs == ["203.0.113.10/32"]
    assert cfg.ctm_version == "9.0.21.300"
    assert cfg.dr.enabled is False


def test_nonprod_v2_network() -> None:
    cfg = _load("nonprod")
    utility = cfg.network.utility
    assert utility.vnet_name == "vnet-dev-utility"
    assert utility.vnet_resource_group_name == "rg-network-nonprod"
    assert utility.em_subnet_name == "snet-ctm-em"
    assert utility.data_subnet_name == "snet-ctm-postgres"
    assert utility.pe_subnet_name == "snet-ctm-pe"

    dev, qa = cfg.servers["dev"].network, cfg.servers["qa"].network
    assert dev.vnet_name == "vnet-ctm-dev" and qa.vnet_name == "vnet-ctm-qa"
    assert dev.vnet_resource_group_name == "rg-network-nonprod"
    assert dev.server_subnet_name == "snet-ctm-server"
    assert dev.agents_subnet == "snet-ctm-agents"
    # Agents stay in the server's own VNet (no relocation overrides in the PoC).
    assert dev.agents_vnet == "vnet-ctm-dev" and qa.agents_vnet == "vnet-ctm-qa"


def test_nonprod_em_and_servers() -> None:
    cfg = _load("nonprod")
    assert cfg.em.vm_size == "Standard_D4s_v5"
    assert cfg.em.data_disk_gb == 256
    assert cfg.em.enable_secondary is False
    assert cfg.em.disk_type == "Premium_LRS"  # TF default
    assert cfg.em.web_port == 8443  # TF default

    assert list(cfg.servers) == ["dev", "qa"]
    dev, qa = cfg.servers["dev"], cfg.servers["qa"]
    assert dev.name == "CTM_DEV" and qa.name == "CTM_QA"
    assert dev.vm_size == "Standard_D4s_v5" and dev.data_disk_gb == 128
    assert dev.enable_secondary is False and qa.enable_secondary is True
    assert qa.data_disk_gb == 256
    assert dev.agents.vm_size == "Standard_D2s_v5"
    assert (dev.agents.min, dev.agents.max, dev.agents.default) == (1, 2, 1)
    assert (qa.agents.min, qa.agents.max, qa.agents.default) == (1, 4, 2)
    assert dev.agents.use_spot is True and qa.agents.use_spot is True
    assert dev.agents.scale_out_cpu == 70 and dev.agents.scale_in_cpu == 25  # TF defaults
    assert dev.agents.agent_port == 7006 and dev.agents.provision_image == "Agent.Linux"


def test_nonprod_database_storage_backup_observability() -> None:
    cfg = _load("nonprod")
    db = cfg.database
    assert db.sku_name == "GP_Standard_D2ds_v5"
    assert db.storage_mb == 131072
    assert db.high_availability_mode == "ZoneRedundant"
    assert db.backup_retention_days == 7
    assert db.geo_redundant_backup is False
    assert db.postgres_version == "16"
    assert db.admin_user == "ctmadmin" and db.port == 5432

    assert cfg.storage.blob_replication_type == "ZRS"
    assert cfg.storage.files_replication_type == "ZRS"
    assert cfg.storage.shared_share_quota_gb == 256

    assert cfg.backup.enabled is True
    assert cfg.backup.storage_mode_type == "ZoneRedundant"
    assert cfg.backup.daily_retention_days == 14
    assert cfg.backup.include_files_share is True
    assert cfg.backup.cross_region_restore is False  # TF default

    assert cfg.observability.log_retention_days == 30
    assert cfg.observability.alert_emails == ["ctm-nonprod-alerts@example.com"]


def test_nonprod_node_plan_exact() -> None:
    cfg = _load("nonprod")
    names = [item.name for item in cfgmod.node_plan(cfg, "primary")]
    assert names == ["ctm-em-a", "ctm-srv-dev-a", "ctm-srv-qa-a", "ctm-srv-qa-b"], names


def test_nonprod_placement() -> None:
    """EM lands in the utility VNet; each server (and its agents) in its own."""
    cfg = _load("nonprod")
    by_name = {item.name: item for item in cfgmod.node_plan(cfg, "primary")}
    assert by_name["ctm-em-a"].subnet_id == _subnet(
        cfg, "rg-network-nonprod", "vnet-dev-utility", "snet-ctm-em"
    )
    assert by_name["ctm-srv-dev-a"].subnet_id == _subnet(
        cfg, "rg-network-nonprod", "vnet-ctm-dev", "snet-ctm-server"
    )
    assert by_name["ctm-srv-qa-a"].subnet_id == _subnet(
        cfg, "rg-network-nonprod", "vnet-ctm-qa", "snet-ctm-server"
    )
    assert by_name["ctm-srv-qa-b"].subnet_id == by_name["ctm-srv-qa-a"].subnet_id

    pools = {pool.stage: pool for pool in cfgmod.agent_pools(cfg, "primary")}
    assert pools["dev"].subnet_id == _subnet(
        cfg, "rg-network-nonprod", "vnet-ctm-dev", "snet-ctm-agents"
    )
    assert pools["qa"].subnet_id == _subnet(
        cfg, "rg-network-nonprod", "vnet-ctm-qa", "snet-ctm-agents"
    )


def test_nonprod_all_vnets() -> None:
    cfg = _load("nonprod")
    assert cfg.all_vnets("primary") == [
        {"name": "vnet-dev-utility", "resourceGroupName": "rg-network-nonprod"},
        {"name": "vnet-ctm-dev", "resourceGroupName": "rg-network-nonprod"},
        {"name": "vnet-ctm-qa", "resourceGroupName": "rg-network-nonprod"},
    ]


def test_nonprod_agent_pools() -> None:
    cfg = _load("nonprod")
    pools = cfgmod.agent_pools(cfg, "primary")
    assert [p.name for p in pools] == ["ctm-nonprod-agents-dev", "ctm-nonprod-agents-qa"]
    dev = pools[0]
    assert (dev.min, dev.max, dev.default) == (1, 2, 1)
    assert dev.use_spot is True
    assert dev.zones == ["1", "2", "3"]  # TF default


def test_nonprod_derived_names() -> None:
    cfg = _load("nonprod")
    assert cfg.name == "ctm-nonprod"
    assert cfg.compact == "ctmnonprod"
    assert cfg.resource_group_name == "ctm-nonprod-rg"
    assert cfg.key_vault_name == "ctm-nonprod-kv-n01"
    assert cfg.blob_account_name == "ctmnonprodblobn01"
    assert cfg.files_account_name == "ctmnonprodfilesn01"
    assert cfg.private_dns_zone_name == "nonprod.ctm.internal"
    assert cfg.db_alias == "ctm-db.nonprod.ctm.internal"
    assert cfg.web_alias == "ctm-web.nonprod.ctm.internal"
    dev = cfg.servers["dev"]
    assert dev.db_name == "ctm_dev" and dev.db_user == "ctm_dev"
    assert dev.secret_name == "ctm-db-password-dev"
    assert dev.hostgroup == "dev-agents"
    assert dev.primary_host == "ctm-srv-dev-a"
    assert dev.secondary_host == ""  # single node
    assert cfg.servers["qa"].secondary_host == "ctm-srv-qa-b"


# ---------------------------------------------------------------------------
# prod (values pinned to the v2 addendum PoC layout)
# ---------------------------------------------------------------------------


def test_prod_core_settings() -> None:
    cfg = _load("prod")
    assert cfg.environment == "prod"
    assert cfg.location == "eastus2"
    assert cfg.unique_suffix == "p01"
    assert cfg.resource_lock is True
    assert cfg.tags == {
        "costCenter": "scheduling",
        "owner": "platform-team",
        "criticality": "tier-1",
    }
    assert cfg.key_vault_public_access is True


def test_prod_v2_network() -> None:
    cfg = _load("prod")
    utility = cfg.network.utility
    assert utility.vnet_name == "vnet-prod-utility"
    assert utility.vnet_resource_group_name == "rg-network-prod"
    assert utility.em_subnet_name == "snet-ctm-em"
    assert utility.data_subnet_name == "snet-ctm-postgres"
    assert utility.pe_subnet_name == "snet-ctm-pe"

    uat, prod = cfg.servers["uat"].network, cfg.servers["prod"].network
    assert uat.vnet_name == "vnet-ctm-uat" and prod.vnet_name == "vnet-ctm-prod"
    assert uat.server_subnet_name == "snet-ctm-server"
    assert uat.agents_subnet == "snet-ctm-agents"
    assert uat.agents_vnet == "vnet-ctm-uat" and prod.agents_vnet == "vnet-ctm-prod"


def test_prod_em_servers_database() -> None:
    cfg = _load("prod")
    assert cfg.em.vm_size == "Standard_D16s_v5"
    assert cfg.em.data_disk_gb == 1024
    assert cfg.em.disk_type == "Premium_ZRS"
    assert cfg.em.enable_secondary is True

    assert set(cfg.servers) == {"uat", "prod"}
    uat, prod = cfg.servers["uat"], cfg.servers["prod"]
    assert uat.name == "CTM_UAT" and prod.name == "CTM_PROD"
    assert uat.vm_size == "Standard_D8s_v5" and prod.vm_size == "Standard_D16s_v5"
    assert uat.disk_type == "Premium_ZRS" and prod.disk_type == "Premium_ZRS"
    assert (uat.agents.min, uat.agents.max, uat.agents.default) == (2, 6, 2)
    assert (prod.agents.min, prod.agents.max, prod.agents.default) == (3, 20, 3)
    assert prod.agents.scale_out_cpu == 65 and prod.agents.scale_in_cpu == 20
    assert uat.agents.scale_out_cpu == 70 and uat.agents.scale_in_cpu == 25  # TF defaults
    assert uat.agents.use_spot is False  # TF default

    db = cfg.database
    assert db.sku_name == "GP_Standard_D8ds_v5"
    assert db.storage_mb == 524288
    assert db.backup_retention_days == 35
    assert db.geo_redundant_backup is True

    assert cfg.storage.blob_replication_type == "RAGZRS"
    assert cfg.backup.storage_mode_type == "GeoRedundant"
    assert cfg.backup.cross_region_restore is True
    assert cfg.backup.daily_retention_days == 35
    assert cfg.backup.weekly_retention_weeks == 52
    assert cfg.observability.log_retention_days == 90
    assert cfg.observability.alert_emails == [
        "ctm-prod-alerts@example.com",
        "noc@example.com",
    ]


def test_prod_node_plan_has_six_nodes() -> None:
    cfg = _load("prod")
    plan = cfgmod.node_plan(cfg, "primary")
    assert len(plan) == 6, [item.name for item in plan]
    assert {item.name for item in plan} == {
        "ctm-em-a",
        "ctm-em-b",
        "ctm-srv-uat-a",
        "ctm-srv-uat-b",
        "ctm-srv-prod-a",
        "ctm-srv-prod-b",
    }
    by_name = {item.name: item for item in plan}
    assert by_name["ctm-em-a"].role == "primary" and by_name["ctm-em-a"].zone == "1"
    assert by_name["ctm-em-b"].role == "secondary" and by_name["ctm-em-b"].zone == "2"
    assert by_name["ctm-em-a"].join_lb is True
    assert by_name["ctm-srv-prod-a"].join_lb is False
    assert by_name["ctm-srv-uat-a"].vm_size == "Standard_D8s_v5"
    assert by_name["ctm-srv-prod-b"].data_disk_gb == 1024
    assert by_name["ctm-srv-uat-b"].tags["ctm:server"] == "CTM_UAT"
    # DR region deploys NO nodes — VMs arrive via Site Recovery.
    assert cfgmod.node_plan(cfg, "dr") == []


def test_prod_placement() -> None:
    cfg = _load("prod")
    by_name = {item.name: item for item in cfgmod.node_plan(cfg, "primary")}
    em_subnet = cfg.em_subnet_id("primary")
    assert "vnet-prod-utility/subnets/snet-ctm-em" in em_subnet
    assert by_name["ctm-em-a"].subnet_id == em_subnet
    assert by_name["ctm-em-b"].subnet_id == em_subnet
    assert "vnet-ctm-uat/subnets/snet-ctm-server" in by_name["ctm-srv-uat-a"].subnet_id
    assert "vnet-ctm-prod/subnets/snet-ctm-server" in by_name["ctm-srv-prod-b"].subnet_id

    pools = {pool.stage: pool for pool in cfgmod.agent_pools(cfg, "primary")}
    assert "vnet-ctm-uat/subnets/snet-ctm-agents" in pools["uat"].subnet_id
    assert "vnet-ctm-prod/subnets/snet-ctm-agents" in pools["prod"].subnet_id


def test_prod_dr_settings_and_warm_pools() -> None:
    cfg = _load("prod")
    assert cfg.dr.enabled is True
    assert cfg.dr.location == "centralus"
    assert cfg.dr.network is not None
    assert cfg.dr.network.utility.vnet_name == "vnet-dr-utility"
    assert cfg.dr.network.utility.vnet_resource_group_name == "rg-network-prod-dr"
    assert cfg.dr.warm_agents == 1
    assert cfg.dr.enable_site_recovery is True
    assert cfg.dr.traffic_manager is False  # TF default
    assert cfg.replica_sku_name == "GP_Standard_D8ds_v5"

    # DR OPT-IN: only prod participates — exactly ONE warm pool, no uat pool.
    pools = cfgmod.agent_pools(cfg, "dr")
    assert [p.name for p in pools] == ["ctm-prod-dr-agents-prod"]
    warm = pools[0]
    assert warm.min == 0  # warm pool
    assert warm.default == 1  # dr.warm_agents
    assert warm.max == 20  # max unchanged
    # Warm agents land in the DR-protected server's DR VNet.
    assert "vnet-ctm-prod-dr/subnets/snet-ctm-agents" in warm.subnet_id

    primary_pools = cfgmod.agent_pools(cfg, "primary")
    assert [p.name for p in primary_pools] == ["ctm-prod-agents-uat", "ctm-prod-agents-prod"]
    assert primary_pools[0].min == 2 and primary_pools[1].min == 3


def test_prod_access_appgw_dr_instance_ships_enabled() -> None:
    """The shipped prod config enables the DR gateway twin and resolves its
    placement (the DR utility VNet's appgw-role subnet) at load time."""
    cfg = _load("prod")
    assert cfg.access.appgw_enabled is True
    assert cfg.access.appgw_dr_instance is True
    assert cfg.appgw_subnet_id("dr") == _subnet(
        cfg, "rg-network-prod-dr", "vnet-dr-utility", "snet-ctm-appgw"
    )
    # Deterministic gateway names (failover reads the twin live by this name).
    assert cfg.appgw_name == "ctm-prod-appgw"
    assert cfg.dr_appgw_name == "ctm-prod-dr-appgw"


def test_prod_dr_server_networks() -> None:
    """DR opt-in set: ONLY prod participates (uat is not DR-protected)."""
    cfg = _load("prod")
    assert set(cfg.dr.server_networks) == {"prod"}
    assert cfg.dr_servers() == ["prod"]
    prod = cfg.dr.server_networks["prod"]
    assert prod.vnet_name == "vnet-ctm-prod-dr"
    assert prod.vnet_resource_group_name == "rg-network-prod-dr"
    assert prod.server_subnet_name == "snet-ctm-server"
    assert prod.agents_subnet == "snet-ctm-agents"

    assert cfg.all_vnets("dr") == [
        {"name": "vnet-dr-utility", "resourceGroupName": "rg-network-prod-dr"},
        {"name": "vnet-ctm-prod-dr", "resourceGroupName": "rg-network-prod-dr"},
    ]


def test_prod_dr_replication_mappings() -> None:
    """ASR mappings: utility->dr-utility + each PARTICIPATING server VNet->
    its DR VNet (uat: none — it is not in the dr.server_networks opt-in set)."""
    cfg = _load("prod")
    pairs = {key: (src, dst) for key, src, dst in replication.vnet_pairs(cfg)}
    assert set(pairs) == {"utility", "prod"}
    assert pairs["utility"][0].endswith("/virtualNetworks/vnet-prod-utility")
    assert pairs["utility"][1].endswith("/virtualNetworks/vnet-dr-utility")
    assert pairs["prod"][0].endswith("/virtualNetworks/vnet-ctm-prod")
    assert pairs["prod"][1].endswith("/virtualNetworks/vnet-ctm-prod-dr")

    # Replicated-VM targets: EM -> DR utility em subnet, server -> its DR
    # server subnet.
    em_vnet, em_subnet = replication.recovery_target(cfg, {"name": "ctm-em-b", "component": "em"})
    assert em_vnet.endswith("/virtualNetworks/vnet-dr-utility")
    assert em_subnet == "snet-ctm-em"
    srv_vnet, srv_subnet = replication.recovery_target(
        cfg, {"name": "ctm-srv-prod-a", "component": "server"}
    )
    assert srv_vnet.endswith("/virtualNetworks/vnet-ctm-prod-dr")
    assert srv_subnet == "snet-ctm-server"


def test_prod_dr_plan_is_exactly_four_protected_vms_one_warm_pool() -> None:
    """Addendum section 4: the prod DR plan protects exactly 4 VMs
    (ctm-em-a/b + ctm-srv-prod-a/b), deploys ONE warm pool (dr-agents-prod)
    and references no uat DR network anywhere."""
    cfg = _load("prod")
    nodes = [
        {"name": item.name, "component": item.component}
        for item in cfgmod.node_plan(cfg, "primary")
    ]
    protected = replication.dr_protected_nodes(cfg, nodes)
    assert {n["name"] for n in protected} == {
        "ctm-em-a",
        "ctm-em-b",
        "ctm-srv-prod-a",
        "ctm-srv-prod-b",
    }

    pools = cfgmod.agent_pools(cfg, "dr")
    assert [p.name for p in pools] == ["ctm-prod-dr-agents-prod"]

    # No uat DR network anywhere: opt-in set, DR VNet list, ASR pairs.
    assert "uat" not in cfg.dr.server_networks
    assert all("uat" not in vnet["name"] for vnet in cfg.all_vnets("dr"))
    for key, src, dst in replication.vnet_pairs(cfg):
        assert key != "uat"
        assert "uat" not in dst
    # Asking for uat's DR network is an explicit error, not a silent default.
    try:
        cfg.server_network("uat", "dr")
    except cfgmod.ConfigError as exc:
        assert "not DR-protected" in str(exc)
    else:
        raise AssertionError("uat has no DR network; server_network should raise")


def test_prod_derived_names() -> None:
    cfg = _load("prod")
    assert cfg.name == "ctm-prod" and cfg.dr_name == "ctm-prod-dr"
    assert cfg.dr_resource_group_name == "ctm-prod-dr-rg"
    assert cfg.key_vault_name == "ctm-prod-kv-p01"
    assert cfg.dr_key_vault_name == "ctm-prod-dr-kv-p01"
    assert cfg.blob_account_name == "ctmprodblobp01"
    assert cfg.dr_blob_account_name == "ctmproddrblobp01"
    assert cfg.postgres_server_name == "ctm-prod-pg-p01"
    assert cfg.replica_name == "ctm-prod-dr-pg-p01"
    assert cfg.replica_fqdn == "ctm-prod-dr-pg-p01.postgres.database.azure.com"
    assert cfg.asr_vault_name == "ctm-prod-asr-rsv"
    assert cfg.asr_fabric_primary == "ctm-prod-fabric-primary"
    assert cfg.asr_container_primary == "ctm-prod-container-primary"
    assert cfg.dr_agent_pool_name("prod") == "ctm-prod-dr-agents-prod"


def test_prod_image_reference() -> None:
    """v4.1 two-image model: base (ctm-rhel9) + agent (ctm-agent)."""
    cfg = _load("prod")
    assert cfg.image.gallery_resource_group == "ctm-images-rg"
    assert cfg.image.gallery_name == "ctmgallery"
    assert cfg.image.base.definition == "ctm-rhel9"
    assert cfg.image.agent.definition == "ctm-agent"
    base_id = cfg.image_id()  # default kind = base (EM/Server VMs)
    assert base_id == cfg.image_id("base")
    assert base_id.startswith(f"/subscriptions/{cfg.subscription_id}/resourceGroups/")
    assert base_id.endswith(
        "/providers/Microsoft.Compute/galleries/ctmgallery/images/ctm-rhel9/versions/"
        + cfg.image.base.version
    )
    agent_id = cfg.image_id("agent")
    assert agent_id.endswith(
        "/providers/Microsoft.Compute/galleries/ctmgallery/images/ctm-agent/versions/"
        + cfg.image.agent.version
    )
    try:
        cfg.image_id("bogus")
    except cfgmod.ConfigError as exc:
        assert "base or agent" in str(exc)
    else:
        raise AssertionError("an unknown image kind was accepted")


def test_image_block_defaults_and_rejects_old_flat_keys() -> None:
    # Absent block -> both defaults.
    cfg = _load_dict(_base_config())
    assert cfg.image.base == cfgmod.ImageRef("ctm-rhel9", "latest")
    assert cfg.image.agent == cfgmod.ImageRef("ctm-agent", "latest")

    # Nested shape parses, versions pinned independently.
    data = _base_config()
    data["image"] = {
        "gallery_resource_group": "ctm-images-rg",
        "gallery_name": "ctmgallery",
        "base": {"definition": "ctm-rhel9", "version": "1.2.0"},
        "agent": {"definition": "ctm-agent", "version": "1.0.3"},
    }
    cfg = _load_dict(data)
    assert cfg.image.base.version == "1.2.0"
    assert cfg.image.agent.version == "1.0.3"
    assert cfg.image_id("agent").endswith("/images/ctm-agent/versions/1.0.3")

    # The pre-v4.1 FLAT keys are rejected with a pointer to the new shape.
    data = _base_config()
    data["image"] = {"image_definition": "ctm-rhel9", "image_version": "latest"}
    _expect_config_error(data, "config.image", "image_definition", "base", "agent")

    # Unknown keys are rejected too.
    data = _base_config()
    data["image"] = {"gallery": "nope"}
    _expect_config_error(data, "config.image", "unknown keys")


def test_bicep_item_shapes() -> None:
    cfg = _load("prod")
    node = cfgmod.node_plan(cfg, "primary")[0].to_bicep("#!/bin/bash", is_new=False)
    assert set(node) == {
        "name",
        "component",
        "role",
        "zone",
        "vmSize",
        "dataDiskGb",
        "diskType",
        "customData",
        "joinLb",
        "tags",
        "subnetId",
        "isNew",
    }
    assert node["isNew"] is False
    assert node["subnetId"].endswith("/subnets/snet-ctm-em")
    pool = cfgmod.agent_pools(cfg, "primary")[0].to_bicep("#!/bin/bash")
    assert set(pool) == {
        "stage",
        "name",
        "vmSize",
        "zones",
        "min",
        "max",
        "default",
        "scaleOutCpu",
        "scaleInCpu",
        "useSpot",
        "customData",
        "subnetId",
        "isNew",
    }
    assert pool["isNew"] is True  # default: offline rendering treats items as new
    assert pool["subnetId"].endswith("/subnets/snet-ctm-agents")


def test_foundation_network_param_shape() -> None:
    """Exact config-shaped `network` object handed to bicep/foundation.bicep."""
    cfg = _load("prod")
    param = cfg.network.as_param()
    assert set(param) == {"utility", "existing_private_dns_zone_ids", "extra_dns_vnets"}
    assert param["utility"] == {
        "vnet_name": "vnet-prod-utility",
        "vnet_resource_group_name": "rg-network-prod",
        "em_subnet_name": "snet-ctm-em",
        "data_subnet_name": "snet-ctm-postgres",
        "pe_subnet_name": "snet-ctm-pe",
    }


def test_placeholder_subscription_rejected_without_flag() -> None:
    path = _config_path("nonprod")
    if not path.is_file():
        _skip(f"{path} does not exist yet (written by the bicep-v2 agent)")
    cfg = cfgmod.load_config("nonprod", REPO_ROOT, allow_placeholders=True)
    if cfg.subscription_id != cfgmod.PLACEHOLDER_SUBSCRIPTION_ID:
        _skip("config.yaml carries a real subscription id; placeholder check not applicable")
    try:
        cfgmod.load_config("nonprod", REPO_ROOT, allow_placeholders=False)
    except cfgmod.ConfigError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("placeholder subscription id was accepted without the flag")


def test_platform_tags() -> None:
    cfg = _load("prod")
    tags = cfg.platform_tags("primary")
    assert tags["environment"] == "prod"
    assert tags["platform"] == "control-m"
    assert tags["role"] == "primary"
    assert tags["costCenter"] == "scheduling"


# ---------------------------------------------------------------------------
# Plain-python runner (pytest also discovers the test_* functions above)
# ---------------------------------------------------------------------------


def _run_all() -> int:
    passed = failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
        except unittest.SkipTest as skip:
            skipped += 1
            print(f"SKIP {name}: {skip}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\n{Path(__file__).name}: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
