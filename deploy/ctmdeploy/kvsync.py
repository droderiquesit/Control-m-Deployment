# ---------------------------------------------------------------------------
# Key Vault -> Control-M sync deployment: `ctmdeploy kvsync deploy` zips the
# functions/kvsync Durable Function source and pushes it to the Function
# App's Kudu zipdeploy endpoint. The Function App itself (EP1 plan, storage,
# system-assigned identity + Key Vault Secrets User, VNet integration into
# snet-ctm-func) is provisioned by the access phase (bicep/modules/
# function-kvsync.bicep, called from bicep/access.bicep when
# kv_sync.enabled). Pure stdlib HTTP (urllib); only the credential import is
# lazy so the module stays importable offline.
# ---------------------------------------------------------------------------
"""Zip-deploy the functions/kvsync source to the <name>-kvsync Function App.

``python -m ctmdeploy kvsync deploy --env prod`` builds a zip of
``functions/kvsync`` in memory and POSTs it to
``https://<app>.scm.azurewebsites.net/api/zipdeploy`` with an ARM-audience
bearer token from DefaultAzureCredential.

NOTE (first-run validation): the SCM site accepts Entra ID bearer tokens
when the Function App's basic-auth publishing is disabled and the caller
holds a role with ``Microsoft.Web/sites/publish/action`` (e.g. Website
Contributor / the platform deployer role). Validate once per tenant — some
older stacks still require publishing-profile credentials.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from ctmdeploy.config import EnvironmentConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

#: Repo-relative directory holding the Durable Function source.
FUNCTION_SOURCE_DIR = Path("functions") / "kvsync"

#: Files the zip MUST contain for a valid python v2 function app.
REQUIRED_FILES = ("host.json", "function_app.py", "requirements.txt")

#: ARM audience used for the SCM bearer token.
ARM_SCOPE = "https://management.azure.com/.default"

#: Directories never shipped to the Function App (mirrors .funcignore).
_EXCLUDED_DIRS = {"__pycache__", ".python_packages", ".venv", ".vscode", ".pytest_cache", "tests"}

#: Individual files never shipped (mirrors .funcignore). local.settings.json
#: can hold real connection strings/secrets from local runs - it must never
#: reach the Function App.
_EXCLUDED_FILES = {"local.settings.json"}


class KvSyncError(RuntimeError):
    """The kvsync deployment cannot run (missing source, HTTP failure, ...)."""


def zip_source(source_dir: str | Path) -> bytes:
    """Zip the function source (flat, relative paths) into memory.

    Raises when a required file is missing, so a broken checkout never
    produces a half-empty Function App.
    """
    source = Path(source_dir)
    if not source.is_dir():
        raise KvSyncError(f"function source not found: {source}")
    missing = [name for name in REQUIRED_FILES if not (source / name).is_file()]
    if missing:
        raise KvSyncError(f"{source} is missing required files: {missing}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(source)
            if any(
                part in _EXCLUDED_DIRS or part.startswith(".git") for part in relative.parts
            ):
                continue
            if relative.name in _EXCLUDED_FILES or relative.suffix == ".pyc":
                continue
            archive.write(file, relative.as_posix())
    return buffer.getvalue()


def zipdeploy_url(app_name: str) -> str:
    """Kudu zipdeploy endpoint (synchronous mode: the POST returns when the
    deployment finished, so the CLI exit code reflects the real outcome)."""
    return f"https://{app_name}.scm.azurewebsites.net/api/zipdeploy?isAsync=false"


def deploy(
    cfg: EnvironmentConfig,
    repo_root: str | Path,
    *,
    credential: "TokenCredential | None" = None,
    timeout: int = 900,
) -> int:
    """Zip functions/kvsync and push it to the <name>-kvsync Function App."""
    if not cfg.kv_sync.enabled:
        raise KvSyncError(
            f"kv_sync.enabled is false for environment {cfg.environment!r}; enable it in "
            "environments/<env>/config.yaml and run the access phase first"
        )
    payload = zip_source(Path(repo_root) / FUNCTION_SOURCE_DIR)
    app_name = cfg.kvsync_function_app_name

    if credential is None:
        from ctmdeploy.azure import get_credential

        credential = get_credential()
    token = credential.get_token(ARM_SCOPE).token

    url = zipdeploy_url(app_name)
    print(f"==> zipdeploy {len(payload)} bytes -> {url}")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/zip")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        if exc.code in (401, 403):
            raise KvSyncError(
                f"zipdeploy to {app_name} was rejected ({exc.code}). The deploying "
                "identity needs Microsoft.Web/sites/publish/action on the Function App "
                "(Website Contributor or the platform deployer role) and the app must "
                f"accept Entra ID auth on the SCM site. Response: {detail}"
            ) from exc
        raise KvSyncError(f"zipdeploy to {app_name} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise KvSyncError(
            f"could not reach {url}: {exc.reason}. Was the access phase deployed "
            "(the Function App is created by bicep/access.bicep when kv_sync.enabled)?"
        ) from exc
    elapsed = int(time.monotonic() - started)
    print(f"    kvsync deployed to {app_name} (HTTP {status}, {elapsed}s)")
    print(
        json.dumps(
            {
                "functionApp": app_name,
                "schedule": cfg.kv_sync.schedule,
                "ctmApiBase": cfg.ctm_api_base,
            },
            indent=2,
        )
    )
    return 0
