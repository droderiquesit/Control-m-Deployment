# ---------------------------------------------------------------------------
# Typed configuration for the Control-M platform: loads environments/<env>/
# config.yaml into dataclasses, applies the defaults the old platform modules
# used, and derives naming, node plans and agent-pool plans. Pure python:
# this module MUST stay importable without any Azure SDK installed.
# ---------------------------------------------------------------------------
"""Configuration loading, validation and derived values (names, node plans)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

PLACEHOLDER_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ENVIRONMENTS = ("nonprod", "prod")

# Fixed platform constants (identical to the old platform module).
NAME_PREFIX = "ctm"
EM_PRIMARY_HOST = "ctm-em-a"
EM_SECONDARY_HOST = "ctm-em-b"
DATA_MOUNT = "/ctm"
EM_API_USER = "emuser"
INSTALLERS_CONTAINER = "installers"
BACKUPS_CONTAINER = "backups"
SHARE_NAME = "ctm-shared"

_SUFFIX_RE = re.compile(r"^[a-z0-9]{3,6}$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Default blob names for BMC media, byte-for-byte the old platform-module
# installer_blobs defaults. They intentionally do NOT
# track ctm_version — override installer_blobs in config.yaml for other media.
DEFAULT_INSTALLER_BLOBS: dict[str, str] = {
    "em_installer": "em/DREM.9.0.21.300_Linux-x86_64.tar.Z",
    "server_installer": "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z",
    "em_silent_primary": "em/silent-em-primary.xml",
    "em_silent_secondary": "em/silent-em-secondary.xml",
    "server_silent_primary": "server/silent-server-primary.xml",
    "server_silent_secondary": "server/silent-server-secondary.xml",
}


class ConfigError(ValueError):
    """A configuration file is missing, malformed or fails validation."""


# ---------------------------------------------------------------------------
# Low-level extraction helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
    return dict(value)


def _str(data: Mapping[str, Any], key: str, path: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if value is None:
        raise ConfigError(f"{path}.{key}: required string is missing")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{path}.{key}: expected a string, got {type(value).__name__}")
    return str(value)


def _int(data: Mapping[str, Any], key: str, path: str, default: int | None = None) -> int:
    value = data.get(key, default)
    if value is None:
        raise ConfigError(f"{path}.{key}: required integer is missing")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key}: expected an integer, got {type(value).__name__}")
    return value


def _bool(data: Mapping[str, Any], key: str, path: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key}: expected a boolean, got {type(value).__name__}")
    return value


def _str_list(data: Mapping[str, Any], key: str, path: str, default: list[str]) -> list[str]:
    value = data.get(key)
    if value is None:
        return list(default)
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{path}.{key}: expected a list, got {type(value).__name__}")
    return [str(item) for item in value]


def _str_map(data: Mapping[str, Any], key: str, path: str) -> dict[str, str]:
    value = _mapping(data.get(key), f"{path}.{key}")
    return {str(k): str(v) for k, v in value.items()}


# ---------------------------------------------------------------------------
# Dataclasses (one per config.yaml section)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkConfig:
    """Existing VNet/subnets to deploy into (selected, never created)."""

    vnet_name: str
    vnet_resource_group_name: str
    app_subnet_name: str
    data_subnet_name: str
    pe_subnet_name: str = ""
    existing_private_dns_zone_ids: dict[str, str] = field(default_factory=dict)

    def as_param(self) -> dict[str, Any]:
        """Config-shaped object passed to bicep/foundation.bicep `network`."""
        return {
            "vnet_name": self.vnet_name,
            "vnet_resource_group_name": self.vnet_resource_group_name,
            "app_subnet_name": self.app_subnet_name,
            "data_subnet_name": self.data_subnet_name,
            "pe_subnet_name": self.pe_subnet_name,
            "existing_private_dns_zone_ids": dict(self.existing_private_dns_zone_ids),
        }


@dataclass(frozen=True)
class ImageConfig:
    """Azure Compute Gallery image every VM/VMSS boots from (built by Packer)."""

    gallery_resource_group: str = "ctm-images-rg"
    gallery_name: str = "ctmgallery"
    image_definition: str = "ctm-rhel9"
    image_version: str = "latest"

    def image_id(self, subscription_id: str) -> str:
        """Full ARM id of the gallery image version ('latest' is ARM-valid)."""
        return (
            f"/subscriptions/{subscription_id}"
            f"/resourceGroups/{self.gallery_resource_group}"
            f"/providers/Microsoft.Compute/galleries/{self.gallery_name}"
            f"/images/{self.image_definition}/versions/{self.image_version}"
        )


@dataclass(frozen=True)
class AgentsConfig:
    """Agent VMSS sizing for one Control-M/Server (TF agents object)."""

    vm_size: str
    min: int
    max: int
    default: int
    zones: list[str] = field(default_factory=lambda: ["1", "2", "3"])
    use_spot: bool = False
    hostgroup: str = ""
    provision_image: str = "Agent.Linux"
    agent_port: int = 7006
    os_user: str = "ctmagent"
    scale_out_cpu: int = 70
    scale_in_cpu: int = 25


@dataclass(frozen=True)
class ServerConfig:
    """One Control-M/Server (stage) with its derived names, exactly as the
    old platform locals computed them."""

    key: str
    name: str
    vm_size: str
    agents: AgentsConfig
    data_disk_gb: int = 256
    disk_type: str = "Premium_LRS"
    enable_secondary: bool = True
    zones: list[str] = field(default_factory=lambda: ["1", "2"])
    os_user: str = "controlm"

    @property
    def db_name(self) -> str:
        return f"ctm_{self.key}"

    @property
    def db_user(self) -> str:
        return f"ctm_{self.key}"

    @property
    def secret_name(self) -> str:
        return f"ctm-db-password-{self.key}"

    @property
    def hostgroup(self) -> str:
        return self.agents.hostgroup or f"{self.key}-agents"

    @property
    def primary_host(self) -> str:
        return f"ctm-srv-{self.key}-a"

    @property
    def secondary_host(self) -> str:
        return f"ctm-srv-{self.key}-b" if self.enable_secondary else ""

    def as_param(self) -> dict[str, Any]:
        """Config-shaped object (defaults applied) for the foundation deployment."""
        return {
            "name": self.name,
            "vm_size": self.vm_size,
            "data_disk_gb": self.data_disk_gb,
            "disk_type": self.disk_type,
            "enable_secondary": self.enable_secondary,
            "zones": list(self.zones),
            "os_user": self.os_user,
            "agents": {
                "vm_size": self.agents.vm_size,
                "min": self.agents.min,
                "max": self.agents.max,
                "default": self.agents.default,
                "zones": list(self.agents.zones),
                "use_spot": self.agents.use_spot,
                "hostgroup": self.hostgroup,
                "provision_image": self.agents.provision_image,
                "agent_port": self.agents.agent_port,
                "scale_out_cpu": self.agents.scale_out_cpu,
                "scale_in_cpu": self.agents.scale_in_cpu,
            },
        }


@dataclass(frozen=True)
class EmConfig:
    """Enterprise Manager tier (TF em object defaults)."""

    vm_size: str
    data_disk_gb: int = 256
    disk_type: str = "Premium_LRS"
    enable_secondary: bool = True
    zones: list[str] = field(default_factory=lambda: ["1", "2"])
    web_port: int = 8443
    os_user: str = "emuser"
    db_name: str = "emdb"
    db_user: str = "emuser"


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL Flexible Server settings (TF database object defaults)."""

    sku_name: str
    storage_mb: int = 131072
    postgres_version: str = "16"
    high_availability_mode: str = "ZoneRedundant"
    backup_retention_days: int = 14
    geo_redundant_backup: bool = False
    admin_user: str = "ctmadmin"
    port: int = 5432

    def as_param(self) -> dict[str, Any]:
        return {
            "sku_name": self.sku_name,
            "storage_mb": self.storage_mb,
            "postgres_version": self.postgres_version,
            "high_availability_mode": self.high_availability_mode,
            "backup_retention_days": self.backup_retention_days,
            "geo_redundant_backup": self.geo_redundant_backup,
            "admin_user": self.admin_user,
            "port": self.port,
        }


