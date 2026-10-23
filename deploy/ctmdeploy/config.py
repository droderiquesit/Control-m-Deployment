# ---------------------------------------------------------------------------
# Typed configuration for the Control-M platform: loads environments/<env>/
# config.yaml (v2 network schema: per-platform utility VNet + one VNet per
# Control-M/Server) into dataclasses and derives naming, per-component
# placement (subnet ids), node plans and agent-pool plans. Pure python:
# this module MUST stay importable without any Azure SDK installed.
# ---------------------------------------------------------------------------
"""Configuration loading, validation and derived values (names, placement, plans).

v2 network model: EMs (and the LB, PostgreSQL, private endpoints) live in a
per-platform *utility VNet*; every Control-M/Server runs in its OWN VNet with
its agent VMSS in that same VNet (unless relocated via the agents_vnet_*
overrides). DR mirrors this with a DR utility VNet plus one DR VNet per
DR-PARTICIPATING server: ``dr.server_networks`` is the DR opt-in set (a
non-empty subset of the servers when dr.enabled; PoC: prod only — uat is
re-deployable and gets no DR VNet, no ASR items, no warm pool). Python
builds subnet/VNet resource ids directly from names — no Azure lookups are
needed for placement.
"""

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
# installer_blobs defaults. EM/Server only (v4.1): the Control-M/Agent is
# BAKED into the ctm-agent Packer image, so no agent media ever reaches the
# platform blob containers. They intentionally do NOT track ctm_version —
# override installer_blobs in config.yaml for other media.
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


def _opt_str(data: Mapping[str, Any], key: str, path: str) -> str:
    """Optional string where an explicit YAML ``null`` means 'not set' ('')."""
    value = data.get(key)
    if value is None:
        return ""
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
# Azure resource-id builders (pure string building, no lookups)
# ---------------------------------------------------------------------------


def vnet_id(subscription_id: str, resource_group_name: str, vnet_name: str) -> str:
    """Full ARM id of an existing (bring-your-own) virtual network."""
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group_name}"
        f"/providers/Microsoft.Network/virtualNetworks/{vnet_name}"
    )


def subnet_id(
    subscription_id: str, resource_group_name: str, vnet_name: str, subnet_name: str
) -> str:
    """Full ARM id of a subnet inside an existing virtual network."""
    return f"{vnet_id(subscription_id, resource_group_name, vnet_name)}/subnets/{subnet_name}"


# ---------------------------------------------------------------------------
# Dataclasses (one per config.yaml section)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VnetRef:
    """A VNet referenced by name + resource group (always pre-existing)."""

    name: str
    resource_group_name: str

    def as_param(self) -> dict[str, str]:
        """Item shape of the bicep `allVnets` array."""
        return {"name": self.name, "resourceGroupName": self.resource_group_name}


@dataclass(frozen=True)
class UtilityNetworkConfig:
    """Per-platform utility VNet: EM VMs + internal LB frontend (em subnet),
    delegated PostgreSQL subnet (data subnet) and private endpoints (pe subnet)."""

    vnet_name: str
    vnet_resource_group_name: str
    em_subnet_name: str
    data_subnet_name: str
    pe_subnet_name: str = ""

    @property
    def ref(self) -> VnetRef:
        return VnetRef(self.vnet_name, self.vnet_resource_group_name)

    def as_param(self) -> dict[str, Any]:
        """Config-shaped `utility` object inside foundation's `network` param."""
        return {
            "vnet_name": self.vnet_name,
            "vnet_resource_group_name": self.vnet_resource_group_name,
            "em_subnet_name": self.em_subnet_name,
            "data_subnet_name": self.data_subnet_name,
            "pe_subnet_name": self.pe_subnet_name,
        }


@dataclass(frozen=True)
class NetworkConfig:
    """v2 regional network: the utility VNet reference plus DNS wiring.

    Used for both the primary region (`network:`) and the DR region
    (`dr.network:`). Per-server VNets live on each server (`servers.<stage>.
    network`) / in `dr.server_networks`, not here.
    """

    utility: UtilityNetworkConfig
    existing_private_dns_zone_ids: dict[str, str] = field(default_factory=dict)
    extra_dns_vnets: tuple[VnetRef, ...] = ()

    def as_param(self) -> dict[str, Any]:
        """Config-shaped object passed to bicep/foundation.bicep `network`."""
        return {
            "utility": self.utility.as_param(),
            "existing_private_dns_zone_ids": dict(self.existing_private_dns_zone_ids),
            "extra_dns_vnets": [ref.as_param() for ref in self.extra_dns_vnets],
        }


@dataclass(frozen=True)
class ServerNetworkConfig:
    """One Control-M/Server's own VNet (REQUIRED per server).

    The agent VMSS lands in `agents_subnet_name` of the same VNet by default;
    the optional agents_vnet_* overrides relocate the agents entirely.
    """

    vnet_name: str
    vnet_resource_group_name: str
    server_subnet_name: str
    agents_subnet_name: str = ""  # '' -> server_subnet_name
    agents_vnet_name: str = ""  # '' -> vnet_name
    agents_vnet_resource_group_name: str = ""  # '' -> vnet_resource_group_name

    @property
    def ref(self) -> VnetRef:
        return VnetRef(self.vnet_name, self.vnet_resource_group_name)

    @property
    def agents_subnet(self) -> str:
        return self.agents_subnet_name or self.server_subnet_name

    @property
    def agents_vnet(self) -> str:
        return self.agents_vnet_name or self.vnet_name

    @property
    def agents_vnet_resource_group(self) -> str:
        if self.agents_vnet_name:
            return self.agents_vnet_resource_group_name or self.vnet_resource_group_name
        return self.vnet_resource_group_name

    @property
    def agents_ref(self) -> VnetRef:
        return VnetRef(self.agents_vnet, self.agents_vnet_resource_group)


@dataclass(frozen=True)
class ImageRef:
    """One gallery image definition + version (the image.base / image.agent shape)."""

    definition: str
    version: str = "latest"


