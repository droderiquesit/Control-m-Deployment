# ---------------------------------------------------------------------------
# Azure plumbing: DefaultAzureCredential, bicep -> ARM JSON compilation via
# the bicep CLI (PATH or CTM_BICEP_BIN; az is NOT required), and deployments
# at subscription or resource-group scope with what-if support.
# Azure SDK imports are guarded inside the functions that need them.
# ---------------------------------------------------------------------------
"""ARM deployment helpers for the Control-M toolchain.

Bicep deployments are stateless: there is no state backend. ``whatif``
(`ctmdeploy whatif`) is the pre-apply change preview — it runs the same
deployments in what-if mode and summarizes the changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

#: Environment variable overriding the bicep binary location.
BICEP_BIN_ENV_VAR = "CTM_BICEP_BIN"

#: What-if change types that mean the deployment would alter resources.
_CHANGE_TYPES = frozenset({"Create", "Delete", "Modify"})

_CHANGE_SYMBOLS = {
    "Create": "+",
    "Delete": "-",
    "Modify": "~",
    "Deploy": "!",
    "NoChange": "=",
    "Ignore": "*",
    "Unsupported": "?",
}


class AzureToolingError(RuntimeError):
    """The bicep CLI or an ARM deployment failed."""


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionScope:
    """Deploy at subscription scope; `location` stores the deployment metadata."""

    location: str


@dataclass(frozen=True)
class ResourceGroupScope:
    """Deploy into one resource group."""

    resource_group: str


Scope = SubscriptionScope | ResourceGroupScope


# ---------------------------------------------------------------------------
# Bicep compilation
# ---------------------------------------------------------------------------


def resolve_bicep_bin(explicit: str | None = None) -> str:
    """Locate the bicep CLI: explicit arg, then CTM_BICEP_BIN, then PATH."""
    candidate = explicit or os.environ.get(BICEP_BIN_ENV_VAR) or shutil.which("bicep")
    if not candidate:
        raise AzureToolingError(
            "bicep CLI not found on PATH; install it (https://aka.ms/bicep) or set "
            f"{BICEP_BIN_ENV_VAR} to the binary (the az CLI is not required)"
        )
    if "/" in candidate or os.sep in candidate:
        path = Path(candidate)
        if not path.is_file():
            raise AzureToolingError(f"bicep binary not found at {candidate}")
        return str(path)
    resolved = shutil.which(candidate)
    if resolved is None:
        raise AzureToolingError(f"bicep binary {candidate!r} not found on PATH")
    return resolved


def compile_bicep(template_path: str | Path, bicep_bin: str | None = None) -> dict[str, Any]:
    """Compile a .bicep file to an ARM template dict via `bicep build --stdout`."""
    template = Path(template_path)
    if not template.is_file():
        raise AzureToolingError(f"bicep template not found: {template}")
    binary = resolve_bicep_bin(bicep_bin)
    completed = subprocess.run(
        [binary, "build", str(template), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AzureToolingError(
            f"bicep build failed for {template} (exit {completed.returncode}):\n"
            f"{completed.stderr.strip()}"
        )
    if completed.stderr.strip():
        # Linter warnings: surface them without failing the deployment.
        print(completed.stderr.strip(), file=sys.stderr)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AzureToolingError(f"bicep build for {template} produced invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhatIfChange:
    change_type: str
    resource_id: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class WhatIfSummary:
    """Readable summary of one deployment's what-if result."""

    deployment: str
    changes: tuple[WhatIfChange, ...]

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.change_type] = counts.get(change.change_type, 0) + 1
        return counts

    @property
    def has_changes(self) -> bool:
        return any(change.change_type in _CHANGE_TYPES for change in self.changes)

    def format(self) -> str:
        lines = [f"What-if: {self.deployment}"]
        for change in sorted(self.changes, key=lambda c: (c.change_type, c.resource_id)):
            symbol = _CHANGE_SYMBOLS.get(change.change_type, "?")
            lines.append(f"  {symbol} [{change.change_type}] {change.resource_id}")
            for detail in change.details:
                lines.append(f"      {detail}")
        summary = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())) or "no resources"
        lines.append(f"  => {summary} (has_changes={str(self.has_changes).lower()})")
        return "\n".join(lines)


@dataclass(frozen=True)
class DeploymentResult:
    """Outcome of deploy(): real outputs, or a what-if summary."""

    name: str
    scope: Scope
    outputs: dict[str, Any] = field(default_factory=dict)
    what_if: WhatIfSummary | None = None

    @property
    def has_changes(self) -> bool:
        return self.what_if.has_changes if self.what_if is not None else True


def _normalize_change_type(change_type: Any) -> str:
    value = getattr(change_type, "value", change_type)
    return str(value) if value is not None else "Unsupported"


def _outputs_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    outputs: dict[str, Any] = {}
    for key, value in dict(raw).items():
        outputs[key] = value.get("value") if isinstance(value, Mapping) else getattr(value, "value", value)
    return outputs