@dataclass(frozen=True)
class StorageConfig:
    blob_replication_type: str = "RAGZRS"
    files_replication_type: str = "ZRS"
    shared_share_quota_gb: int = 256

    def as_param(self) -> dict[str, Any]:
        return {
            "blob_replication_type": self.blob_replication_type,
            "files_replication_type": self.files_replication_type,
            "shared_share_quota_gb": self.shared_share_quota_gb,
        }


@dataclass(frozen=True)
class LoadBalancerConfig:
    internal: bool = True
    public_dns_label: str = ""

    def as_param(self, web_port: int) -> dict[str, Any]:
        return {
            "internal": self.internal,
            "public_dns_label": self.public_dns_label,
            "web_port": web_port,
        }


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = True
    storage_mode_type: str = "GeoRedundant"
    cross_region_restore: bool = False
    daily_retention_days: int = 30
    weekly_retention_weeks: int = 12
    include_files_share: bool = True

    def as_param(self, *, enabled: bool | None = None) -> dict[str, Any]:
        return {
            "enabled": self.enabled if enabled is None else enabled,
            "storage_mode_type": self.storage_mode_type,
            "cross_region_restore": self.cross_region_restore,
            "daily_retention_days": self.daily_retention_days,
            "weekly_retention_weeks": self.weekly_retention_weeks,
            "include_files_share": self.include_files_share,
        }


