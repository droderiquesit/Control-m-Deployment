# ---------------------------------------------------------------------------
# Phase engine: network(optional) -> rg -> foundation(primary[,dr]) ->
# secrets -> compute -> access -> dr -> replication, passing each
# deployment's outputs to the next. v2: per-node/per-pool subnet placement
# (utility VNet + one VNet per server); v3/v4: optional full-network
# provisioning (network/main.bicep, only when network_infrastructure.create)
# and the access layer (Bastion, private Application Gateway over both
# regional LBs, kvsync Function, ADO build agents). The compute phase first
# uploads node-scripts/ctm_node_install.py to the installers container so
# nodes always run the repo's script version. Idempotency pre-reads
# (existing VMs/VMSS -> isNew, promoted replica, object-replication policy
# ids, live PG zones). Azure SDK imports are guarded inside the functions
# that need them.
# ---------------------------------------------------------------------------
"""Python-orchestrated Bicep deployments for one Control-M environment.

``python -m ctmdeploy deploy --env prod [--phase all|network|rg|foundation|
secrets|compute|access|dr|replication]`` runs the phases; ``whatif`` runs the
same ARM deployments in what-if mode (the pre-apply change preview). The
``network`` phase runs first and only when ``network_infrastructure.create``
is true (the default is bring-your-own networking, where it is skipped); it
deploys network/main.bicep, the standalone folder the cloud team can also
deploy with nothing but the az CLI. The ``access`` phase (after compute,
before dr) deploys bicep/access.bicep: Bastion (+ the kvsync Function when
kv_sync.enabled) everywhere it is enabled, and on prod additionally the
PRIVATE Application Gateway balancing the primary and DR internal load
balancers plus the Azure DevOps build-agent scale set.
Deployment names are ``ctm-<env>-<phase>[-dr]``; later phases re-read the
earlier phases' recorded outputs so each phase can run standalone.

Re-running ``deploy --phase all`` is safe: before each ARM deployment the
engine reads the live state it must not fight (existing VMs/VMSS for the
per-item ``isNew`` flags, a promoted DR replica, an existing blob
object-replication policy, the database's current availability zones) and
parameterizes the templates so they converge instead of churning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ctmdeploy import render
from ctmdeploy.azure import (
    AzureToolingError,
    Deployer,
    DeploymentResult,
    ResourceGroupScope,
    Scope,
    SubscriptionScope,
)
from ctmdeploy.config import (
    EM_API_USER,
    EM_PRIMARY_HOST,
    EM_SECONDARY_HOST,
    SHARE_NAME,
    AgentPoolPlanItem,
    EnvironmentConfig,
    NodePlanItem,
    agent_pools,
    node_plan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

PHASES = ("network", "rg", "foundation", "secrets", "compute", "access", "dr", "replication")

#: Used by `ctmdeploy render` when no live foundation outputs are available.
PLACEHOLDER_IDENTITY_CLIENT_ID = "00000000-0000-0000-0000-000000000000"


class DeployError(RuntimeError):
    """A phase could not run (bad phase, missing prerequisite outputs, ...)."""


# ---------------------------------------------------------------------------
# Foundation outputs -> cloud-init tokens (pure helpers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundationView:
    """The foundation outputs cloud-init rendering needs for one region."""

    key_vault_name: str
    identity_client_id: str
    blob_account: str
    nfs_server: str
    nfs_export: str
    dns_zone: str


def foundation_view(outputs: Mapping[str, Any]) -> FoundationView:
    """Build a FoundationView from a foundation deployment's outputs."""
    required = {
        "keyVaultName": "key_vault_name",
        "identityClientId": "identity_client_id",
        "blobAccountName": "blob_account",
        "nfsServer": "nfs_server",
        "nfsExportPath": "nfs_export",
        "dnsZoneName": "dns_zone",
    }
    missing = sorted(key for key in required if not outputs.get(key))
    if missing:
        raise DeployError(f"foundation outputs are missing {missing}; redeploy the foundation phase")
    return FoundationView(**{attr: str(outputs[key]) for key, attr in required.items()})