@dataclass(frozen=True)
class ImageConfig:
    """Azure Compute Gallery images the VMs/VMSS boot from (built by Packer).

    v4.1 two-image model: ``base`` (ctm-rhel9, OS only — EM/Server VMs) and
    ``agent`` (ctm-agent, OS prep + the Control-M/Agent silent-installed but
    unconfigured — the agent VM scale sets).
    """

    gallery_resource_group: str = "ctm-images-rg"
    gallery_name: str = "ctmgallery"
    base: ImageRef = field(default_factory=lambda: ImageRef("ctm-rhel9"))
    agent: ImageRef = field(default_factory=lambda: ImageRef("ctm-agent"))

    def ref(self, kind: str) -> ImageRef:
        if kind not in ("base", "agent"):
            raise ConfigError(f"image kind must be base or agent, got {kind!r}")
        return self.base if kind == "base" else self.agent

    def image_id(self, subscription_id: str, kind: str = "base") -> str:
        """Full ARM id of the gallery image version ('latest' is ARM-valid)."""
        ref = self.ref(kind)
        return (
            f"/subscriptions/{subscription_id}"
            f"/resourceGroups/{self.gallery_resource_group}"
            f"/providers/Microsoft.Compute/galleries/{self.gallery_name}"
            f"/images/{ref.definition}/versions/{ref.version}"
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
    network: ServerNetworkConfig
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
    """Disaster-recovery region settings; enabled=false skips DR entirely.

    v2: `network.utility` is the DR utility VNet (failed-over EM target,
    replica data subnet, PEs, DR LB); `server_networks` is the DR OPT-IN
    set — a server participates in DR if and only if it has an entry here
    (a non-empty SUBSET of the servers keys when enabled; servers absent
    from it are simply not DR-protected: no ASR items, no DR VNet, no warm
    agent pool).
    """

    enabled: bool = False
    location: str = ""
    network: NetworkConfig | None = None
    server_networks: dict[str, ServerNetworkConfig] = field(default_factory=dict)
    warm_agents: int = 0
    replica_sku_name: str = ""
    enable_site_recovery: bool = True
    traffic_manager: bool = False


@dataclass(frozen=True)
class InstallerBlobs:
    """Blob names (inside the installers container) uploaded from BMC EPD.

    EM/Server only — the agent media feeds the ctm-agent Packer build (v4.1)
    and never reaches the platform blob containers.
    """

    em_installer: str
    server_installer: str
    em_silent_primary: str
    em_silent_secondary: str
    server_silent_primary: str
    server_silent_secondary: str


# ---------------------------------------------------------------------------
# v4: access layer (Bastion + private Application Gateway), Key Vault ->
# Control-M sync function, and the Azure DevOps build-agent scale set.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessConfig:
    """The `access:` block — Bastion and the private Application Gateway.

    `appgw` is parsed for every environment but HONORED on prod only (the
    gateway balances across the primary + DR internal load balancers, which
    only prod has).
    """

    bastion_enabled: bool = False
    appgw_enabled: bool = False

    @property
    def any_enabled(self) -> bool:
        return self.bastion_enabled or self.appgw_enabled


@dataclass(frozen=True)
class KvSyncConfig:
    """The `kv_sync:` block — the Key Vault -> Control-M Durable Function."""

    enabled: bool = False
    schedule: str = "0 */15 * * * *"  # NCRONTAB, every 15 minutes


@dataclass(frozen=True)
class BuildAgentsConfig:
    """The `build_agents:` block — the Azure DevOps build-agent VMSS (prod).

    The scale set lands in its own VNet (vnet-ctm-build), peered to the
    nonprod, prod and DR utility VNets so one elastic pool can deploy into
    all three platforms.
    """

    enabled: bool = False
    resource_group: str = "ctm-build-rg"
    vm_size: str = "Standard_D4s_v5"
    instances: int = 2
    vnet_name: str = ""
    vnet_resource_group_name: str = ""
    subnet_name: str = ""


# ---------------------------------------------------------------------------
# v3/v4: optional full-network infrastructure (network/main.bicep, the
# standalone folder the cloud team can deploy with az alone) — BYO stays
# the default (`create: false`); the vnets list then documents the PoC plan.
# ---------------------------------------------------------------------------

#: Valid `role` values of a network_infrastructure.vnets entry. 'external' is
#: resolution-only (name + resource group + address space of a VNet ANOTHER
#: plan deploys — e.g. the prod config declaring the nonprod vnet-dev-utility
#: so the build VNet peering resolves); nothing is created for it and it
#: carries no subnets.
VNET_ROLES = ("utility", "spoke", "build", "external")
#: Valid `role` values of a subnet inside a vnets entry (drives the NSG rules).
SUBNET_ROLES = ("em", "data", "pe", "server", "agents", "bastion", "appgw", "func", "build")
#: Azure requires the Bastion subnet to carry exactly this name (no NSG here).
BASTION_SUBNET_NAME = "AzureBastionSubnet"


@dataclass(frozen=True)
class SubnetSpec:
    """One subnet of a to-be-created VNet; `role` selects its NSG rule set.

    v4 roles: `bastion` (must be named AzureBastionSubnet, gets NO NSG),
    `appgw` (Application Gateway v2 NSG: GatewayManager 65200-65535 +
    user-facing 443/8443), `func` (Microsoft.Web/serverFarms delegation for
    the kvsync Function's VNet integration) and `build` (SSH from bastion/
    utility only; the ADO build-agent scale set).
    """

    name: str
    prefix: str
    role: str  # one of SUBNET_ROLES

    def as_param(self) -> dict[str, str]:
        return {"name": self.name, "prefix": self.prefix, "role": self.role}


@dataclass(frozen=True)
class VnetSpec:
    """One VNet of the optional full-network deployment.

    role 'utility' hosts the em/data/pe (+ bastion/appgw/func) subnets;
    role 'spoke' hosts a server/agents pair and MUST name its utility VNet
    in `peer_to`; role 'build' (v4) hosts the ADO build agents and peers to
    EVERY utility VNet it lists — peer_to entries may name VNets from the
    OTHER environment's config (cross-environment peering by VNet name; the
    network deployment builds the remote id from name + resource group);
    role 'external' (v4.1) is a RESOLUTION-ONLY entry — the name, resource
    group and address space of a VNet another plan deploys (no subnets, no
    peer_to of its own; nothing is created for it), so peer_to names like the
    prod build VNet -> nonprod vnet-dev-utility resolve on the ctmdeploy path
    too. `peer_to` accepts a string or a list in YAML; it is normalized to a
    tuple here.
    """

    name: str
    resource_group_name: str
    location: str
    role: str  # one of VNET_ROLES
    address_space: str
    subnets: tuple[SubnetSpec, ...]
    peer_to: tuple[str, ...] = ()  # spokes: exactly their utility; build: 1+

    def subnet(self, name: str) -> SubnetSpec | None:
        return next((s for s in self.subnets if s.name == name), None)

    def subnet_by_role(self, role: str) -> SubnetSpec | None:
        return next((s for s in self.subnets if s.role == role), None)


@dataclass(frozen=True)
class NetworkInfrastructure:
    """The optional `network_infrastructure:` block.

    `create: false` (the default) means bring-your-own networking: the block
    is inert documentation of the PoC address plan and nothing is validated
    beyond its shape. Flipping `create: true` makes `ctmdeploy deploy --phase
    network` provision every listed VNet — and makes load-time validation
    cross-check the placement blocks against this list.
    """

    create: bool = False
    user_access_cidrs: tuple[str, ...] = ()  # allowed to reach Control-M Web/API
    bastion_cidrs: tuple[str, ...] = ()  # allowed SSH 22 to nodes
    vnets: tuple[VnetSpec, ...] = ()

    def vnet(self, name: str) -> VnetSpec | None:
        return next((v for v in self.vnets if v.name == name), None)

    def spoke_address_spaces(self) -> list[str]:
        """Address spaces of ALL spoke VNets (the bicep NSG rules allow e.g.
        postgres 5432 from every spoke, so DR failback keeps working)."""
        return [v.address_space for v in self.vnets if v.role == "spoke"]

    def vnets_param(self) -> list[dict[str, Any]]:
        """Items of the network/main.bicep `vnets` param: the RAW config
        shape, with `peer_to` normalized to a list. All peer/spoke address-
        space resolution now happens INSIDE network/main.bicep (bicep
        map/filter), so the cloud team can deploy the folder with nothing
        but the az CLI — no python required.
        """
        return [
            {
                "name": vnet.name,
                "resource_group_name": vnet.resource_group_name,
                "location": vnet.location,
                "role": vnet.role,
                "peer_to": list(vnet.peer_to),
                "address_space": vnet.address_space,
                "subnets": [s.as_param() for s in vnet.subnets],
            }
            for vnet in self.vnets
        ]


# ---------------------------------------------------------------------------
# v3: installer media via Azure Artifacts Universal Packages (optional block;
# the pipeline downloads the packages, `ctmdeploy media upload` pushes them
# to the installers blob container of each platform).
# ---------------------------------------------------------------------------

#: Required package keys under media.azure_artifacts.packages. v4.1: the
#: `agent` package feeds the PACKER ctm-agent image build (pipeline image
#: stage), NOT the platform blob containers; em/server/silent_xml are what
#: `ctmdeploy media upload` publishes to blob.
MEDIA_PACKAGE_KEYS = ("em", "server", "agent", "silent_xml")


@dataclass(frozen=True)
class MediaPackageConfig:
    """One Azure Artifacts universal package (name + pinned version)."""

    name: str
    version: str


@dataclass(frozen=True)
class AzureArtifactsConfig:
    """The Azure Artifacts feed holding the BMC EPD media packages."""

    organization: str  # e.g. https://dev.azure.com/<org>
    project: str
    feed: str
    packages: dict[str, MediaPackageConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaConfig:
    """The optional `media:` block (absent -> azure_artifacts is None)."""

    azure_artifacts: AzureArtifactsConfig | None = None


# ---------------------------------------------------------------------------
# Plans handed to bicep/compute.bicep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodePlanItem:
    """One EM or Control-M/Server VM in the compute deployment's nodePlan.

    v2: each node carries its own `subnet_id` (EM -> utility em subnet,
    Server -> its own VNet's server subnet). `is_new` (passed at to_bicep
    time) gates immutable-at-create properties such as customData.
    """

    name: str
    component: str  # 'em' | 'server'
    role: str  # 'primary' | 'secondary'
    zone: str
    vm_size: str
    data_disk_gb: int
    disk_type: str
    join_lb: bool
    tags: dict[str, str]
    subnet_id: str
    server_key: str = ""  # '' for EM nodes

    def to_bicep(self, custom_data: str, *, is_new: bool = True) -> dict[str, Any]:
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
            "subnetId": self.subnet_id,
            "isNew": is_new,
        }


@dataclass(frozen=True)
class AgentPoolPlanItem:
    """One agent VMSS in the compute deployment's agentPools.

    v2: carries its own `subnet_id` (the server's agents subnet — same VNet
    as the server unless relocated). `is_new` (passed at to_bicep time) lets
    bicep set sku.capacity only at create; autoscale owns the count afterwards.
    """

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
    subnet_id: str

    def to_bicep(self, custom_data: str, *, is_new: bool = True) -> dict[str, Any]:
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
            "subnetId": self.subnet_id,
            "isNew": is_new,
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
    # v3 optional blocks (absent from config.yaml -> inert defaults).
    network_infrastructure: NetworkInfrastructure = field(default_factory=NetworkInfrastructure)
    media: MediaConfig = field(default_factory=MediaConfig)
    # v4 optional blocks: access layer, KV->Control-M sync, ADO build agents.
    access: AccessConfig = field(default_factory=AccessConfig)
    kv_sync: KvSyncConfig = field(default_factory=KvSyncConfig)
    build_agents: BuildAgentsConfig = field(default_factory=BuildAgentsConfig)

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

    def image_id(self, kind: str = "base") -> str:
        """ARM id of a gallery image version: 'base' (EM/Server VMs) or
        'agent' (the agent VM scale sets — the ctm-agent Packer image)."""
        return self.image.image_id(self.subscription_id, kind)

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

    # ---- v2 network placement (ids are built from names, never looked up) --

    def vnet_id(self, resource_group_name: str, vnet_name: str) -> str:
        """ARM id of a VNet in this subscription."""
        return vnet_id(self.subscription_id, resource_group_name, vnet_name)

    def subnet_id(self, resource_group_name: str, vnet_name: str, subnet_name: str) -> str:
        """ARM id of a subnet in this subscription."""
        return subnet_id(self.subscription_id, resource_group_name, vnet_name, subnet_name)

    def region_network(self, role: str = "primary") -> NetworkConfig:
        """The regional network block: `network` (primary) or `dr.network`."""
        if role == "primary":
            return self.network
        if self.dr.network is None:
            raise ConfigError("dr.network is required for the DR region")
        return self.dr.network

    def dr_servers(self) -> list[str]:
        """Stage keys of the DR-PARTICIPATING servers (the `dr.server_networks`
        opt-in set), in servers order. Servers absent from the set are not
        DR-protected (no ASR items, no DR VNet, no warm agent pool)."""
        return [key for key in self.servers if key in self.dr.server_networks]

    def server_network(self, key: str, role: str = "primary") -> ServerNetworkConfig:
        """One server's own VNet in a region (`servers.<key>.network` or
        `dr.server_networks.<key>`)."""
        if key not in self.servers:
            raise ConfigError(f"unknown server key {key!r}; expected one of {sorted(self.servers)}")
        if role == "primary":
            return self.servers[key].network
        try:
            return self.dr.server_networks[key]
        except KeyError:
            raise ConfigError(
                f"dr.server_networks.{key}: server {key!r} is not DR-protected "
                "(dr.server_networks is the DR opt-in set; add an entry to protect it)"
            ) from None

    def em_subnet_id(self, role: str = "primary") -> str:
        """EM VMs + internal LB frontend -> the region's utility em subnet."""
        utility = self.region_network(role).utility
        return self.subnet_id(
            utility.vnet_resource_group_name, utility.vnet_name, utility.em_subnet_name
        )

    def server_subnet_id(self, key: str, role: str = "primary") -> str:
        """Server VMs -> the server's own VNet's server subnet."""
        net = self.server_network(key, role)
        return self.subnet_id(net.vnet_resource_group_name, net.vnet_name, net.server_subnet_name)

    def agents_subnet_id(self, key: str, role: str = "primary") -> str:
        """Agent VMSS -> the server's agents subnet (same VNet unless relocated)."""
        net = self.server_network(key, role)
        return self.subnet_id(net.agents_vnet_resource_group, net.agents_vnet, net.agents_subnet)

    # ---- v4 access-layer placement (utility VNet subnets by ROLE, resolved
    # through the network_infrastructure plan — the plan documents the same
    # subnets whether networking is created by network/ or brought-your-own).

    def _utility_role_subnet(self, subnet_role: str, region: str = "primary") -> tuple[str, str, str]:
        """(resource group, vnet, subnet name) of the region's utility subnet
        carrying `subnet_role` in the network_infrastructure plan."""
        utility = self.region_network(region).utility
        plan = self.network_infrastructure.vnet(utility.vnet_name)
        if plan is None:
            raise ConfigError(
                f"network_infrastructure.vnets has no entry for the {region} utility VNet "
                f"{utility.vnet_name!r}; the access layer derives its subnet ids from that "
                "plan (add the VNet with its bastion/appgw/func subnets)"
            )
        subnet = plan.subnet_by_role(subnet_role)
        if subnet is None:
            raise ConfigError(
                f"network_infrastructure.vnets[{utility.vnet_name}] has no role-"
                f"{subnet_role!r} subnet; add it to the plan (see docs/NETWORK-REQUIREMENTS.md)"
            )
        return (utility.vnet_resource_group_name, utility.vnet_name, subnet.name)

    def bastion_subnet_id(self, region: str = "primary") -> str:
        """AzureBastionSubnet in the region's utility VNet."""
        rg, vnet, subnet = self._utility_role_subnet("bastion", region)
        return self.subnet_id(rg, vnet, subnet)

    def appgw_subnet_id(self, region: str = "primary") -> str:
        """The Application Gateway subnet in the region's utility VNet."""
        rg, vnet, subnet = self._utility_role_subnet("appgw", region)
        return self.subnet_id(rg, vnet, subnet)

    def func_subnet_id(self, region: str = "primary") -> str:
        """The delegated Function-integration subnet in the utility VNet."""
        rg, vnet, subnet = self._utility_role_subnet("func", region)
        return self.subnet_id(rg, vnet, subnet)

    def build_agents_subnet_id(self) -> str:
        """The build-agent VMSS subnet (from the build_agents.network block)."""
        ba = self.build_agents
        if not (ba.vnet_name and ba.vnet_resource_group_name and ba.subnet_name):
            raise ConfigError(
                "build_agents.network is incomplete: vnet_name, "
                "vnet_resource_group_name and subnet_name are all required"
            )
        return self.subnet_id(ba.vnet_resource_group_name, ba.vnet_name, ba.subnet_name)

    @property
    def key_vault_id(self) -> str:
        """ARM id of the primary Key Vault (names are deterministic)."""
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group_name}"
            f"/providers/Microsoft.KeyVault/vaults/{self.key_vault_name}"
        )

    @property
    def ctm_api_base(self) -> str:
        """EM Automation API base URL through the internal load balancer."""
        return f"https://{self.web_alias}:{self.em.web_port}/automation-api"

    @property
    def kvsync_function_app_name(self) -> str:
        return f"{self.name}-kvsync"

    def all_vnets(self, role: str = "primary") -> list[dict[str, str]]:
        """Deduped `{name, resourceGroupName}` list of EVERY VNet in a region:
        utility + each server VNet + agent relocation VNets + extra_dns_vnets
        (order preserved). This is the bicep `allVnets` param — each created
        privatelink zone gets one VNet link per entry. In the DR region only
        the DR-participating servers (`dr.server_networks`) contribute VNets."""
        region = self.region_network(role)
        refs: list[VnetRef] = [region.utility.ref]
        keys = list(self.servers) if role == "primary" else self.dr_servers()
        for key in keys:
            net = self.server_network(key, role)
            refs.append(net.ref)
            refs.append(net.agents_ref)
        refs.extend(region.extra_dns_vnets)
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, str]] = []
        for ref in refs:
            ident = (ref.name, ref.resource_group_name)
            if ident not in seen:
                seen.add(ident)
                deduped.append(ref.as_param())
        return deduped

    def dns_vnet_links(self, role: str = "primary") -> list[dict[str, str]]:
        """`{name, vnetId}` items for the platform DNS zone's registration-
        enabled links (bicep `dnsVnetLinks`): one per VNet in the region."""
        return [
            {"name": ref["name"], "vnetId": self.vnet_id(ref["resourceGroupName"], ref["name"])}
            for ref in self.all_vnets(role)
        ]


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

    em_subnet = cfg.em_subnet_id(role)
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
                subnet_id=em_subnet,
            )
        )

    for key, server in cfg.servers.items():
        server_subnet = cfg.server_subnet_id(key, role)
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
                    subnet_id=server_subnet,
                    server_key=key,
                )
            )
    return plan