@dataclass(frozen=True)
class ObservabilityConfig:
    log_retention_days: int = 30
    alert_emails: list[str] = field(default_factory=list)

    def as_param(self) -> dict[str, Any]:
        return {
            "log_retention_days": self.log_retention_days,
            "alert_emails": list(self.alert_emails),
        }


@dataclass(frozen=True)
class DrConfig:
    """Disaster-recovery region settings; enabled=false skips DR entirely."""

    enabled: bool = False
    location: str = ""
    network: NetworkConfig | None = None
    warm_agents: int = 0
    replica_sku_name: str = ""
    enable_site_recovery: bool = True
    traffic_manager: bool = False


@dataclass(frozen=True)
class InstallerBlobs:
    """Blob names (inside the installers container) uploaded from BMC EPD."""

    em_installer: str
    server_installer: str
    em_silent_primary: str
    em_silent_secondary: str
    server_silent_primary: str
    server_silent_secondary: str


# ---------------------------------------------------------------------------
# Plans handed to bicep/compute.bicep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodePlanItem:
    """One EM or Control-M/Server VM in the compute deployment's nodePlan."""

    name: str
    component: str  # 'em' | 'server'
    role: str  # 'primary' | 'secondary'
    zone: str
    vm_size: str
    data_disk_gb: int
    disk_type: str
    join_lb: bool
    tags: dict[str, str]
    server_key: str = ""  # '' for EM nodes

    def to_bicep(self, custom_data: str) -> dict[str, Any]:
        """Exact item shape of bicep/compute.bicep param nodePlan."""
        return {
            "name": self.name,
            "component": self.component,
            "role": self.role,
            "zone": self.zone,
            "vmSize": self.vm_size,
            "dataDiskGb": self.data_disk_gb,
            "diskType": self.disk_type,
            "customData": custom_data,
            "joinLb": self.join_lb,
            "tags": dict(self.tags),
        }


@dataclass(frozen=True)
class AgentPoolPlanItem:
    """One agent VMSS in the compute deployment's agentPools."""

    stage: str
    name: str
    vm_size: str
    zones: list[str]
    min: int
    max: int
    default: int
    scale_out_cpu: int
    scale_in_cpu: int
    use_spot: bool

    def to_bicep(self, custom_data: str) -> dict[str, Any]:
        """Exact item shape of bicep/compute.bicep param agentPools."""
        return {
            "stage": self.stage,
            "name": self.name,
            "vmSize": self.vm_size,
            "zones": list(self.zones),
            "min": self.min,
            "max": self.max,
            "default": self.default,
            "scaleOutCpu": self.scale_out_cpu,
            "scaleInCpu": self.scale_in_cpu,
            "useSpot": self.use_spot,
            "customData": custom_data,
        }