def placeholder_foundation_view(cfg: EnvironmentConfig, role: str = "primary") -> FoundationView:
    """Offline stand-in for foundation outputs (used by `ctmdeploy render`).

    Every value except the identity client id is derivable from config via
    the fixed naming rules; the client id gets an obvious placeholder.
    """
    primary = role == "primary"
    files = cfg.files_account_name if primary else cfg.dr_files_account_name
    return FoundationView(
        key_vault_name=cfg.key_vault_name if primary else cfg.dr_key_vault_name,
        identity_client_id=PLACEHOLDER_IDENTITY_CLIENT_ID,
        blob_account=cfg.blob_account_name if primary else cfg.dr_blob_account_name,
        nfs_server=f"{files}.file.core.windows.net",
        nfs_export=f"/{files}/{SHARE_NAME}",
        dns_zone=cfg.private_dns_zone_name,
    )


def node_custom_data(
    cfg: EnvironmentConfig,
    view: FoundationView,
    item: NodePlanItem,
    template_dir: str | Path | None = None,
) -> str:
    """Rendered cloud-init for one EM or Control-M/Server VM (no secrets)."""
    tokens = render.common_tokens(
        role=f"{item.component}-{item.role}",
        environment=cfg.environment,
        key_vault_name=view.key_vault_name,
        identity_client_id=view.identity_client_id,
        blob_account=view.blob_account,
        nfs_server=view.nfs_server,
        nfs_export=view.nfs_export,
        ctm_version=cfg.ctm_version,
        dns_zone=view.dns_zone,
        attach_data_disk=True,
    )
    blobs = cfg.installer_blobs
    if item.component == "em":
        tokens |= render.em_tokens(
            em_role=item.role,
            em_user=cfg.em.os_user,
            em_installer_blob=blobs.em_installer,
            em_silent_blob=(
                blobs.em_silent_primary if item.role == "primary" else blobs.em_silent_secondary
            ),
            db_host=cfg.db_alias,
            db_port=cfg.database.port,
            em_db_name=cfg.em.db_name,
            em_db_user=cfg.em.db_user,
            db_admin_user=cfg.database.admin_user,
            em_primary_host=EM_PRIMARY_HOST,
            em_secondary_host=EM_SECONDARY_HOST if cfg.em.enable_secondary else "",
            web_port=cfg.em.web_port,
        )
        return render.render("em", tokens, template_dir)

    server = cfg.servers[item.server_key]
    tokens |= render.server_tokens(
        server_role=item.role,
        ctm_user=server.os_user,
        server_installer_blob=blobs.server_installer,
        server_silent_blob=(
            blobs.server_silent_primary if item.role == "primary" else blobs.server_silent_secondary
        ),
        db_host=cfg.db_alias,
        db_port=cfg.database.port,
        ctm_db_name=server.db_name,
        ctm_db_user=server.db_user,
        ctm_db_secret_name=server.secret_name,
        db_admin_user=cfg.database.admin_user,
        ctm_server_name=server.name,
        server_primary_host=server.primary_host,
        server_secondary_host=server.secondary_host,
        em_host=EM_PRIMARY_HOST,
    )
    return render.render("server", tokens, template_dir)


def agent_custom_data(
    cfg: EnvironmentConfig,
    view: FoundationView,
    pool: AgentPoolPlanItem,
    template_dir: str | Path | None = None,
) -> str:
    """Rendered cloud-init for one agent VMSS (no secrets)."""
    server = cfg.servers[pool.stage]
    tokens = render.common_tokens(
        role=f"agent-{pool.stage}",
        environment=cfg.environment,
        key_vault_name=view.key_vault_name,
        identity_client_id=view.identity_client_id,
        blob_account=view.blob_account,
        nfs_server=view.nfs_server,
        nfs_export=view.nfs_export,
        ctm_version=cfg.ctm_version,
        dns_zone=view.dns_zone,
        attach_data_disk=False,
    )
    # v4.1: the agent software is BAKED into the ctm-agent image, so the
    # tokens carry configuration + registration inputs only (no media blobs).
    tokens |= render.agent_tokens(
        agent_user=server.agents.os_user,
        em_api_endpoint=f"https://{cfg.web_alias}:{cfg.em.web_port}/automation-api",
        em_api_user=EM_API_USER,
        ctm_server_name=server.name,
        hostgroup=server.hostgroup,
        agent_port=server.agents.agent_port,
        provision_image=server.agents.provision_image,
        server_primary_host=server.primary_host,
    )
    return render.render("agent", tokens, template_dir)


# ---------------------------------------------------------------------------
# Phase engine
# ---------------------------------------------------------------------------