def agent_pools(cfg: EnvironmentConfig, role: str = "primary") -> list[AgentPoolPlanItem]:
    """One agent VMSS per Control-M/Server.

    role='primary': one pool per server, sizes straight from config, placed
    in each server's agents subnet (same VNet as the server unless
    relocated). role='dr': warm pools for the DR-PARTICIPATING servers only
    (`dr.server_networks`, the opt-in set) — min=0, default=dr.warm_agents,
    max unchanged (the TF DR override) — placed in
    `dr.server_networks.<stage>` agents subnets.
    """
    if role not in ("primary", "dr"):
        raise ConfigError(f"agent_pools role must be primary or dr, got {role!r}")
    prefix = cfg.name if role == "primary" else cfg.dr_name
    pools: list[AgentPoolPlanItem] = []
    keys = list(cfg.servers) if role == "primary" else cfg.dr_servers()
    for key in keys:
        server = cfg.servers[key]
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
                subnet_id=cfg.agents_subnet_id(key, role),
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


def _parse_vnet_ref(raw: Any, path: str) -> VnetRef:
    data = _mapping(raw, path)
    return VnetRef(
        name=_str(data, "vnet_name", path),
        resource_group_name=_str(data, "vnet_resource_group_name", path),
    )


def _parse_utility(raw: Any, path: str) -> UtilityNetworkConfig:
    data = _mapping(raw, path)
    if not data:
        raise ConfigError(f"{path}: required mapping is missing (v2 network schema)")
    return UtilityNetworkConfig(
        vnet_name=_str(data, "vnet_name", path),
        vnet_resource_group_name=_str(data, "vnet_resource_group_name", path),
        em_subnet_name=_str(data, "em_subnet_name", path),
        data_subnet_name=_str(data, "data_subnet_name", path),
        pe_subnet_name=_opt_str(data, "pe_subnet_name", path),
    )