# ---------------------------------------------------------------------------
# Environment config + derived naming
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentConfig:
    """Everything environments/<env>/config.yaml declares, plus derived names.

    Naming is identical to the old platform locals: name = ctm-<env>[-dr],
    compact = name without dashes.
    """

    subscription_id: str
    environment: str
    location: str
    unique_suffix: str
    resource_lock: bool
    tags: dict[str, str]
    network: NetworkConfig
    deployer_ip_cidrs: list[str]
    key_vault_public_access: bool
    ctm_version: str
    installer_blobs: InstallerBlobs
    image: ImageConfig
    em: EmConfig
    servers: dict[str, ServerConfig]
    database: DatabaseConfig
    storage: StorageConfig
    load_balancer: LoadBalancerConfig
    backup: BackupConfig
    observability: ObservabilityConfig
    dr: DrConfig

    # ---- names -----------------------------------------------------------
    @property
    def name(self) -> str:
        return f"{NAME_PREFIX}-{self.environment}"

    @property
    def compact(self) -> str:
        return self.name.replace("-", "")

    @property
    def dr_name(self) -> str:
        return f"{self.name}-dr"

    @property
    def dr_compact(self) -> str:
        return self.dr_name.replace("-", "")

    @property
    def private_dns_zone_name(self) -> str:
        return f"{self.environment}.ctm.internal"

    @property
    def db_alias(self) -> str:
        return f"ctm-db.{self.private_dns_zone_name}"

    @property
    def web_alias(self) -> str:
        return f"ctm-web.{self.private_dns_zone_name}"

    @property
    def resource_group_name(self) -> str:
        return f"{self.name}-rg"

    @property
    def dr_resource_group_name(self) -> str:
        return f"{self.dr_name}-rg"

    @property
    def key_vault_name(self) -> str:
        return f"{self.name}-kv-{self.unique_suffix}"

    @property
    def dr_key_vault_name(self) -> str:
        return f"{self.dr_name}-kv-{self.unique_suffix}"

    @property
    def key_vault_uri(self) -> str:
        return f"https://{self.key_vault_name}.vault.azure.net/"

    @property
    def dr_key_vault_uri(self) -> str:
        return f"https://{self.dr_key_vault_name}.vault.azure.net/"

    @property
    def blob_account_name(self) -> str:
        return f"{self.compact}blob{self.unique_suffix}"[:24]

    @property
    def files_account_name(self) -> str:
        return f"{self.compact}files{self.unique_suffix}"[:24]

    @property
    def dr_blob_account_name(self) -> str:
        return f"{self.dr_compact}blob{self.unique_suffix}"[:24]

    @property
    def dr_files_account_name(self) -> str:
        return f"{self.dr_compact}files{self.unique_suffix}"[:24]

    @property
    def postgres_server_name(self) -> str:
        return f"{self.name}-pg-{self.unique_suffix}"

    @property
    def replica_name(self) -> str:
        return f"{self.name}-dr-pg-{self.unique_suffix}"

    @property
    def replica_fqdn(self) -> str:
        return f"{self.replica_name}.postgres.database.azure.com"

    @property
    def asr_vault_name(self) -> str:
        return f"{self.name}-asr-rsv"

    @property
    def asr_fabric_primary(self) -> str:
        return f"{self.name}-fabric-primary"

    @property
    def asr_fabric_dr(self) -> str:
        return f"{self.name}-fabric-dr"

    @property
    def asr_container_primary(self) -> str:
        return f"{self.name}-container-primary"

    @property
    def asr_container_dr(self) -> str:
        return f"{self.name}-container-dr"

    @property
    def asr_policy_name(self) -> str:
        return f"{self.name}-asr-policy"

    @property
    def replica_sku_name(self) -> str:
        """DR replica SKU: dr.replica_sku_name, falling back to the primary SKU."""
        return self.dr.replica_sku_name or self.database.sku_name

    def image_id(self) -> str:
        """ARM id of the gallery image version all nodes boot from."""
        return self.image.image_id(self.subscription_id)

    def dr_agent_pool_name(self, stage: str) -> str:
        return f"{self.dr_name}-agents-{stage}"

    def platform_tags(self, role: str) -> dict[str, str]:
        """Common platform tags; managedBy now reads 'bicep' (the one tag value
        that changed with the toolchain migration)."""
        return {
            **self.tags,
            "environment": self.environment,
            "platform": "control-m",
            "role": role,
            "managedBy": "bicep",
        }