class Engine:
    """Runs the deployment phases for one environment."""

    def __init__(
        self,
        cfg: EnvironmentConfig,
        repo_root: str | Path,
        *,
        deployer: Deployer | None = None,
        credential: "TokenCredential | None" = None,
        deployer_object_id: str = "",
        bicep_bin: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.repo_root = Path(repo_root)
        self.deployer = deployer or Deployer(
            cfg.subscription_id, credential=credential, bicep_bin=bicep_bin
        )
        self.deployer_object_id = deployer_object_id
        self._outputs: dict[str, dict[str, Any]] = {}
        self._secrets: dict[str, str] | None = None
        self._overridden: set[str] = set()

    # -- shared helpers ------------------------------------------------------

    def _bicep(self, name: str) -> Path:
        """Template path: bare names live in bicep/; a name carrying a '/'
        (e.g. network/main.bicep) is resolved from the repository root."""
        if "/" in name:
            return self.repo_root / name
        return self.repo_root / "bicep" / name

    def _template_dir(self) -> Path:
        return self.repo_root / "cloud-init"

    def _deploy(
        self,
        scope: Scope,
        name: str,
        template: str,
        params: Mapping[str, Any],
        what_if: bool,
    ) -> DeploymentResult:
        action = "what-if" if what_if else "deploy"
        print(f"==> {action} {name} ({template})")
        result = self.deployer.deploy(scope, name, self._bicep(template), params, what_if=what_if)
        if result.what_if is not None:
            print(result.what_if.format())
        else:
            self._outputs[name] = result.outputs
            print(f"    {name}: succeeded")
        return result

    def _saved_outputs(self, scope: Scope, name: str, hint: str) -> dict[str, Any]:
        """Outputs of a phase run earlier (this process, or a past deployment)."""
        if name in self._outputs:
            return self._outputs[name]
        fetched = self.deployer.get_outputs(scope, name)
        if fetched is None:
            raise DeployError(
                f"deployment {name!r} has no recorded outputs; run `ctmdeploy deploy "
                f"--env {self.cfg.environment} --phase {hint}` first"
            )
        self._outputs[name] = fetched
        return fetched

    def _foundation_outputs(self, role: str) -> dict[str, Any]:
        cfg = self.cfg
        if role == "primary":
            return self._saved_outputs(
                ResourceGroupScope(cfg.resource_group_name), f"{cfg.name}-foundation", "foundation"
            )
        return self._saved_outputs(
            ResourceGroupScope(cfg.dr_resource_group_name),
            f"{cfg.name}-foundation-dr",
            "foundation",
        )

    def _resolve_secrets(self) -> dict[str, str]:
        """Resolve the platform secret values once per run (never logged)."""
        if self._secrets is None:
            from ctmdeploy import secrets as secrets_mod

            values, overridden = secrets_mod.resolve(self.cfg, self.deployer.credential)
            self._secrets = values
            self._overridden = overridden
        return self._secrets

    def _ssh_public_key(self) -> str:
        from ctmdeploy import secrets as secrets_mod

        return secrets_mod.ssh_public_key_from_pem(self._resolve_secrets()["ssh-private-key"])

    # -- phases --------------------------------------------------------------

    def phase_network(self, what_if: bool = False) -> list[DeploymentResult]:
        """Subscription deployment of network/main.bicep (optional full network).

        Runs only when ``network_infrastructure.create`` is true; the default
        (false) is bring-your-own networking, where the placement blocks
        reference pre-existing VNets and this phase is skipped. The single
        subscription-scope deployment covers BOTH regions (each vnets entry
        carries its own resource group + location, DR VNets included). The
        template lives in the standalone network/ folder so the cloud team
        can deploy the SAME plan with just the az CLI — the vnets param is
        the raw config shape (peer_to normalized to a list) and main.bicep
        resolves all peer address spaces itself.
        """
        cfg = self.cfg
        infra = cfg.network_infrastructure
        if not infra.create:
            print("    network: skipped (network_infrastructure.create is false; bring-your-own)")
            return []
        params = {
            "vnets": infra.vnets_param(),
            "userAccessCidrs": list(infra.user_access_cidrs),
            "bastionCidrs": list(infra.bastion_cidrs),
            "tags": cfg.platform_tags("network"),
        }
        scope = SubscriptionScope(cfg.location)
        return [
            self._deploy(scope, f"{cfg.name}-network", "network/main.bicep", params, what_if)
        ]

    def phase_rg(self, what_if: bool = False) -> list[DeploymentResult]:
        """Subscription deployment of bicep/rg.bicep (primary RG + DR RG)."""
        cfg = self.cfg
        params = {
            "name": cfg.name,
            "location": cfg.location,
            "drLocation": cfg.dr.location if cfg.dr.enabled else "",
            "resourceLock": cfg.resource_lock,
            "tags": cfg.platform_tags("primary"),
        }
        scope = SubscriptionScope(cfg.location)
        return [self._deploy(scope, f"{cfg.name}-rg", "rg.bicep", params, what_if)]

    def _foundation_params(self, role: str) -> dict[str, Any]:
        cfg = self.cfg
        primary = role == "primary"
        try:
            network = cfg.region_network(role)
        except Exception as exc:  # defensive: config validation enforces this already
            raise DeployError(str(exc)) from exc
        secrets_map = self._resolve_secrets()
        # Idempotency (5.5): never fight an HA zone swap — read the live
        # primary server's zones and re-assert those instead of the defaults.
        database = dict(cfg.database.as_param())
        zone, standby_zone = self._database_zones() if primary else ("1", "2")
        database["availability_zone"] = zone
        database["standby_zone"] = standby_zone
        return {
            "environment": cfg.environment,
            "role": role,
            "uniqueSuffix": cfg.unique_suffix,
            "network": network.as_param(),
            # Every VNet in the region: privatelink zones + the platform DNS
            # zone get one link per entry (utility + server VNets + extras).
            "allVnets": cfg.all_vnets(role),
            "dnsVnetLinks": cfg.dns_vnet_links(role),
            "privateDnsZoneName": cfg.private_dns_zone_name,
            "deployerIpCidrs": list(cfg.deployer_ip_cidrs),
            "keyVaultPublicAccess": cfg.key_vault_public_access,
            "deployerObjectId": self.deployer_object_id,
            "databaseCreate": primary,
            "databaseHostOverride": "" if primary else cfg.replica_fqdn,
            "databaseAdminPassword": secrets_map["db-admin-password"],
            "database": database,
            "storage": cfg.storage.as_param(),
            "loadBalancer": cfg.load_balancer.as_param(cfg.em.web_port),
            "observability": cfg.observability.as_param(),
            "servers": {key: server.as_param() for key, server in cfg.servers.items()},
            "emDbName": cfg.em.db_name,
            "tags": cfg.platform_tags(role),
        }

    def _database_zones(self) -> tuple[str, str]:
        """Live (availabilityZone, standbyAvailabilityZone) of the primary PG
        server when it exists; the create-time defaults ('1', '2') otherwise.

        Azure may swap the zones after an HA failover; re-deploys must assert
        the live values, not the originals. Lazy/guarded: offline work never
        reaches this (it is only called on a real foundation deployment).
        """
        default = ("1", "2")
        try:
            from azure.core.exceptions import ResourceNotFoundError
            from azure.mgmt.postgresqlflexibleservers import PostgreSQLManagementClient
        except ImportError:
            return default
        client = PostgreSQLManagementClient(self.deployer.credential, self.cfg.subscription_id)
        try:
            server = client.servers.get(self.cfg.resource_group_name, self.cfg.postgres_server_name)
        except ResourceNotFoundError:
            return default
        except Exception as exc:  # noqa: BLE001 - pre-read only; deploy surfaces real errors
            print(f"    note: could not read live database zones ({exc}); using defaults")
            return default
        zone = str(getattr(server, "availability_zone", "") or "") or default[0]
        ha = getattr(server, "high_availability", None)
        standby = str(getattr(ha, "standby_availability_zone", "") or "") if ha else ""
        print(f"    database {self.cfg.postgres_server_name}: live zones {zone}/{standby or '-'}")
        return (zone, standby or default[1])

    def phase_foundation(self, what_if: bool = False) -> list[DeploymentResult]:
        """RG deployments of bicep/foundation.bicep: primary, then DR."""
        cfg = self.cfg
        results = [
            self._deploy(
                ResourceGroupScope(cfg.resource_group_name),
                f"{cfg.name}-foundation",
                "foundation.bicep",
                self._foundation_params("primary"),
                what_if,
            )
        ]
        if cfg.dr.enabled:
            results.append(
                self._deploy(
                    ResourceGroupScope(cfg.dr_resource_group_name),
                    f"{cfg.name}-foundation-dr",
                    "foundation.bicep",
                    self._foundation_params("dr"),
                    what_if,
                )
            )
        return results

    def phase_secrets(self) -> None:
        """Seed the Key Vault of every region (idempotent, never rotates)."""
        from ctmdeploy import secrets as secrets_mod

        values = self._resolve_secrets()
        actions = secrets_mod.seed(
            self.cfg, values, self._overridden, credential=self.deployer.credential
        )
        for vault, name, action in actions:
            print(f"    secret {name} @ {vault}: {action}")

    def _existing_compute(self, resource_group: str) -> tuple[set[str], set[str]]:
        """Names of the VMs and VMSS already in a resource group.

        Drives the per-item ``isNew`` flags (idempotency 5.2): customData is
        immutable after VM create, and VMSS capacity belongs to autoscale once
        the scale set exists. A missing resource group means everything is new.
        """
        from azure.core.exceptions import ResourceNotFoundError
        from azure.mgmt.compute import ComputeManagementClient

        client = ComputeManagementClient(self.deployer.credential, self.cfg.subscription_id)
        try:
            vms = {vm.name for vm in client.virtual_machines.list(resource_group)}
            vmss = {s.name for s in client.virtual_machine_scale_sets.list(resource_group)}
        except ResourceNotFoundError:
            return set(), set()
        return vms, vmss

    def _compute_params(self, role: str) -> dict[str, Any]:
        cfg = self.cfg
        primary = role == "primary"
        outputs = self._foundation_outputs(role)
        view = foundation_view(outputs)
        template_dir = self._template_dir()
        resource_group = cfg.resource_group_name if primary else cfg.dr_resource_group_name
        existing_vms, existing_vmss = self._existing_compute(resource_group)

        # v2: every nodePlan/agentPools item carries its own subnetId (set by
        # the plan derivation from config) and an isNew flag read from Azure.
        nodes = [
            item.to_bicep(
                node_custom_data(cfg, view, item, template_dir),
                is_new=item.name not in existing_vms,
            )
            for item in node_plan(cfg, role)
        ]
        pools = [
            pool.to_bicep(
                agent_custom_data(cfg, view, pool, template_dir),
                is_new=pool.name not in existing_vmss,
            )
            for pool in agent_pools(cfg, role)
        ]
        return {
            "name": cfg.name if primary else cfg.dr_name,
            "location": cfg.location if primary else cfg.dr.location,
            # v4.1 two-image model: EM/Server VMs boot the base (OS-only)
            # image, the agent scale sets boot the ctm-agent image with the
            # Control-M/Agent baked in (configured + registered at boot).
            "imageId": cfg.image_id("base"),
            "agentImageId": cfg.image_id("agent"),
            "sshPublicKey": self._ssh_public_key(),
            "identityId": outputs["identityId"],
            "dataCollectionRuleId": outputs["dataCollectionRuleId"],
            "lbBackendPoolId": outputs["lbBackendPoolId"],
            "actionGroupId": outputs["actionGroupId"],
            "nodePlan": nodes,
            "agentPools": pools,
            # DR runs no VM backup (TF: platform_dr backup = { enabled = false }).
            "backup": cfg.backup.as_param(enabled=cfg.backup.enabled if primary else False),
            "filesAccountId": outputs["filesAccountId"],
            "filesShareName": outputs["shareName"],
            "vmAvailabilityAlerts": True,
            "tags": cfg.platform_tags(role),
        }

    def _upload_node_script(self) -> None:
        """Push node-scripts/ctm_node_install.py to the installers container
        of every region (scripts/ctm_node_install.py, overwrite) so the
        script version the nodes fetch at boot == the repo version being
        deployed. `ctmdeploy media upload` uploads it too.
        """
        script = self.repo_root / "node-scripts" / "ctm_node_install.py"
        if not script.is_file():
            raise DeployError(f"node installer not found: {script}")
        from azure.storage.blob import BlobServiceClient

        from ctmdeploy.config import INSTALLERS_CONTAINER

        cfg = self.cfg
        accounts = [cfg.blob_account_name]
        if cfg.dr.enabled:
            accounts.append(cfg.dr_blob_account_name)
        data = script.read_bytes()
        for account in accounts:
            client = BlobServiceClient(
                account_url=f"https://{account}.blob.core.windows.net",
                credential=self.deployer.credential,
            ).get_blob_client(INSTALLERS_CONTAINER, render.NODE_SCRIPT_BLOB)
            client.upload_blob(data, overwrite=True)
            print(f"    uploaded {render.NODE_SCRIPT_BLOB} -> {account}/{INSTALLERS_CONTAINER}")

    def phase_compute(self, what_if: bool = False) -> list[DeploymentResult]:
        """RG deployments of bicep/compute.bicep: full primary, warm-pool DR.

        The node installer script is uploaded FIRST (both regions), because
        the cloud-init shim baked into customData fetches it at boot.
        """
        cfg = self.cfg
        if not what_if:
            self._upload_node_script()
        results = [
            self._deploy(
                ResourceGroupScope(cfg.resource_group_name),
                f"{cfg.name}-compute",
                "compute.bicep",
                self._compute_params("primary"),
                what_if,
            )
        ]
        if cfg.dr.enabled:
            results.append(
                self._deploy(
                    ResourceGroupScope(cfg.dr_resource_group_name),
                    f"{cfg.name}-compute-dr",
                    "compute.bicep",
                    self._compute_params("dr"),
                    what_if,
                )
            )
        return results

    # -- access layer (v4) -----------------------------------------------------

    def _access_params(self) -> dict[str, Any] | None:
        """Parameters of bicep/access.bicep, or None when nothing is enabled.

        nonprod: Bastion (+ the kvsync Function when kv_sync.enabled).
        prod: Bastion + the PRIVATE Application Gateway whose single backend
        pool holds BOTH internal LB frontend IPs (primary + DR — healthy-only
        routing gives automatic primary/DR behavior) + kvsync + the Azure
        DevOps build-agent scale set. Subnet ids are built from config and
        the network plan; backend IPs come from the two foundation outputs.
        """
        cfg = self.cfg
        bastion_enabled = cfg.access.bastion_enabled
        # The gateway balances the primary and DR LBs: honored on prod only.
        appgw_enabled = cfg.access.appgw_enabled and cfg.environment == "prod"
        kvsync_enabled = cfg.kv_sync.enabled
        build_enabled = cfg.build_agents.enabled
        if not (bastion_enabled or appgw_enabled or kvsync_enabled or build_enabled):
            return None

        appgw_backend_ips: list[str] = []
        if appgw_enabled:
            primary = self._foundation_outputs("primary")
            appgw_backend_ips.append(str(primary["lbFrontendIp"]))
            if cfg.dr.enabled:
                dr = self._foundation_outputs("dr")
                appgw_backend_ips.append(str(dr["lbFrontendIp"]))
        build_agents = {
            "enabled": build_enabled,
            "resourceGroupName": cfg.build_agents.resource_group,
            "vmSize": cfg.build_agents.vm_size,
            "instances": cfg.build_agents.instances,
            "subnetId": cfg.build_agents_subnet_id() if build_enabled else "",
            "sshPublicKey": self._ssh_public_key() if build_enabled else "",
        }
        return {
            "name": cfg.name,
            "location": cfg.location,
            "platformRgName": cfg.resource_group_name,
            "bastionEnabled": bastion_enabled,
            "bastionSubnetId": cfg.bastion_subnet_id() if bastion_enabled else "",
            "appgwEnabled": appgw_enabled,
            "appgwSubnetId": cfg.appgw_subnet_id() if appgw_enabled else "",
            "appgwBackendIps": appgw_backend_ips,
            "dnsZoneName": cfg.private_dns_zone_name,
            "kvSyncEnabled": kvsync_enabled,
            "funcSubnetId": cfg.func_subnet_id() if kvsync_enabled else "",
            "keyVaultId": cfg.key_vault_id,
            "keyVaultUri": cfg.key_vault_uri,
            "ctmApiBase": cfg.ctm_api_base,
            "syncSchedule": cfg.kv_sync.schedule,
            "buildAgents": build_agents,
            "tags": cfg.platform_tags("access"),
        }

    def phase_access(self, what_if: bool = False) -> list[DeploymentResult]:
        """Subscription deployment of bicep/access.bicep (Bastion, private
        Application Gateway over both regional LBs, kvsync Function, build
        agents). Runs after compute (it needs the foundation LB IPs) and
        before dr."""
        cfg = self.cfg
        params = self._access_params()
        if params is None:
            print(
                "    access: skipped (access.bastion/appgw, kv_sync and "
                "build_agents are all disabled)"
            )
            return []
        scope = SubscriptionScope(cfg.location)
        return [self._deploy(scope, f"{cfg.name}-access", "access.bicep", params, what_if)]

    #: PG replication roles that mean 'still a replica' (safe to re-assert).
    _REPLICA_ROLES = frozenset({"asyncreplica", "geoasyncreplica"})

    def _should_deploy_replica(self) -> bool:
        """False when the DR replica exists but is no longer a replica.

        Idempotency (5.3): after a failover promotes the replica to a
        standalone/primary server, re-PUTting createMode=Replica would fail
        (or worse). GET it first and gate the replica module instead.
        """
        cfg = self.cfg
        try:
            from azure.core.exceptions import ResourceNotFoundError
            from azure.mgmt.postgresqlflexibleservers import PostgreSQLManagementClient
        except ImportError:
            return True
        client = PostgreSQLManagementClient(self.deployer.credential, cfg.subscription_id)
        try:
            server = client.servers.get(cfg.dr_resource_group_name, cfg.replica_name)
        except ResourceNotFoundError:
            return True  # first run: create the replica
        role = getattr(getattr(server, "replica", None), "role", None)
        if role is None:
            role = getattr(server, "replication_role", None)
        role_value = str(getattr(role, "value", role) or "").lower()
        if role_value in self._REPLICA_ROLES:
            return True  # still a replica; the PUT converges
        print(
            f"    replica {cfg.replica_name}: exists and is "
            f"{role_value or 'standalone'} (promoted); deployReplica=false"
        )
        return False

    def _object_replication_state(
        self, primary_account: str, dr_account: str
    ) -> tuple[str, list[str]]:
        """(policyId, [installers ruleId, backups ruleId]) of the existing
        object-replication policy on the DESTINATION (DR) account.

        Idempotency (5.4): the service mints policy/rule ids on first create;
        re-deploys must echo them or ARM creates duplicate policies. First
        run (or SDK unavailable): ('default', []) lets the service mint them.
        The ruleIds order matches dr.bicep's rules array: installers, backups.
        """
        cfg = self.cfg
        try:
            from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
            from azure.mgmt.storage import StorageManagementClient
        except ImportError:
            return ("default", [])
        client = StorageManagementClient(self.deployer.credential, cfg.subscription_id)
        try:
            policies = list(
                client.object_replication_policies.list(cfg.dr_resource_group_name, dr_account)
            )
        except (ResourceNotFoundError, HttpResponseError):
            return ("default", [])
        primary_lower = primary_account.lower()
        for policy in policies:
            # source_account is the account name or its full resource id,
            # depending on the allowCrossTenantReplication setting.
            source = str(getattr(policy, "source_account", "") or "").lower()
            if source != primary_lower and not source.endswith(f"/{primary_lower}"):
                continue
            rules = {
                str(rule.destination_container): str(rule.rule_id)
                for rule in (policy.rules or [])
                if getattr(rule, "rule_id", None)
            }
            rule_ids = [rules.get("installers", ""), rules.get("backups", "")]
            policy_id = str(getattr(policy, "policy_id", "") or policy.name or "")
            if policy_id and all(rule_ids):
                print(f"    object replication: reusing existing policy {policy_id}")
                return (policy_id, rule_ids)
        return ("default", [])

    def phase_dr(self, what_if: bool = False) -> list[DeploymentResult]:
        """Subscription deployment of bicep/dr.bicep (replica, ASR vault, ...)."""
        cfg = self.cfg
        if not cfg.dr.enabled:
            print("    dr: skipped (dr.enabled is false)")
            return []
        primary = self._foundation_outputs("primary")
        dr = self._foundation_outputs("dr")
        or_policy_id, or_rule_ids = self._object_replication_state(
            primary["blobAccountName"], dr["blobAccountName"]
        )
        params = {
            "name": cfg.name,
            "primaryRg": cfg.resource_group_name,
            "drRg": cfg.dr_resource_group_name,
            "primaryLocation": cfg.location,
            "drLocation": cfg.dr.location,
            "replicaName": cfg.replica_name,
            "replicaSkuName": cfg.replica_sku_name,
            # Idempotency (5.3): never re-PUT createMode=Replica over a
            # promoted (standalone) server.
            "deployReplica": self._should_deploy_replica(),
            "primaryDatabaseServerId": primary["databaseServerId"],
            "drDataSubnetId": dr["dataSubnetId"],
            "drPostgresPrivateDnsZoneId": dr["postgresPrivateDnsZoneId"],
            "primaryBlobAccountName": primary["blobAccountName"],
            "drBlobAccountName": dr["blobAccountName"],
            # Idempotency (5.4): echo the service-minted policy/rule ids.
            "existingPolicyId": or_policy_id,
            "existingRuleIds": or_rule_ids,
            "enableSiteRecovery": cfg.dr.enable_site_recovery,
            "trafficManager": {
                "enabled": cfg.dr.traffic_manager,
                "relativeName": f"{cfg.name}-web",
            },
            "primaryLbPublicIpId": primary.get("lbPublicIpId", "") or "",
            "drLbPublicIpId": dr.get("lbPublicIpId", "") or "",
            "tags": dict(cfg.tags),
        }
        scope = SubscriptionScope(cfg.location)
        return [self._deploy(scope, f"{cfg.name}-dr", "dr.bicep", params, what_if)]

    def phase_replication(self, wait: bool = False) -> None:
        """SDK-driven ASR enablement (fabrics, containers, mappings, items)."""
        cfg = self.cfg
        if not (cfg.dr.enabled and cfg.dr.enable_site_recovery):
            print("    replication: skipped (site recovery not enabled)")
            return
        from ctmdeploy import replication

        dr = self._foundation_outputs("dr")
        compute = self._saved_outputs(
            ResourceGroupScope(cfg.resource_group_name), f"{cfg.name}-compute", "compute"
        )
        dr_deploy = self._saved_outputs(
            SubscriptionScope(cfg.location), f"{cfg.name}-dr", "dr"
        )
        # v2: the VNet pairs (utility -> DR utility, each server VNet -> its
        # DR VNet) and per-component target subnets come straight from config.
        replication.ensure(
            cfg,
            compute["nodes"],
            cache_storage_account_id=dr_deploy["cacheStorageAccountId"],
            dr_lb_backend_pool_id=dr.get("lbBackendPoolId", "") or "",
            credential=self.deployer.credential,
            wait=wait,
        )

    # -- orchestration ---------------------------------------------------------

    def run(
        self, phase: str = "all", *, what_if: bool = False, wait: bool = False
    ) -> tuple[list[DeploymentResult], bool]:
        """Run one phase or all of them; returns (results, has_changes).

        In what-if mode the python-only phases (secrets, replication) are
        skipped and compute/dr fall back to the outputs of the previously
        recorded deployments; on a first-ever what-if those phases are
        reported as not analyzable instead of failing the run.
        """
        if phase != "all" and phase not in PHASES:
            raise DeployError(f"unknown phase {phase!r}; expected all or one of {PHASES}")
        phases = list(PHASES) if phase == "all" else [phase]
        results: list[DeploymentResult] = []
        for current in phases:
            if current == "network":
                results += self.phase_network(what_if)
            elif current == "rg":
                results += self.phase_rg(what_if)
            elif current == "foundation":
                results += self.phase_foundation(what_if)
            elif current == "secrets":
                if what_if:
                    print("    secrets: skipped in what-if mode (python-only phase)")
                else:
                    self.phase_secrets()
            elif current == "compute":
                results += self._maybe_skip_whatif(self.phase_compute, what_if, "compute")
            elif current == "access":
                results += self._maybe_skip_whatif(self.phase_access, what_if, "access")
            elif current == "dr":
                results += self._maybe_skip_whatif(self.phase_dr, what_if, "dr")
            elif current == "replication":
                if what_if:
                    print("    replication: skipped in what-if mode (python-only phase)")
                else:
                    self.phase_replication(wait)
        has_changes = any(r.has_changes for r in results if r.what_if is not None)
        return results, has_changes

    def _maybe_skip_whatif(self, fn, what_if: bool, label: str) -> list[DeploymentResult]:
        if not what_if:
            return fn(False)
        try:
            return fn(True)
        except (DeployError, AzureToolingError) as exc:
            print(f"    {label}: what-if not analyzable yet ({exc})")
            return []