def _parse_network(raw: Any, path: str) -> NetworkConfig:
    data = _mapping(raw, path)
    if "utility" not in data:
        raise ConfigError(
            f"{path}.utility: required mapping is missing "
            "(v2 network schema: the per-platform utility VNet)"
        )
    extra_raw = data.get("extra_dns_vnets") or []
    if not isinstance(extra_raw, (list, tuple)):
        raise ConfigError(f"{path}.extra_dns_vnets: expected a list")
    return NetworkConfig(
        utility=_parse_utility(data["utility"], f"{path}.utility"),
        existing_private_dns_zone_ids=_str_map(data, "existing_private_dns_zone_ids", path),
        extra_dns_vnets=tuple(
            _parse_vnet_ref(item, f"{path}.extra_dns_vnets[{i}]")
            for i, item in enumerate(extra_raw)
        ),
    )


def _parse_server_network(raw: Any, path: str) -> ServerNetworkConfig:
    data = _mapping(raw, path)
    if not data:
        raise ConfigError(
            f"{path}: required mapping is missing (v2: every server has its own VNet)"
        )
    net = ServerNetworkConfig(
        vnet_name=_str(data, "vnet_name", path),
        vnet_resource_group_name=_str(data, "vnet_resource_group_name", path),
        server_subnet_name=_str(data, "server_subnet_name", path),
        agents_subnet_name=_opt_str(data, "agents_subnet_name", path),
        agents_vnet_name=_opt_str(data, "agents_vnet_name", path),
        agents_vnet_resource_group_name=_opt_str(data, "agents_vnet_resource_group_name", path),
    )
    if net.agents_vnet_resource_group_name and not net.agents_vnet_name:
        raise ConfigError(
            f"{path}.agents_vnet_resource_group_name: requires agents_vnet_name "
            "(set both to relocate the agents, neither to keep them with the server)"
        )
    return net


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
    if data.get("network") is None:
        raise ConfigError(
            f"{path}.network: required mapping is missing "
            "(v2: every Control-M/Server declares its own VNet)"
        )
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
        network=_parse_server_network(data["network"], f"{path}.network"),
    )