# ---------------------------------------------------------------------------
# Plan derivation (ported from modules/platform/main.tf locals and the
# environments/<env>/main.tf DR overrides)
# ---------------------------------------------------------------------------


def node_plan(cfg: EnvironmentConfig, role: str = "primary") -> list[NodePlanItem]:
    """EM + Control-M/Server VMs for one region.

    role='primary': EM pair (single node when em.enable_secondary is false,
    as in nonprod) followed by each Server's a/b pair in config order.
    role='dr': empty — DR VMs arrive through Site Recovery, never deployed.
    """
    if role not in ("primary", "dr"):
        raise ConfigError(f"node_plan role must be primary or dr, got {role!r}")
    if role == "dr":
        return []

    base_tags = cfg.platform_tags(role)
    plan: list[NodePlanItem] = []

    em_nodes = [(EM_PRIMARY_HOST, "primary", cfg.em.zones[0])]
    if cfg.em.enable_secondary:
        em_nodes.append((EM_SECONDARY_HOST, "secondary", cfg.em.zones[1]))
    for name, node_role, zone in em_nodes:
        plan.append(
            NodePlanItem(
                name=name,
                component="em",
                role=node_role,
                zone=zone,
                vm_size=cfg.em.vm_size,
                data_disk_gb=cfg.em.data_disk_gb,
                disk_type=cfg.em.disk_type,
                join_lb=True,
                tags={**base_tags, "ctm:component": "em", "ctm:role": node_role},
            )
        )

    for key, server in cfg.servers.items():
        server_nodes = [(server.primary_host, "primary", server.zones[0])]
        if server.enable_secondary:
            server_nodes.append((server.secondary_host, "secondary", server.zones[1]))
        for name, node_role, zone in server_nodes:
            plan.append(
                NodePlanItem(
                    name=name,
                    component="server",
                    role=node_role,
                    zone=zone,
                    vm_size=server.vm_size,
                    data_disk_gb=server.data_disk_gb,
                    disk_type=server.disk_type,
                    join_lb=False,
                    tags={
                        **base_tags,
                        "ctm:component": "server",
                        "ctm:role": node_role,
                        "ctm:server": server.name,
                    },
                    server_key=key,
                )
            )
    return plan


def agent_pools(cfg: EnvironmentConfig, role: str = "primary") -> list[AgentPoolPlanItem]:
    """One agent VMSS per Control-M/Server.

    role='primary': sizes straight from config. role='dr': warm pools —
    min=0, default=dr.warm_agents, max unchanged (the TF DR override).
    """
    if role not in ("primary", "dr"):
        raise ConfigError(f"agent_pools role must be primary or dr, got {role!r}")
    prefix = cfg.name if role == "primary" else cfg.dr_name
    pools: list[AgentPoolPlanItem] = []
    for key, server in cfg.servers.items():
        agents = server.agents
        pools.append(
            AgentPoolPlanItem(
                stage=key,
                name=f"{prefix}-agents-{key}",
                vm_size=agents.vm_size,
                zones=list(agents.zones),
                min=agents.min if role == "primary" else 0,
                max=agents.max,
                default=agents.default if role == "primary" else cfg.dr.warm_agents,
                scale_out_cpu=agents.scale_out_cpu,
                scale_in_cpu=agents.scale_in_cpu,
                use_spot=agents.use_spot,
            )
        )
    return pools


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def find_repo_root(start: str | Path | None = None) -> Path:
    """Locate the repository root (the directory holding environments/).

    Honors CTM_REPO_ROOT, then walks up from `start`, the working directory
    and finally this package's location.
    """
    env_root = os.environ.get("CTM_REPO_ROOT")
    if env_root:
        root = Path(env_root).resolve()
        if not (root / "environments").is_dir():
            raise ConfigError(f"CTM_REPO_ROOT={env_root} has no environments/ directory")
        return root

    bases: list[Path] = []
    if start is not None:
        bases.append(Path(start).resolve())
    bases.append(Path.cwd())
    bases.append(Path(__file__).resolve().parent)
    for base in bases:
        for candidate in (base, *base.parents):
            if (candidate / "environments").is_dir():
                return candidate
    raise ConfigError(
        "could not find the repository root (no environments/ directory above "
        f"{', '.join(str(b) for b in bases)}); set CTM_REPO_ROOT or pass --repo-root"
    )


