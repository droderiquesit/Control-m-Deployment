# ---------------------------------------------------------------------------
# Phase engine: rg -> foundation(primary[,dr]) -> secrets -> compute -> dr ->
# replication, passing each deployment's outputs to the next. Node plans and
# agent pools are derived from config exactly like the old platform locals.
# Azure SDK imports are guarded inside the functions that need them.
# ---------------------------------------------------------------------------
"""Python-orchestrated Bicep deployments for one Control-M environment.

``python -m ctmdeploy deploy --env prod [--phase all|rg|foundation|secrets|
compute|dr|replication]`` runs the phases; ``whatif`` runs the same ARM
deployments in what-if mode (the pre-apply change preview).
Deployment names are ``ctm-<env>-<phase>[-dr]``; later phases re-read the
earlier phases' recorded outputs so each phase can run standalone.
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

PHASES = ("rg", "foundation", "secrets", "compute", "dr", "replication")

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
    tokens |= render.agent_tokens(
        agent_user=server.agents.os_user,
        em_api_endpoint=f"https://{cfg.web_alias}:{cfg.em.web_port}/automation-api",
        em_api_user=EM_API_USER,
        ctm_server_name=server.name,
        hostgroup=server.hostgroup,
        agent_port=server.agents.agent_port,
        provision_image=server.agents.provision_image,
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
        network = cfg.network if primary else cfg.dr.network
        if network is None:  # defensive: config validation enforces this already
            raise DeployError("dr.network is required for the DR foundation")
        secrets_map = self._resolve_secrets()
        return {
            "environment": cfg.environment,
            "role": role,
            "uniqueSuffix": cfg.unique_suffix,
            "network": network.as_param(),
            "privateDnsZoneName": cfg.private_dns_zone_name,
            "deployerIpCidrs": list(cfg.deployer_ip_cidrs),
            "keyVaultPublicAccess": cfg.key_vault_public_access,
            "deployerObjectId": self.deployer_object_id,
            "databaseCreate": primary,
            "databaseHostOverride": "" if primary else cfg.replica_fqdn,
            "databaseAdminPassword": secrets_map["db-admin-password"],
            "database": cfg.database.as_param(),
            "storage": cfg.storage.as_param(),
            "loadBalancer": cfg.load_balancer.as_param(cfg.em.web_port),
            "observability": cfg.observability.as_param(),
            "servers": {key: server.as_param() for key, server in cfg.servers.items()},
            "emDbName": cfg.em.db_name,
            "tags": cfg.platform_tags(role),
        }

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

    def _compute_params(self, role: str) -> dict[str, Any]:
        cfg = self.cfg
        primary = role == "primary"
        outputs = self._foundation_outputs(role)
        view = foundation_view(outputs)
        template_dir = self._template_dir()

        nodes = [
            item.to_bicep(node_custom_data(cfg, view, item, template_dir))
            for item in node_plan(cfg, role)
        ]
        pools = [
            pool.to_bicep(agent_custom_data(cfg, view, pool, template_dir))
            for pool in agent_pools(cfg, role)
        ]
        return {
            "name": cfg.name if primary else cfg.dr_name,
            "location": cfg.location if primary else cfg.dr.location,
            "appSubnetId": outputs["appSubnetId"],
            "imageId": cfg.image_id(),
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

    def phase_compute(self, what_if: bool = False) -> list[DeploymentResult]:
        """RG deployments of bicep/compute.bicep: full primary, warm-pool DR."""
        cfg = self.cfg
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

    def phase_dr(self, what_if: bool = False) -> list[DeploymentResult]:
        """Subscription deployment of bicep/dr.bicep (replica, ASR vault, ...)."""
        cfg = self.cfg
        if not cfg.dr.enabled:
            print("    dr: skipped (dr.enabled is false)")
            return []
        primary = self._foundation_outputs("primary")
        dr = self._foundation_outputs("dr")
        params = {
            "name": cfg.name,
            "primaryRg": cfg.resource_group_name,
            "drRg": cfg.dr_resource_group_name,
            "primaryLocation": cfg.location,
            "drLocation": cfg.dr.location,
            "replicaName": cfg.replica_name,
            "replicaSkuName": cfg.replica_sku_name,
            "primaryDatabaseServerId": primary["databaseServerId"],
            "drDataSubnetId": dr["dataSubnetId"],
            "drPostgresPrivateDnsZoneId": dr["postgresPrivateDnsZoneId"],
            "primaryBlobAccountName": primary["blobAccountName"],
            "drBlobAccountName": dr["blobAccountName"],
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

        primary = self._foundation_outputs("primary")
        dr = self._foundation_outputs("dr")
        compute = self._saved_outputs(
            ResourceGroupScope(cfg.resource_group_name), f"{cfg.name}-compute", "compute"
        )
        dr_deploy = self._saved_outputs(
            SubscriptionScope(cfg.location), f"{cfg.name}-dr", "dr"
        )
        replication.ensure(
            cfg,
            compute["nodes"],
            primary_vnet_id=primary["vnetId"],
            dr_vnet_id=dr["vnetId"],
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
            if current == "rg":
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