# ---------------------------------------------------------------------------
# Credential + deployer
# ---------------------------------------------------------------------------


def get_credential() -> "TokenCredential":
    """DefaultAzureCredential: works locally (az login), in ADO (workload
    identity federation via AzurePipelinesCredential env vars) and in GitHub
    Actions (azure/login OIDC)."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


class Deployer:
    """Submits compiled Bicep templates as ARM deployments."""

    def __init__(
        self,
        subscription_id: str,
        credential: "TokenCredential | None" = None,
        bicep_bin: str | None = None,
    ) -> None:
        self.subscription_id = subscription_id
        self._credential = credential
        self._bicep_bin = bicep_bin
        self._client: Any = None

    @property
    def credential(self) -> "TokenCredential":
        if self._credential is None:
            self._credential = get_credential()
        return self._credential

    def _resource_client(self) -> Any:
        if self._client is None:
            from azure.mgmt.resource import ResourceManagementClient

            self._client = ResourceManagementClient(self.credential, self.subscription_id)
        return self._client

    # -- deployments -------------------------------------------------------

    def deploy(
        self,
        scope: Scope,
        name: str,
        template_path: str | Path,
        params: Mapping[str, Any] | None = None,
        mode: str = "Incremental",
        what_if: bool = False,
    ) -> DeploymentResult:
        """Compile `template_path` and run (or what-if) the ARM deployment.

        Blocks until the deployment (or what-if analysis) completes and
        returns the outputs dict / change summary.
        """
        template = compile_bicep(template_path, self._bicep_bin)
        parameters = {key: {"value": value} for key, value in (params or {}).items()}

        from azure.mgmt.resource.resources.models import (
            Deployment,
            DeploymentProperties,
            DeploymentWhatIf,
            DeploymentWhatIfProperties,
        )

        client = self._resource_client()

        if what_if:
            properties = DeploymentWhatIfProperties(
                mode=mode, template=template, parameters=parameters
            )
            if isinstance(scope, SubscriptionScope):
                poller = client.deployments.begin_what_if_at_subscription_scope(
                    name, DeploymentWhatIf(location=scope.location, properties=properties)
                )
            else:
                poller = client.deployments.begin_what_if(
                    scope.resource_group, name, DeploymentWhatIf(properties=properties)
                )
            result = poller.result()
            changes: list[WhatIfChange] = []
            for change in result.changes or []:
                details: list[str] = []
                for prop in getattr(change, "delta", None) or []:
                    prop_type = _normalize_change_type(getattr(prop, "property_change_type", ""))
                    details.append(f"{prop_type}: {getattr(prop, 'path', '?')}")
                changes.append(
                    WhatIfChange(
                        change_type=_normalize_change_type(change.change_type),
                        resource_id=change.resource_id or "?",
                        details=tuple(details[:8]),
                    )
                )
            return DeploymentResult(
                name=name, scope=scope, what_if=WhatIfSummary(deployment=name, changes=tuple(changes))
            )

        properties = DeploymentProperties(mode=mode, template=template, parameters=parameters)
        try:
            if isinstance(scope, SubscriptionScope):
                poller = client.deployments.begin_create_or_update_at_subscription_scope(
                    name, Deployment(location=scope.location, properties=properties)
                )
            else:
                poller = client.deployments.begin_create_or_update(
                    scope.resource_group, name, Deployment(properties=properties)
                )
            deployment = poller.result()
        except Exception as exc:  # azure-core HttpResponseError and friends
            raise AzureToolingError(f"deployment {name} failed: {exc}") from exc
        state = getattr(deployment.properties, "provisioning_state", None)
        if state is not None and str(state) != "Succeeded":
            raise AzureToolingError(f"deployment {name} finished in state {state}")
        return DeploymentResult(
            name=name, scope=scope, outputs=_outputs_dict(deployment.properties.outputs)
        )

    def get_outputs(self, scope: Scope, name: str) -> dict[str, Any] | None:
        """Outputs of an existing deployment, or None when it does not exist.

        Lets later phases (`--phase compute`, `--phase dr`, ...) run standalone
        by reading the outputs the earlier phases recorded in ARM.
        """
        from azure.core.exceptions import ResourceNotFoundError

        client = self._resource_client()
        try:
            if isinstance(scope, SubscriptionScope):
                deployment = client.deployments.get_at_subscription_scope(name)
            else:
                deployment = client.deployments.get(scope.resource_group, name)
        except ResourceNotFoundError:
            return None
        return _outputs_dict(deployment.properties.outputs)

    def ensure_resource_group(self, name: str, location: str, tags: Mapping[str, str]) -> None:
        """Create or update a plain resource group (used for ctm-images-rg)."""
        client = self._resource_client()
        client.resource_groups.create_or_update(
            name, {"location": location, "tags": dict(tags)}
        )