def _parse_network(raw: Mapping[str, Any], path: str) -> NetworkConfig:
    data = _mapping(raw, path)
    return NetworkConfig(
        vnet_name=_str(data, "vnet_name", path),
        vnet_resource_group_name=_str(data, "vnet_resource_group_name", path),
        app_subnet_name=_str(data, "app_subnet_name", path),
        data_subnet_name=_str(data, "data_subnet_name", path),
        pe_subnet_name=_str(data, "pe_subnet_name", path, default=""),
        existing_private_dns_zone_ids=_str_map(data, "existing_private_dns_zone_ids", path),
    )


def _parse_agents(raw: Mapping[str, Any], path: str) -> AgentsConfig:
    data = _mapping(raw, path)
    agents = AgentsConfig(
        vm_size=_str(data, "vm_size", path),
        min=_int(data, "min", path),
        max=_int(data, "max", path),
        default=_int(data, "default", path),
        zones=_str_list(data, "zones", path, ["1", "2", "3"]),
        use_spot=_bool(data, "use_spot", path, False),
        hostgroup=_str(data, "hostgroup", path, default=""),
        provision_image=_str(data, "provision_image", path, default="Agent.Linux"),
        agent_port=_int(data, "agent_port", path, default=7006),
        os_user=_str(data, "os_user", path, default="ctmagent"),
        scale_out_cpu=_int(data, "scale_out_cpu", path, default=70),
        scale_in_cpu=_int(data, "scale_in_cpu", path, default=25),
    )
    if not (agents.min <= agents.default <= agents.max):
        raise ConfigError(
            f"{path}: expected min <= default <= max, "
            f"got min={agents.min} default={agents.default} max={agents.max}"
        )
    return agents


def _parse_server(key: str, raw: Mapping[str, Any], path: str) -> ServerConfig:
    data = _mapping(raw, path)
    if "agents" not in data:
        raise ConfigError(f"{path}.agents: required mapping is missing")
    return ServerConfig(
        key=key,
        name=_str(data, "name", path, default=f"CTM_{key.upper()}"),
        vm_size=_str(data, "vm_size", path),
        data_disk_gb=_int(data, "data_disk_gb", path, default=256),
        disk_type=_str(data, "disk_type", path, default="Premium_LRS"),
        enable_secondary=_bool(data, "enable_secondary", path, True),
        zones=_str_list(data, "zones", path, ["1", "2"]),
        os_user=_str(data, "os_user", path, default="controlm"),
        agents=_parse_agents(data["agents"], f"{path}.agents"),
    )


def _parse_dr(raw: Mapping[str, Any], path: str) -> DrConfig:
    data = _mapping(raw, path)
    enabled = _bool(data, "enabled", path, False)
    network = None
    if data.get("network") is not None:
        network = _parse_network(data["network"], f"{path}.network")
    dr = DrConfig(
        enabled=enabled,
        location=_str(data, "location", path, default=""),
        network=network,
        warm_agents=_int(data, "warm_agents", path, default=0),
        replica_sku_name=_str(data, "replica_sku_name", path, default=""),
        enable_site_recovery=_bool(data, "enable_site_recovery", path, True),
        traffic_manager=_bool(data, "traffic_manager", path, False),
    )
    if dr.enabled:
        if not dr.location:
            raise ConfigError(f"{path}.location: required when dr.enabled is true")
        if dr.network is None:
            raise ConfigError(f"{path}.network: required when dr.enabled is true")
    return dr