def _parse_dr(raw: Mapping[str, Any], path: str, server_keys: list[str]) -> DrConfig:
    data = _mapping(raw, path)
    enabled = _bool(data, "enabled", path, False)
    network = None
    if data.get("network") is not None:
        network = _parse_network(data["network"], f"{path}.network")
    server_networks_raw = _mapping(data.get("server_networks"), f"{path}.server_networks")
    server_networks = {
        str(key): _parse_server_network(value, f"{path}.server_networks.{key}")
        for key, value in server_networks_raw.items()
    }
    dr = DrConfig(
        enabled=enabled,
        location=_str(data, "location", path, default=""),
        network=network,
        server_networks=server_networks,
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
        # DR OPT-IN set: a non-empty SUBSET of the servers keys. Servers absent
        # from it are simply not DR-protected (no ASR items, no warm pool).
        if not dr.server_networks:
            raise ConfigError(
                f"{path}.server_networks: at least one DR-protected server is "
                "required when dr.enabled is true (the DR opt-in set)"
            )
        unknown = sorted(key for key in dr.server_networks if key not in server_keys)
        if unknown:
            raise ConfigError(
                f"{path}.server_networks: unknown server keys {unknown} "
                f"(configured servers: {sorted(server_keys)})"
            )
    return dr


def _parse_subnet_spec(raw: Any, path: str) -> SubnetSpec:
    data = _mapping(raw, path)
    spec = SubnetSpec(
        name=_str(data, "name", path),
        prefix=_str(data, "prefix", path),
        role=_str(data, "role", path),
    )
    if spec.role not in SUBNET_ROLES:
        raise ConfigError(f"{path}.role: must be one of {SUBNET_ROLES}, got {spec.role!r}")
    # Azure hard-codes the Bastion subnet name; catch the drift at load time.
    if spec.role == "bastion" and spec.name != BASTION_SUBNET_NAME:
        raise ConfigError(
            f"{path}.name: a role-'bastion' subnet must be named exactly "
            f"{BASTION_SUBNET_NAME!r} (Azure requirement), got {spec.name!r}"
        )
    if spec.name == BASTION_SUBNET_NAME and spec.role != "bastion":
        raise ConfigError(
            f"{path}.role: subnet {BASTION_SUBNET_NAME!r} must carry role 'bastion', "
            f"got {spec.role!r}"
        )
    return spec


def _parse_peer_to(data: Mapping[str, Any], path: str) -> tuple[str, ...]:
    """`peer_to` is a string OR a list of strings (v4); '' / absent -> ()."""
    value = data.get("peer_to")
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        items = tuple(str(item) for item in value)
        if len(set(items)) != len(items):
            raise ConfigError(f"{path}.peer_to: duplicate entries in {list(items)}")
        return items
    raise ConfigError(
        f"{path}.peer_to: expected a string or a list of strings, "
        f"got {type(value).__name__}"
    )


def _parse_vnet_spec(raw: Any, path: str) -> VnetSpec:
    data = _mapping(raw, path)
    role = _str(data, "role", path)
    if role not in VNET_ROLES:
        raise ConfigError(f"{path}.role: must be one of {VNET_ROLES}, got {role!r}")
    subnets_raw = data.get("subnets")
    if role == "external":
        # Resolution-only: name + resource group + address space; another
        # plan deploys the VNet, so declaring subnets or peer_to here would
        # silently do nothing — reject the drift instead.
        if subnets_raw:
            raise ConfigError(
                f"{path}.subnets: a role-'external' entry is resolution-only "
                "(name, resource_group_name, location, address_space) and "
                "must not declare subnets — the plan that OWNS the VNet does"
            )
        if data.get("peer_to"):
            raise ConfigError(
                f"{path}.peer_to: a role-'external' entry declares no peerings "
                "of its own; the peering is declared on the local VNet that "
                "names this entry in ITS peer_to"
            )
        subnets_raw = []
    elif not isinstance(subnets_raw, (list, tuple)) or not subnets_raw:
        raise ConfigError(f"{path}.subnets: expected a non-empty list of subnets")
    return VnetSpec(
        name=_str(data, "name", path),
        resource_group_name=_str(data, "resource_group_name", path),
        location=_str(data, "location", path),
        role=role,
        address_space=_str(data, "address_space", path),
        subnets=tuple(
            _parse_subnet_spec(item, f"{path}.subnets[{i}]") for i, item in enumerate(subnets_raw)
        ),
        peer_to=_parse_peer_to(data, path),
    )


def _parse_network_infrastructure(raw: Any, path: str) -> NetworkInfrastructure:
    data = _mapping(raw, path)
    if not data:
        return NetworkInfrastructure()
    vnets_raw = data.get("vnets") or []
    if not isinstance(vnets_raw, (list, tuple)):
        raise ConfigError(f"{path}.vnets: expected a list, got {type(vnets_raw).__name__}")
    infra = NetworkInfrastructure(
        create=_bool(data, "create", path, False),
        user_access_cidrs=tuple(_str_list(data, "user_access_cidrs", path, [])),
        bastion_cidrs=tuple(_str_list(data, "bastion_cidrs", path, [])),
        vnets=tuple(
            _parse_vnet_spec(item, f"{path}.vnets[{i}]") for i, item in enumerate(vnets_raw)
        ),
    )
    seen: set[str] = set()
    for vnet in infra.vnets:
        if vnet.name in seen:
            raise ConfigError(f"{path}.vnets: duplicate VNet name {vnet.name!r}")
        seen.add(vnet.name)
    if infra.create and not infra.vnets:
        raise ConfigError(
            f"{path}.vnets: at least one VNet is required when {path}.create is true"
        )
    return infra


def _parse_media(raw: Any, path: str) -> MediaConfig:
    data = _mapping(raw, path)
    if not data or data.get("azure_artifacts") is None:
        return MediaConfig()
    aa_path = f"{path}.azure_artifacts"
    aa = _mapping(data["azure_artifacts"], aa_path)
    packages_raw = _mapping(aa.get("packages"), f"{aa_path}.packages")
    packages: dict[str, MediaPackageConfig] = {}
    for key, value in packages_raw.items():
        pkg_path = f"{aa_path}.packages.{key}"
        pkg = _mapping(value, pkg_path)
        packages[str(key)] = MediaPackageConfig(
            name=_str(pkg, "name", pkg_path), version=_str(pkg, "version", pkg_path)
        )
    unknown = sorted(set(packages) - set(MEDIA_PACKAGE_KEYS))
    if unknown:
        raise ConfigError(
            f"{aa_path}.packages: unknown keys {unknown}; expected a subset of "
            f"{list(MEDIA_PACKAGE_KEYS)}"
        )
    missing = [key for key in MEDIA_PACKAGE_KEYS if key not in packages]
    if missing:
        raise ConfigError(f"{aa_path}.packages: missing required packages {missing}")
    return MediaConfig(
        azure_artifacts=AzureArtifactsConfig(
            organization=_str(aa, "organization", aa_path),
            project=_str(aa, "project", aa_path),
            feed=_str(aa, "feed", aa_path),
            packages=packages,
        )
    )


def _parse_image(raw: Any, path: str) -> ImageConfig:
    """The nested v4.1 `image:` block (two-image model).

    Shape::

        image:
          gallery_resource_group: ctm-images-rg
          gallery_name: ctmgallery
          base:  {definition: ctm-rhel9, version: latest}   # EM/Server VMs
          agent: {definition: ctm-agent, version: latest}   # agent VMSS

    The pre-v4.1 flat keys (image_definition / image_version) are rejected
    with a pointer to the new shape.
    """
    data = _mapping(raw, path)
    old_keys = sorted(set(data) & {"image_definition", "image_version"})
    if old_keys:
        raise ConfigError(
            f"{path}: the flat keys {old_keys} were replaced by the v4.1 nested "
            f"shape — use {path}.base {{definition, version}} for the EM/Server "
            f"image and {path}.agent {{definition, version}} for the agent VMSS "
            "image (see docs/CONFIGURATION.md)"
        )
    unknown = sorted(set(data) - {"gallery_resource_group", "gallery_name", "base", "agent"})
    if unknown:
        raise ConfigError(
            f"{path}: unknown keys {unknown}; expected gallery_resource_group, "
            "gallery_name, base, agent"
        )

    def ref(key: str, default_definition: str) -> ImageRef:
        ref_raw = _mapping(data.get(key), f"{path}.{key}")
        return ImageRef(
            definition=_str(ref_raw, "definition", f"{path}.{key}", default=default_definition),
            version=_str(ref_raw, "version", f"{path}.{key}", default="latest"),
        )

    return ImageConfig(
        gallery_resource_group=_str(
            data, "gallery_resource_group", path, default="ctm-images-rg"
        ),
        gallery_name=_str(data, "gallery_name", path, default="ctmgallery"),
        base=ref("base", "ctm-rhel9"),
        agent=ref("agent", "ctm-agent"),
    )


def _parse_access(raw: Any, path: str) -> AccessConfig:
    data = _mapping(raw, path)
    if not data:
        return AccessConfig()
    bastion = _mapping(data.get("bastion"), f"{path}.bastion")
    appgw = _mapping(data.get("appgw"), f"{path}.appgw")
    unknown = sorted(set(data) - {"bastion", "appgw"})
    if unknown:
        raise ConfigError(f"{path}: unknown keys {unknown}; expected bastion/appgw")
    return AccessConfig(
        bastion_enabled=_bool(bastion, "enabled", f"{path}.bastion", False),
        appgw_enabled=_bool(appgw, "enabled", f"{path}.appgw", False),
    )


def _parse_kv_sync(raw: Any, path: str) -> KvSyncConfig:
    data = _mapping(raw, path)
    if not data:
        return KvSyncConfig()
    sync = KvSyncConfig(
        enabled=_bool(data, "enabled", path, False),
        schedule=_str(data, "schedule", path, default="0 */15 * * * *"),
    )
    # NCRONTAB has six fields (seconds first); catch the classic 5-field slip.
    if sync.enabled and len(sync.schedule.split()) != 6:
        raise ConfigError(
            f"{path}.schedule: {sync.schedule!r} is not a 6-field NCRONTAB "
            "expression (Azure Functions timers include a seconds field)"
        )
    return sync


def _parse_build_agents(raw: Any, path: str) -> BuildAgentsConfig:
    data = _mapping(raw, path)
    if not data:
        return BuildAgentsConfig()
    network = _mapping(data.get("network"), f"{path}.network")
    agents = BuildAgentsConfig(
        enabled=_bool(data, "enabled", path, False),
        resource_group=_str(data, "resource_group", path, default="ctm-build-rg"),
        vm_size=_str(data, "vm_size", path, default="Standard_D4s_v5"),
        instances=_int(data, "instances", path, default=2),
        vnet_name=_opt_str(network, "vnet_name", f"{path}.network"),
        vnet_resource_group_name=_opt_str(
            network, "vnet_resource_group_name", f"{path}.network"
        ),
        subnet_name=_opt_str(network, "subnet_name", f"{path}.network"),
    )
    if agents.enabled:
        if agents.instances < 1:
            raise ConfigError(f"{path}.instances: must be >= 1 when enabled")
        missing = [
            key
            for key, value in (
                ("vnet_name", agents.vnet_name),
                ("vnet_resource_group_name", agents.vnet_resource_group_name),
                ("subnet_name", agents.subnet_name),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"{path}.network: required keys missing when enabled: {missing}"
            )
    return agents


# ---------------------------------------------------------------------------
# network_infrastructure validation (runs only when create is true)
# ---------------------------------------------------------------------------


def _placement_refs(cfg: EnvironmentConfig) -> list[tuple[str, str, str, frozenset[str], str]]:
    """Every (vnet, resource group, subnet, allowed subnet roles, config path)
    the placement blocks reference: the utility em/data/pe subnets plus each
    server's server/agents subnets — primary AND DR regions."""
    refs: list[tuple[str, str, str, frozenset[str], str]] = []

    def add_utility(utility: UtilityNetworkConfig, path: str) -> None:
        vnet, rg = utility.vnet_name, utility.vnet_resource_group_name
        refs.append((vnet, rg, utility.em_subnet_name, frozenset({"em"}), f"{path}.em_subnet_name"))
        refs.append(
            (vnet, rg, utility.data_subnet_name, frozenset({"data"}), f"{path}.data_subnet_name")
        )
        if utility.pe_subnet_name:
            refs.append(
                (vnet, rg, utility.pe_subnet_name, frozenset({"pe"}), f"{path}.pe_subnet_name")
            )

    def add_server(net: ServerNetworkConfig, path: str) -> None:
        refs.append(
            (
                net.vnet_name,
                net.vnet_resource_group_name,
                net.server_subnet_name,
                frozenset({"server"}),
                f"{path}.server_subnet_name",
            )
        )
        # Agents default into the server subnet, so 'server' stays allowed.
        refs.append(
            (
                net.agents_vnet,
                net.agents_vnet_resource_group,
                net.agents_subnet,
                frozenset({"agents", "server"}),
                f"{path} (agents placement)",
            )
        )

    add_utility(cfg.network.utility, "config.network.utility")
    for key, server in cfg.servers.items():
        add_server(server.network, f"config.servers.{key}.network")
    if cfg.dr.enabled and cfg.dr.network is not None:
        add_utility(cfg.dr.network.utility, "config.dr.network.utility")
        for key, net in cfg.dr.server_networks.items():
            add_server(net, f"config.dr.server_networks.{key}")
    return refs


def _validate_network_infrastructure(cfg: EnvironmentConfig) -> None:
    """Cross-check the network_infrastructure block (create: true only).

    Enforces: parseable, pairwise non-overlapping VNet address spaces;
    subnets that parse, sit inside their VNet and do not overlap each other;
    spokes that peer to an existing utility VNet; parseable access CIDRs;
    and that EVERY placement reference (utility + each server/agents VNet +
    subnet, including the DR region) resolves to a declared VNet + subnet.
    """
    import ipaddress

    infra = cfg.network_infrastructure
    top = "config.network_infrastructure"
    by_name = {vnet.name: vnet for vnet in infra.vnets}

    # 1. VNet address spaces: parse + pairwise overlap detection.
    networks: dict[str, Any] = {}
    for vnet in infra.vnets:
        try:
            networks[vnet.name] = ipaddress.ip_network(vnet.address_space)
        except ValueError as exc:
            raise ConfigError(
                f"{top}.vnets[{vnet.name}].address_space: {vnet.address_space!r} "
                f"is not a valid CIDR: {exc}"
            ) from exc
    names = list(networks)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            a, b = networks[first], networks[second]
            if a.version == b.version and a.overlaps(b):
                raise ConfigError(
                    f"{top}.vnets: address spaces overlap: {first} ({a}) and {second} ({b}) "
                    "— all platform VNets must be non-overlapping (DR/failback requirement)"
                )

    # 2. Subnets: parse, containment in the VNet, no intra-VNet overlap.
    for vnet in infra.vnets:
        vnet_net = networks[vnet.name]
        parsed: list[tuple[str, Any]] = []
        for subnet in vnet.subnets:
            sub_path = f"{top}.vnets[{vnet.name}].subnets[{subnet.name}]"
            if any(name == subnet.name for name, _ in parsed):
                raise ConfigError(f"{sub_path}: duplicate subnet name {subnet.name!r}")
            try:
                subnet_net = ipaddress.ip_network(subnet.prefix)
            except ValueError as exc:
                raise ConfigError(
                    f"{sub_path}.prefix: {subnet.prefix!r} is not a valid CIDR: {exc}"
                ) from exc
            if subnet_net.version != vnet_net.version or not subnet_net.subnet_of(vnet_net):
                raise ConfigError(
                    f"{sub_path}.prefix: {subnet.prefix} is outside the VNet address space "
                    f"{vnet.address_space}"
                )
            for other_name, other_net in parsed:
                if subnet_net.overlaps(other_net):
                    raise ConfigError(
                        f"{sub_path}.prefix: {subnet.prefix} overlaps subnet "
                        f"{other_name!r} ({other_net}) in VNet {vnet.name!r}"
                    )
            parsed.append((subnet.name, subnet_net))

    # 3. Peering rules per role (v4: peer_to is a tuple; utility <-> utility
    #    global peering and the cross-environment build VNet are allowed;
    #    v4.1: role 'external' entries resolve build peer_to names locally).
    for vnet in infra.vnets:
        path = f"{top}.vnets[{vnet.name}]"
        if vnet.role == "external":
            continue  # resolution-only: no peerings (enforced at parse time)
        if vnet.role == "spoke":
            if len(vnet.peer_to) != 1:
                raise ConfigError(
                    f"{path}.peer_to: a 'spoke' names exactly ONE utility VNet "
                    f"(got {list(vnet.peer_to)})"
                )
            peer = by_name.get(vnet.peer_to[0])
            if peer is None:
                raise ConfigError(
                    f"{path}.peer_to: {vnet.peer_to[0]!r} does not name a VNet in "
                    f"{top}.vnets (declared: {sorted(by_name)})"
                )
            if peer.role != "utility":
                raise ConfigError(
                    f"{path}.peer_to: {vnet.peer_to[0]!r} has role {peer.role!r}; "
                    "spokes must peer to a 'utility' VNet"
                )
        elif vnet.role == "utility":
            # utility <-> utility global peering (e.g. prod-utility <->
            # dr-utility for appgw -> DR LB and bastion -> DR reach).
            for target in vnet.peer_to:
                peer = by_name.get(target)
                if peer is None:
                    raise ConfigError(
                        f"{path}.peer_to: {target!r} does not name a VNet in "
                        f"{top}.vnets (declared: {sorted(by_name)})"
                    )
                if peer.role != "utility" or peer.name == vnet.name:
                    raise ConfigError(
                        f"{path}.peer_to: utility VNets may only peer to OTHER "
                        f"utility VNets, got {target!r} (role {peer.role!r})"
                    )
        else:  # build
            if not vnet.peer_to:
                raise ConfigError(
                    f"{path}.peer_to: required for role 'build' (the utility "
                    "VNets the build agents deploy through)"
                )
            for target in vnet.peer_to:
                peer = by_name.get(target)
                if peer is None:
                    # Cross-ENVIRONMENT peering by VNet name with NO local
                    # entry: tolerated — network/main.bicep skips names it
                    # cannot resolve (unresolvedPeerings output). Prefer a
                    # role-'external' entry so the pair IS emitted (the prod
                    # config declares vnet-dev-utility that way).
                    continue
                if peer.role not in ("utility", "external"):
                    raise ConfigError(
                        f"{path}.peer_to: {target!r} has role {peer.role!r}; "
                        "build VNets peer to 'utility' (or resolution-only "
                        "'external') VNets only"
                    )

    # 4. Access CIDR lists must parse.
    for key, cidrs in (
        ("user_access_cidrs", infra.user_access_cidrs),
        ("bastion_cidrs", infra.bastion_cidrs),
    ):
        for cidr in cidrs:
            try:
                ipaddress.ip_network(cidr)
            except ValueError as exc:
                raise ConfigError(f"{top}.{key}: {cidr!r} is not a valid CIDR: {exc}") from exc

    # 5. Placement cross-check: every referenced VNet/subnet must be created here.
    for vnet_name, rg, subnet_name, allowed_roles, ref_path in _placement_refs(cfg):
        vnet = by_name.get(vnet_name)
        if vnet is None:
            raise ConfigError(
                f"{ref_path}: references VNet {vnet_name!r}, which is missing from "
                f"{top}.vnets — when network_infrastructure.create is true every VNet "
                f"used by the placement blocks must be declared there "
                f"(declared: {sorted(by_name)})"
            )
        if vnet.resource_group_name != rg:
            raise ConfigError(
                f"{ref_path}: VNet {vnet_name!r} is declared in resource group "
                f"{vnet.resource_group_name!r} but the placement block references "
                f"resource group {rg!r} (they must match)"
            )
        subnet = vnet.subnet(subnet_name)
        if subnet is None:
            raise ConfigError(
                f"{ref_path}: subnet {subnet_name!r} is missing from VNet {vnet_name!r} in "
                f"{top}.vnets (declared subnets: {[s.name for s in vnet.subnets]})"
            )
        if subnet.role not in allowed_roles:
            raise ConfigError(
                f"{ref_path}: subnet {subnet_name!r} in VNet {vnet_name!r} has role "
                f"{subnet.role!r}; this placement requires role "
                f"{' or '.join(sorted(allowed_roles))}"
            )


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
        hint = ""
        if unknown_blobs & {"agent_installer", "agent_silent"}:
            hint = (
                " (v4.1: the Control-M/Agent is baked into the ctm-agent Packer "
                "image — agent media no longer lives in the installers container)"
            )
        raise ConfigError(f"{top}.installer_blobs: unknown keys {sorted(unknown_blobs)}{hint}")

    image = _parse_image(data.get("image"), f"{top}.image")

    cfg = EnvironmentConfig(
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
        dr=_parse_dr(data.get("dr"), f"{top}.dr", list(servers)),
        network_infrastructure=_parse_network_infrastructure(
            data.get("network_infrastructure"), f"{top}.network_infrastructure"
        ),
        media=_parse_media(data.get("media"), f"{top}.media"),
        access=_parse_access(data.get("access"), f"{top}.access"),
        kv_sync=_parse_kv_sync(data.get("kv_sync"), f"{top}.kv_sync"),
        build_agents=_parse_build_agents(data.get("build_agents"), f"{top}.build_agents"),
    )
    # Semantic validation is gated on create: with create false (bring-your-
    # own networking, the default) the vnets list is inert documentation.
    if cfg.network_infrastructure.create:
        _validate_network_infrastructure(cfg)
    return cfg


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
