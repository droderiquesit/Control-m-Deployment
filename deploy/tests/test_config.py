# ---------------------------------------------------------------------------
# Pure-python tests for ctmdeploy.config against the REAL environment files
# (environments/<env>/config.yaml). No Azure SDK required: pyyaml only.
# Runnable via `python3 -m pytest` or plain `python3 deploy/tests/test_config.py`.
# ---------------------------------------------------------------------------
"""Config parsing, derived naming and node/agent plan derivation tests."""

from __future__ import annotations

import sys
import traceback
import unittest
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from ctmdeploy import config as cfgmod  # noqa: E402


def _skip(message: str) -> None:
    raise unittest.SkipTest(message)


def _config_path(env: str) -> Path:
    return REPO_ROOT / "environments" / env / "config.yaml"


def _load(env: str) -> cfgmod.EnvironmentConfig:
    path = _config_path(env)
    if not path.is_file():
        _skip(f"{path} does not exist yet (written by the bicep-entry agent)")
    return cfgmod.load_config(env, REPO_ROOT, allow_placeholders=True)


# ---------------------------------------------------------------------------
# nonprod (values pinned to the old environments/nonprod tfvars file)
# ---------------------------------------------------------------------------


def test_nonprod_core_settings() -> None:
    cfg = _load("nonprod")
    assert cfg.environment == "nonprod"
    assert cfg.location == "eastus2"
    assert cfg.unique_suffix == "n01"
    assert cfg.resource_lock is False
    assert cfg.tags == {"costCenter": "scheduling", "owner": "platform-team"}
    assert cfg.network.vnet_name == "vnet-nonprod-eastus2"
    assert cfg.network.vnet_resource_group_name == "rg-network-nonprod"
    assert cfg.network.app_subnet_name == "snet-ctm-app"
    assert cfg.network.data_subnet_name == "snet-ctm-postgres"
    assert cfg.deployer_ip_cidrs == ["203.0.113.10/32"]
    assert cfg.ctm_version == "9.0.21.300"
    assert cfg.dr.enabled is False


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
# prod (values pinned to the old environments/prod tfvars file)
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
    assert cfg.network.vnet_name == "vnet-prod-eastus2"
    assert cfg.key_vault_public_access is True


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


def test_prod_dr_settings_and_warm_pools() -> None:
    cfg = _load("prod")
    assert cfg.dr.enabled is True
    assert cfg.dr.location == "centralus"
    assert cfg.dr.network is not None
    assert cfg.dr.network.vnet_name == "vnet-prod-centralus"
    assert cfg.dr.warm_agents == 1
    assert cfg.dr.enable_site_recovery is True
    assert cfg.dr.traffic_manager is False  # TF default
    assert cfg.replica_sku_name == "GP_Standard_D8ds_v5"

    pools = cfgmod.agent_pools(cfg, "dr")
    assert [p.name for p in pools] == ["ctm-prod-dr-agents-uat", "ctm-prod-dr-agents-prod"]
    for pool in pools:
        assert pool.min == 0  # warm pools
        assert pool.default == 1  # dr.warm_agents
    assert pools[0].max == 6 and pools[1].max == 20  # max unchanged

    primary_pools = cfgmod.agent_pools(cfg, "primary")
    assert [p.name for p in primary_pools] == ["ctm-prod-agents-uat", "ctm-prod-agents-prod"]
    assert primary_pools[0].min == 2 and primary_pools[1].min == 3


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
    assert cfg.dr_agent_pool_name("uat") == "ctm-prod-dr-agents-uat"


def test_prod_image_reference() -> None:
    cfg = _load("prod")
    assert cfg.image.gallery_resource_group == "ctm-images-rg"
    assert cfg.image.gallery_name == "ctmgallery"
    assert cfg.image.image_definition == "ctm-rhel9"
    image_id = cfg.image_id()
    assert image_id.startswith(f"/subscriptions/{cfg.subscription_id}/resourceGroups/")
    assert image_id.endswith(
        "/providers/Microsoft.Compute/galleries/ctmgallery/images/ctm-rhel9/versions/"
        + cfg.image.image_version
    )


def test_bicep_item_shapes() -> None:
    cfg = _load("prod")
    node = cfgmod.node_plan(cfg, "primary")[0].to_bicep("#!/bin/bash")
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
    }
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
    }


def test_placeholder_subscription_rejected_without_flag() -> None:
    path = _config_path("nonprod")
    if not path.is_file():
        _skip(f"{path} does not exist yet (written by the bicep-entry agent)")
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