def load_config_file(
    path: str | Path, *, env: str | None = None, allow_placeholders: bool = False
) -> EnvironmentConfig:
    """Parse and validate one config.yaml file into an EnvironmentConfig."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"configuration file not found: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_path}: invalid YAML: {exc}") from exc
    data = _mapping(raw, str(file_path))
    top = "config"

    environment = _str(data, "environment", top)
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"{top}.environment: must be one of {ENVIRONMENTS}, got {environment!r}")
    if env is not None and environment != env:
        raise ConfigError(
            f"{file_path}: environment is {environment!r} but the file belongs to {env!r}"
        )

    subscription_id = _str(data, "subscription_id", top)
    if not _GUID_RE.match(subscription_id):
        raise ConfigError(f"{top}.subscription_id: {subscription_id!r} is not a GUID")
    if subscription_id == PLACEHOLDER_SUBSCRIPTION_ID and not allow_placeholders:
        raise ConfigError(
            f"{top}.subscription_id is the placeholder {PLACEHOLDER_SUBSCRIPTION_ID}; "
            "set the real subscription id in config.yaml (or pass --allow-placeholders "
            "for offline work such as rendering and tests)"
        )

    unique_suffix = _str(data, "unique_suffix", top)
    if not _SUFFIX_RE.match(unique_suffix):
        raise ConfigError(f"{top}.unique_suffix: must match ^[a-z0-9]{{3,6}}$, got {unique_suffix!r}")

    servers_raw = _mapping(data.get("servers"), f"{top}.servers")
    if not servers_raw:
        raise ConfigError(f"{top}.servers: at least one Control-M/Server is required")
    servers = {
        str(key): _parse_server(str(key), value, f"{top}.servers.{key}")
        for key, value in servers_raw.items()
    }

    em_raw = _mapping(data.get("em"), f"{top}.em")
    em = EmConfig(
        vm_size=_str(em_raw, "vm_size", f"{top}.em"),
        data_disk_gb=_int(em_raw, "data_disk_gb", f"{top}.em", default=256),
        disk_type=_str(em_raw, "disk_type", f"{top}.em", default="Premium_LRS"),
        enable_secondary=_bool(em_raw, "enable_secondary", f"{top}.em", True),
        zones=_str_list(em_raw, "zones", f"{top}.em", ["1", "2"]),
        web_port=_int(em_raw, "web_port", f"{top}.em", default=8443),
        os_user=_str(em_raw, "os_user", f"{top}.em", default="emuser"),
        db_name=_str(em_raw, "db_name", f"{top}.em", default="emdb"),
        db_user=_str(em_raw, "db_user", f"{top}.em", default="emuser"),
    )

    db_raw = _mapping(data.get("database"), f"{top}.database")
    database = DatabaseConfig(
        sku_name=_str(db_raw, "sku_name", f"{top}.database"),
        storage_mb=_int(db_raw, "storage_mb", f"{top}.database", default=131072),
        postgres_version=_str(db_raw, "postgres_version", f"{top}.database", default="16"),
        high_availability_mode=_str(
            db_raw, "high_availability_mode", f"{top}.database", default="ZoneRedundant"
        ),
        backup_retention_days=_int(db_raw, "backup_retention_days", f"{top}.database", default=14),
        geo_redundant_backup=_bool(db_raw, "geo_redundant_backup", f"{top}.database", False),
        admin_user=_str(db_raw, "admin_user", f"{top}.database", default="ctmadmin"),
        port=_int(db_raw, "port", f"{top}.database", default=5432),
    )

    storage_raw = _mapping(data.get("storage"), f"{top}.storage")
    storage = StorageConfig(
        blob_replication_type=_str(
            storage_raw, "blob_replication_type", f"{top}.storage", default="RAGZRS"
        ),
        files_replication_type=_str(
            storage_raw, "files_replication_type", f"{top}.storage", default="ZRS"
        ),
        shared_share_quota_gb=_int(
            storage_raw, "shared_share_quota_gb", f"{top}.storage", default=256
        ),
    )

    lb_raw = _mapping(data.get("load_balancer"), f"{top}.load_balancer")
    load_balancer = LoadBalancerConfig(
        internal=_bool(lb_raw, "internal", f"{top}.load_balancer", True),
        public_dns_label=_str(lb_raw, "public_dns_label", f"{top}.load_balancer", default=""),
    )

    backup_raw = _mapping(data.get("backup"), f"{top}.backup")
    backup = BackupConfig(
        enabled=_bool(backup_raw, "enabled", f"{top}.backup", True),
        storage_mode_type=_str(
            backup_raw, "storage_mode_type", f"{top}.backup", default="GeoRedundant"
        ),
        cross_region_restore=_bool(backup_raw, "cross_region_restore", f"{top}.backup", False),
        daily_retention_days=_int(backup_raw, "daily_retention_days", f"{top}.backup", default=30),
        weekly_retention_weeks=_int(
            backup_raw, "weekly_retention_weeks", f"{top}.backup", default=12
        ),
        include_files_share=_bool(backup_raw, "include_files_share", f"{top}.backup", True),
    )

    obs_raw = _mapping(data.get("observability"), f"{top}.observability")
    observability = ObservabilityConfig(
        log_retention_days=_int(obs_raw, "log_retention_days", f"{top}.observability", default=30),
        alert_emails=_str_list(obs_raw, "alert_emails", f"{top}.observability", []),
    )

    blobs = {**DEFAULT_INSTALLER_BLOBS, **_str_map(data, "installer_blobs", top)}
    unknown_blobs = set(blobs) - set(DEFAULT_INSTALLER_BLOBS)
    if unknown_blobs:
        raise ConfigError(f"{top}.installer_blobs: unknown keys {sorted(unknown_blobs)}")

    image_raw = _mapping(data.get("image"), f"{top}.image")
    image = ImageConfig(
        gallery_resource_group=_str(
            image_raw, "gallery_resource_group", f"{top}.image", default="ctm-images-rg"
        ),
        gallery_name=_str(image_raw, "gallery_name", f"{top}.image", default="ctmgallery"),
        image_definition=_str(image_raw, "image_definition", f"{top}.image", default="ctm-rhel9"),
        image_version=_str(image_raw, "image_version", f"{top}.image", default="latest"),
    )

    return EnvironmentConfig(
        subscription_id=subscription_id,
        environment=environment,
        location=_str(data, "location", top),
        unique_suffix=unique_suffix,
        resource_lock=_bool(data, "resource_lock", top, False),
        tags=_str_map(data, "tags", top),
        network=_parse_network(data.get("network"), f"{top}.network"),
        deployer_ip_cidrs=_str_list(data, "deployer_ip_cidrs", top, []),
        key_vault_public_access=_bool(data, "key_vault_public_access", top, True),
        ctm_version=_str(data, "ctm_version", top, default="9.0.21.300"),
        installer_blobs=InstallerBlobs(**blobs),
        image=image,
        em=em,
        servers=servers,
        database=database,
        storage=storage,
        load_balancer=load_balancer,
        backup=backup,
        observability=observability,
        dr=_parse_dr(data.get("dr"), f"{top}.dr"),
    )


def load_config(
    env: str,
    repo_root: str | Path | None = None,
    *,
    allow_placeholders: bool = False,
) -> EnvironmentConfig:
    """Load environments/<env>/config.yaml relative to the repository root."""
    if env not in ENVIRONMENTS:
        raise ConfigError(f"environment must be one of {ENVIRONMENTS}, got {env!r}")
    root = Path(repo_root) if repo_root is not None else find_repo_root()
    return load_config_file(
        Path(root) / "environments" / env / "config.yaml",
        env=env,
        allow_placeholders=allow_placeholders,
    )
