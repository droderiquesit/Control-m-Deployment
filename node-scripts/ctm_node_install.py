#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Control-M node installer: ONE idempotent, stdlib-only python3 script that
# installs and wires up Control-M/EM and Control-M/Server nodes on RHEL 9,
# and CONFIGURES + REGISTERS Control-M/Agent scale-set instances (v4.1: the
# agent software is BAKED into the ctm-agent Packer image — the agent role
# downloads no media and runs no installer). It is fetched from the
# installers blob container by the cloud-init shim (cloud-init/*.sh.tmpl),
# which writes /etc/ctm/node.json (names/ids ONLY — never secrets) and runs:
#
#     python3 /usr/local/sbin/ctm_node_install.py --config /etc/ctm/node.json
#
# Re-running at any time is safe: an explicit stage machine records progress
# in /var/lib/ctm/install-state.json and every stage ALSO checks real-world
# done-ness (the .ctm_installed marker on the persistent /ctm disk survives
# VM rebuilds, the NFS media cache carries a .extracted marker, ...) before
# acting. A systemd oneshot (ctm-install.service, installed by the shim) is
# provided for manual re-runs. Logs go to /var/log/ctm-bootstrap.log.
#
# STDLIB ONLY by design (runs on the system python3 of the golden image, no
# pip on nodes): urllib for IMDS/Key Vault/Blob/EM REST, subprocess for the
# BMC setup.sh, fcntl.flock around the shared media extraction.
#
# Secrets are fetched from Key Vault via the managed identity at run time,
# held in memory only, substituted into the silent-install XML (0600) and
# the XML is deleted right after setup.sh finishes. Nothing secret is ever
# written to node.json, the state file, or any log line.
# ---------------------------------------------------------------------------
"""Idempotent Control-M node installer (EM / Server / Agent) for Azure VMs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

# ---------------------------------------------------------------------------
# Constants / contracts (kept in sync with deploy/ctmdeploy/render.py and the
# cloud-init shims; the deploy test-suite cross-checks them)
# ---------------------------------------------------------------------------

NODE_CONFIG_DEFAULT = "/etc/ctm/node.json"
STATE_FILE = Path("/var/lib/ctm/install-state.json")
LOG_FILE = Path("/var/log/ctm-bootstrap.log")
SHARED_MOUNT = Path("/mnt/ctm-shared")
DEREGISTERED_MARKER = Path("/var/lib/ctm/deregistered")
REGISTERED_MARKER = Path("/var/lib/ctm/agent-registered")
SCRIPT_INSTALL_PATH = "/usr/local/sbin/ctm_node_install.py"

IMDS_BASE = "http://169.254.169.254/metadata"
IMDS_TOKEN_API = "2018-02-01"
IMDS_INSTANCE_API = "2021-02-01"
IMDS_EVENTS_API = "2020-07-01"
KEY_VAULT_API = "7.4"
BLOB_API_VERSION = "2021-08-06"

#: Agent -> Server port (BMC default; not configurable in config.yaml).
AGENT_TO_SERVER_PORT = "7005"

#: Backups older than this many days are pruned from the shared volume.
BACKUP_RETENTION_DAYS = 14

#: Node kinds the role field of node.json may resolve to.
KINDS = ("em", "server", "agent")

#: node.json keys common to every role (the cloud-init common token set).
COMMON_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "role",
        "environment",
        "key_vault_name",
        "identity_client_id",
        "blob_account",
        "installers_container",
        "backups_container",
        "nfs_server",
        "nfs_export",
        "data_mount",
        "ctm_version",
        "dns_zone",
    }
)

#: Role-specific node.json keys (mirrors the cloud-init role token sets).
ROLE_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "em": frozenset(
        {
            "em_role",
            "em_user",
            "em_installer_blob",
            "em_silent_blob",
            "db_host",
            "db_port",
            "em_db_name",
            "em_db_user",
            "db_admin_user",
            "em_primary_host",
            "em_secondary_host",
            "web_port",
        }
    ),
    "server": frozenset(
        {
            "server_role",
            "ctm_user",
            "server_installer_blob",
            "server_silent_blob",
            "db_host",
            "db_port",
            "ctm_db_name",
            "ctm_db_user",
            "ctm_db_secret_name",
            "db_admin_user",
            "ctm_server_name",
            "server_primary_host",
            "server_secondary_host",
            "em_host",
        }
    ),
    # v4.1: the agent is baked into the ctm-agent image, so the agent
    # node.json carries configuration + registration inputs only — no media
    # blob names.
    "agent": frozenset(
        {
            "agent_user",
            "em_api_endpoint",
            "em_api_user",
            "ctm_server_name",
            "hostgroup",
            "agent_port",
            "provision_image",
            "server_primary_host",
        }
    ),
}

#: Substrings that may NEVER appear in a node.json key (secrets stay in KV).
FORBIDDEN_CONFIG_KEY_PARTS = ("password", "secret_value", "token", "key_data")

#: Stage order per node kind (state-machine contract; unit-tested).
STAGE_PLAN: dict[str, tuple[str, ...]] = {
    "em": (
        "wait-for-deps",
        "fetch-secrets",
        "fetch-media",
        "render-silent-xml",
        "run-setup",
        "systemd-unit",
        "backup-cron",
    ),
    "server": (
        "wait-for-deps",
        "fetch-secrets",
        "fetch-media",
        "render-silent-xml",
        "run-setup",
        "systemd-unit",
    ),
    # v4.1 agent = CONFIGURE + REGISTER ONLY: the Control-M/Agent is baked
    # into the ctm-agent image (packer/ctm-agent.pkr.hcl) — no media
    # download, no silent install at boot. Registration fetches the one
    # secret it needs (em-admin-password) on demand, in memory only.
    "agent": (
        "wait-for-deps",
        "configure-agent",
        "start-agent",
        "register-agent",
        "scheduled-events-watcher",
    ),
}

PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")


class InstallError(RuntimeError):
    """A stage cannot proceed; the message is safe to log (never secrets)."""


# ---------------------------------------------------------------------------
# Logging (stdout always; the log file only when not already teed by the
# cloud-init shim, which exports CTM_SHIM_LOGS=1 around its tee pipeline)
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    line = f"== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
    print(line, flush=True)
    if os.environ.get("CTM_SHIM_LOGS") == "1":
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # logging must never break the install


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested offline by deploy/tests/test_installer_access.py)
# ---------------------------------------------------------------------------


def parse_role(role: str) -> tuple[str, str]:
    """Split a node.json role into (kind, qualifier).

    'em-primary' -> ('em', 'primary'); 'server-secondary' -> ('server',
    'secondary'); 'agent' -> ('agent', ''); 'agent-dev' -> ('agent', 'dev').
    """
    kind, _, qualifier = role.partition("-")
    if kind not in KINDS:
        raise InstallError(f"node.json role {role!r}: kind must be one of {KINDS}")
    if kind in ("em", "server") and qualifier not in ("primary", "secondary"):
        raise InstallError(
            f"node.json role {role!r}: {kind} roles must be {kind}-primary or {kind}-secondary"
        )
    return kind, qualifier


def required_config_keys(kind: str) -> frozenset[str]:
    """Exact node.json key set for one node kind (common + role keys)."""
    if kind not in KINDS:
        raise InstallError(f"kind must be one of {KINDS}, got {kind!r}")
    return COMMON_CONFIG_KEYS | ROLE_CONFIG_KEYS[kind]


def validate_config(cfg: Mapping[str, object]) -> str:
    """Validate node.json shape; returns the node kind. Secrets are rejected."""
    role = str(cfg.get("role", ""))
    kind, _ = parse_role(role)
    required = required_config_keys(kind)
    missing = sorted(key for key in required if not str(cfg.get(key, "")) and key not in _OPTIONAL)
    if missing:
        raise InstallError(f"node.json is missing required keys: {missing}")
    bad = sorted(
        key
        for key in cfg
        if any(part in key.lower() for part in FORBIDDEN_CONFIG_KEY_PARTS)
    )
    if bad:
        raise InstallError(
            f"node.json carries forbidden (secret-looking) keys: {bad}; "
            "secrets live in Key Vault only"
        )
    return kind


#: Keys that may legitimately be empty (single-node platforms).
_OPTIONAL = frozenset({"em_secondary_host", "server_secondary_host"})


def secret_names(kind: str, cfg: Mapping[str, object]) -> tuple[str, ...]:
    """Key Vault secret names one node kind needs (fetched in-memory only)."""
    if kind == "em":
        return ("db-admin-password", "em-db-password", "em-admin-password")
    if kind == "server":
        return ("db-admin-password", str(cfg["ctm_db_secret_name"]))
    return ("em-admin-password",)


def render_xml(template: str, mapping: Mapping[str, str]) -> str:
    """Replace every ``__KEY__`` placeholder; fail on any leftover.

    Same semantics as the old per-role sed blocks plus the renderer's
    leftover check: a placeholder/contract drift aborts the install instead
    of handing BMC's installer a literal ``__TOKEN__``.
    """
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace(f"__{key}__", str(value))
    leftovers = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if leftovers:
        pretty = ", ".join(f"__{name}__" for name in leftovers)
        raise InstallError(f"silent-install XML has unreplaced placeholders: {pretty}")
    return rendered


def em_xml_mapping(
    cfg: Mapping[str, object], secrets: Mapping[str, str], hostname: str
) -> dict[str, str]:
    """The 14 EM silent-install placeholders (scripts/silent-install contract)."""
    em_home = f"{cfg['data_mount']}/{cfg['em_user']}"
    return {
        "DB_HOST": str(cfg["db_host"]),
        "DB_PORT": str(cfg["db_port"]),
        "DB_NAME": str(cfg["em_db_name"]),
        "DB_ADMIN_USER": str(cfg["db_admin_user"]),
        "DB_ADMIN_PASSWORD": secrets["db-admin-password"],
        "EM_DB_USER": str(cfg["em_db_user"]),
        "EM_DB_PASSWORD": secrets["em-db-password"],
        "EM_HOME": em_home,
        "EM_USER": str(cfg["em_user"]),
        "EM_ADMIN_PASSWORD": secrets["em-admin-password"],
        "HOSTNAME": hostname,
        "PRIMARY_HOST": str(cfg["em_primary_host"]),
        "SECONDARY_HOST": str(cfg["em_secondary_host"]),
        "WEB_PORT": str(cfg["web_port"]),
    }


def server_xml_mapping(
    cfg: Mapping[str, object], secrets: Mapping[str, str], hostname: str
) -> dict[str, str]:
    """The 14 Server silent-install placeholders."""
    ctm_home = f"{cfg['data_mount']}/{cfg['ctm_user']}"
    return {
        "DB_HOST": str(cfg["db_host"]),
        "DB_PORT": str(cfg["db_port"]),
        "DB_NAME": str(cfg["ctm_db_name"]),
        "DB_ADMIN_USER": str(cfg["db_admin_user"]),
        "DB_ADMIN_PASSWORD": secrets["db-admin-password"],
        "CTM_DB_USER": str(cfg["ctm_db_user"]),
        "CTM_DB_PASSWORD": secrets[str(cfg["ctm_db_secret_name"])],
        "CTM_HOME": ctm_home,
        "CTM_USER": str(cfg["ctm_user"]),
        "CTM_SERVER_NAME": str(cfg["ctm_server_name"]),
        "HOSTNAME": hostname,
        "PRIMARY_HOST": str(cfg["server_primary_host"]),
        "SECONDARY_HOST": str(cfg["server_secondary_host"]),
        "EM_HOST": str(cfg["em_host"]),
    }


def agent_config_mapping(cfg: Mapping[str, object], hostname: str) -> dict[str, str]:
    """CONFIG.dat values the boot-time agent configuration asserts (v4.1).

    The ctm-agent image ships the agent installed with PLACEHOLDER server
    settings; this mapping points it at the real Control-M/Server. Parameter
    names follow the 9.0.21 Control-M/Agent CONFIG.dat — validate them
    against your release's documentation (same caveat as the silent XMLs).
    """
    return {
        "CTMSHOST": str(cfg["server_primary_host"]),
        "CTMPERMHOSTS": str(cfg["server_primary_host"]),
        "CTMSPORT": AGENT_TO_SERVER_PORT,
        "AGTOSPORT": str(cfg["agent_port"]),
        "LOGICAL_AGENT_NAME": hostname,
    }


def update_config_dat(text: str, mapping: Mapping[str, str]) -> str:
    """Assert ``KEY VALUE`` lines in a Control-M CONFIG.dat body (pure).

    Existing keys are rewritten in place; missing keys are appended. Safe to
    re-run: asserting the same mapping twice is a no-op.
    """
    lines = text.splitlines()
    remaining = dict(mapping)
    for index, line in enumerate(lines):
        parts = line.split(None, 1)
        if parts and parts[0] in remaining:
            lines[index] = f"{parts[0]} {remaining.pop(parts[0])}"
    lines.extend(f"{key} {value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def installer_blob(kind: str, cfg: Mapping[str, object]) -> str:
    """Installer tarball blob name (EM/Server only — the agent is baked
    into the ctm-agent image and has no media blob)."""
    if kind not in ("em", "server"):
        raise InstallError(f"no installer blob for kind {kind!r} (agents are image-baked)")
    return str(cfg[{"em": "em_installer_blob", "server": "server_installer_blob"}[kind]])


def silent_blob(kind: str, cfg: Mapping[str, object]) -> str:
    """Silent-install XML blob name (EM/Server only)."""
    if kind not in ("em", "server"):
        raise InstallError(f"no silent XML blob for kind {kind!r} (agents are image-baked)")
    return str(cfg[{"em": "em_silent_blob", "server": "server_silent_blob"}[kind]])


def media_cache_dir(kind: str, version: str, shared_mount: Path = SHARED_MOUNT) -> Path:
    """NFS media cache directory for one kind+version (shared across nodes)."""
    return shared_mount / "installers" / kind / version


def blob_forbidden_hint(account: str, container: str, blob: str, identity: str) -> str:
    """Clear fail-fast message for a 403 from blob storage (agent VMSS etc.)."""
    return (
        f"access to https://{account}.blob.core.windows.net/{container}/{blob} was DENIED "
        f"(403). The VM's user-assigned managed identity ({identity}) needs the "
        "'Storage Blob Data Reader' role on the storage account — the platform grants it "
        "at account scope in bicep/modules/security.bicep; verify the role assignment "
        "exists and has propagated (can take a few minutes after deployment)."
    )


# ---------------------------------------------------------------------------
# Stage machine (pure core; IO happens inside each stage's run callable)
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    """One install stage.

    ``done`` is the real-world done-ness probe (marker files, ...); stages
    whose results cannot persist (in-memory secrets, the rendered XML) set
    ``persistent=False`` so the state file never claims them as done — they
    are instead skipped via ``done`` once the install marker exists.
    """

    name: str
    run: Callable[["Context"], None]
    done: Callable[["Context"], bool] | None = None
    persistent: bool = True


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("completed"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"completed": {}}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run_stages(
    stages: list[Stage],
    ctx: "Context",
    state: dict,
    save: Callable[[dict], None],
    logger: Callable[[str], None] = log,
) -> list[str]:
    """Run the stage plan; returns the names of the stages actually executed.

    Gating rules (unit-tested): a persistent stage recorded in the state
    file is skipped; any stage whose ``done`` probe answers True is skipped
    (and recorded, when persistent, so later runs stay quiet); everything
    else runs and is recorded on success. A failure stops the machine — the
    state file keeps earlier successes so a re-run resumes where it failed.
    """
    completed = state.setdefault("completed", {})
    executed: list[str] = []
    for stage in stages:
        if stage.persistent and stage.name in completed:
            logger(f"stage {stage.name}: already completed ({completed[stage.name]}); skipping")
            continue
        if stage.done is not None and stage.done(ctx):
            logger(f"stage {stage.name}: already satisfied; skipping")
            if stage.persistent:
                completed[stage.name] = _utcnow()
                save(state)
            continue
        logger(f"stage {stage.name}: running")
        stage.run(ctx)
        executed.append(stage.name)
        if stage.persistent:
            completed[stage.name] = _utcnow()
            save(state)
        logger(f"stage {stage.name}: done")
    return executed


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Azure plumbing: IMDS managed-identity tokens, Key Vault, Blob (urllib only)
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 30,
    context: ssl.SSLContext | None = None,
    retries: int = 5,
    retry_delay: int = 3,
) -> bytes:
    """urllib request with retries; raises the final error after the retries."""
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # 4xx (auth/missing) will not heal by retrying — surface it now.
            if 400 <= exc.code < 500:
                raise
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(retry_delay)
    raise InstallError(f"{method} {url} failed after {retries} attempts: {last}")


def imds_token(resource: str, identity_client_id: str) -> str:
    """Managed-identity access token for one resource (vault / storage)."""
    query = urllib.parse.urlencode(
        {
            "api-version": IMDS_TOKEN_API,
            "resource": resource,
            "client_id": identity_client_id,
        }
    )
    raw = _http(
        "GET",
        f"{IMDS_BASE}/identity/oauth2/token?{query}",
        headers={"Metadata": "true"},
        retries=10,
    )
    token = json.loads(raw).get("access_token", "")
    if not token:
        raise InstallError("IMDS returned no access_token for the managed identity")
    return token


def kv_secret(cfg: Mapping[str, object], name: str) -> str:
    """One Key Vault secret value — held in memory only, never logged."""
    token = imds_token("https://vault.azure.net", str(cfg["identity_client_id"]))
    url = (
        f"https://{cfg['key_vault_name']}.vault.azure.net/secrets/"
        f"{urllib.parse.quote(name)}?api-version={KEY_VAULT_API}"
    )
    raw = _http("GET", url, headers={"Authorization": f"Bearer {token}"})
    value = json.loads(raw).get("value")
    if value is None:
        raise InstallError(f"Key Vault secret {name!r} has no value")
    return str(value)


def _storage_headers(cfg: Mapping[str, object]) -> dict[str, str]:
    token = imds_token("https://storage.azure.com/", str(cfg["identity_client_id"]))
    return {"Authorization": f"Bearer {token}", "x-ms-version": BLOB_API_VERSION}


def blob_url(cfg: Mapping[str, object], container: str, blob: str) -> str:
    return f"https://{cfg['blob_account']}.blob.core.windows.net/{container}/{blob}"


def blob_get(cfg: Mapping[str, object], blob: str, dest: Path) -> None:
    """Download one installers-container blob to a local file (streamed)."""
    container = str(cfg["installers_container"])
    url = blob_url(cfg, container, blob)
    headers = _storage_headers(cfg)
    request = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=300) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out, length=4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # Fail FAST and clearly: on agent VMSS instances this is almost
            # always a missing/unpropagated Storage Blob Data Reader grant.
            raise InstallError(
                blob_forbidden_hint(
                    str(cfg["blob_account"]), container, blob, str(cfg["identity_client_id"])
                )
            ) from exc
        raise InstallError(f"blob download {container}/{blob} failed: HTTP {exc.code}") from exc


def blob_put_backup(cfg: Mapping[str, object], local: Path, blob: str) -> None:
    """Upload one file to the backups container (block blob, overwrite)."""
    url = blob_url(cfg, str(cfg["backups_container"]), blob)
    headers = _storage_headers(cfg) | {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "application/octet-stream",
    }
    _http("PUT", url, headers=headers, data=local.read_bytes(), timeout=600, retries=3)


# ---------------------------------------------------------------------------
# OS helpers
# ---------------------------------------------------------------------------


def run_cmd(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def run_as(user: str, command: str) -> None:
    """Run a shell command as a product user through a login shell (su -)."""
    run_cmd(["su", "-", user, "-c", command])


def ensure_user(user: str, home: str) -> None:
    try:
        run_cmd(["id", "-u", user], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        run_cmd(["useradd", "-m", "-d", home, "-s", "/bin/bash", user])
    Path(home).mkdir(parents=True, exist_ok=True)
    shutil.chown(home, user=user, group=user)


def short_hostname() -> str:
    return socket.gethostname().split(".", 1)[0]


def wait_for(
    probe: Callable[[], bool], timeout: int, description: str, interval: int = 5
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if probe():
                return
        except Exception:  # noqa: BLE001 - the probe failing means 'not yet'
            pass
        if time.monotonic() >= deadline:
            raise InstallError(f"timeout ({timeout}s) waiting for: {description}")
        time.sleep(interval)


def tcp_open(host: str, port: int, timeout: int = 5) -> bool:
    with socket.create_connection((host, port), timeout=timeout):
        return True


def https_up(url: str, timeout: int = 10) -> bool:
    """True once the endpoint answers ANY HTTP response over TLS.

    SECURITY NOTE: certificate verification is intentionally disabled, same
    as the previous bash bootstrap's `curl -k` — EM ships a self-signed
    certificate by default. Replace it with a CA-signed certificate and
    switch to a verifying context in production.
    """
    context = ssl._create_unverified_context()  # noqa: SLF001 - flagged above
    try:
        urllib.request.urlopen(url, timeout=timeout, context=context)
        return True
    except urllib.error.HTTPError:
        return True  # the service answered; status does not matter for 'up'
    except (urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# Context: parsed config + lazily fetched secrets + derived paths
# ---------------------------------------------------------------------------


@dataclass
class Context:
    cfg: dict
    kind: str
    qualifier: str  # primary|secondary for em/server; stage for agents
    hostname: str = field(default_factory=short_hostname)
    secrets: dict[str, str] = field(default_factory=dict)
    xml_path: Path | None = None

    @property
    def node_role(self) -> str:
        return self.qualifier if self.kind in ("em", "server") else "agent"

    @property
    def product_user(self) -> str:
        return str(
            self.cfg[{"em": "em_user", "server": "ctm_user", "agent": "agent_user"}[self.kind]]
        )

    @property
    def product_home(self) -> str:
        if self.kind == "agent":
            return f"/home/{self.cfg['agent_user']}"
        return f"{self.cfg['data_mount']}/{self.product_user}"

    @property
    def install_marker(self) -> Path:
        return Path(self.product_home) / ".ctm_installed"

    @property
    def media_dir(self) -> Path:
        return media_cache_dir(self.kind, str(self.cfg["ctm_version"]))

    def installed(self) -> bool:
        return self.install_marker.is_file()


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def stage_wait_for_deps(ctx: Context) -> None:
    cfg = ctx.cfg
    if ctx.kind == "em":
        if ctx.qualifier == "secondary":
            primary, port = cfg["em_primary_host"], int(cfg["web_port"])
            log(f"secondary EM: waiting for primary ({primary}) Control-M Web on :{port}")
            wait_for(
                lambda: https_up(f"https://{primary}:{port}/"),
                7200,
                f"primary EM https://{primary}:{port}/",
            )
    elif ctx.kind == "server":
        if ctx.qualifier == "secondary":
            primary = str(cfg["server_primary_host"])
            log(f"secondary Server: waiting for primary ({primary}) Configuration Agent :2369")
            wait_for(lambda: tcp_open(primary, 2369), 7200, f"{primary}:2369")
    else:  # agent: the EM Automation API must answer before registration
        endpoint = str(cfg["em_api_endpoint"])
        log(f"agent: waiting for the EM Automation API ({endpoint})")
        wait_for(lambda: https_up(f"{endpoint}/"), 1800, f"EM API {endpoint}")


def stage_fetch_secrets(ctx: Context) -> None:
    """Fetch the role's Key Vault secrets (in-memory only; never logged)."""
    for name in secret_names(ctx.kind, ctx.cfg):
        if name not in ctx.secrets:
            ctx.secrets[name] = kv_secret(ctx.cfg, name)
    log(f"fetched {len(ctx.secrets)} secret(s) from Key Vault {ctx.cfg['key_vault_name']}")


def stage_fetch_media(ctx: Context) -> None:
    """Download + extract the installer into the shared NFS cache (once).

    An exclusive flock around the download/extract fixes the concurrent-
    extract race: with several nodes (or a whole agent scale-set) booting at
    once, exactly one does the work while the rest wait on the lock and then
    see the .extracted marker.
    """
    if not SHARED_MOUNT.is_dir() or not os.path.ismount(SHARED_MOUNT):
        raise InstallError(
            f"{SHARED_MOUNT} is not mounted; the cloud-init shim mounts the shared "
            "NFS volume before running this installer"
        )
    cache = ctx.media_dir
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / ".extracted"
    lock_path = cache / ".extract.lock"
    blob = installer_blob(ctx.kind, ctx.cfg)
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if marker.is_file():
                log(f"media cache {cache} already extracted")
            else:
                tmp = cache / ".download.partial"
                log(f"downloading installer blob {blob} -> {tmp}")
                blob_get(ctx.cfg, blob, tmp)
                log(f"extracting into {cache}")
                run_cmd(["tar", "-xf", str(tmp), "-C", str(cache)])
                tmp.unlink(missing_ok=True)
                marker.touch()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # Ownership is re-asserted on every run (the cache is shared).
    run_cmd(["chown", "-R", f"{ctx.product_user}:{ctx.product_user}", str(cache)])


def stage_render_silent_xml(ctx: Context) -> None:
    """Fetch the silent-install XML and substitute its placeholders (0600)."""
    blob = silent_blob(ctx.kind, ctx.cfg)
    tmp = Path(f"/tmp/ctm-silent-{ctx.kind}.xml")
    blob_get(ctx.cfg, blob, tmp)
    template = tmp.read_text(encoding="utf-8")
    if ctx.kind == "em":
        mapping = em_xml_mapping(ctx.cfg, ctx.secrets, ctx.hostname)
    else:
        mapping = server_xml_mapping(ctx.cfg, ctx.secrets, ctx.hostname)
    tmp.write_text(render_xml(template, mapping), encoding="utf-8")
    os.chmod(tmp, 0o600)
    shutil.chown(tmp, user=ctx.product_user, group=ctx.product_user)
    ctx.xml_path = tmp
    log(f"rendered silent-install XML for {ctx.kind} ({len(mapping)} placeholders)")


def _find_setup(cache: Path) -> Path:
    """setup.sh at depth <=2 inside the extracted media (same as `find`)."""
    for depth1 in sorted(cache.glob("setup.sh")):
        return depth1
    for depth2 in sorted(cache.glob("*/setup.sh")):
        return depth2
    raise InstallError(f"setup.sh not found under {cache}")


def stage_run_setup(ctx: Context) -> None:
    """Run the BMC installer silently as the product user, then mark done."""
    ensure_user(ctx.product_user, ctx.product_home)
    if ctx.xml_path is None or not ctx.xml_path.is_file():
        raise InstallError("silent-install XML was not rendered (stage ordering bug)")
    setup = _find_setup(ctx.media_dir)
    log(f"installing Control-M/{ctx.kind.upper()} ({ctx.node_role}) from {setup}")
    try:
        run_as(ctx.product_user, f"{setup} -silent {ctx.xml_path}")
    finally:
        # The rendered XML holds secrets: delete it whether setup worked or not.
        ctx.xml_path.unlink(missing_ok=True)
        ctx.xml_path = None
    ctx.install_marker.write_text(_utcnow() + "\n", encoding="utf-8")
    shutil.chown(ctx.install_marker, user=ctx.product_user, group=ctx.product_user)


def _write_unit(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    run_cmd(["systemctl", "daemon-reload"])


def stage_systemd_unit(ctx: Context) -> None:
    """EM/Server systemd integration (same units as the old role templates)."""
    if ctx.kind == "em":
        unit_name = "controlm-em.service"
        description = f"Control-M/Enterprise Manager ({ctx.node_role})"
        start_cmd, stop_cmd = "start_all", "stop_all"
    else:
        unit_name = "controlm-server.service"
        description = f"Control-M/Server ({ctx.node_role})"
        if ctx.node_role == "primary":
            start_cmd, stop_cmd = "start_ca; start_ctm", "shut_ctm; shut_ca"
        else:
            start_cmd, stop_cmd = "start_ca", "shut_ca"
    unit = f"""[Unit]
Description={description}
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Type=forking
User={ctx.product_user}
WorkingDirectory={ctx.product_home}
ExecStart=/bin/bash -lc '{start_cmd}'
ExecStop=/bin/bash -lc '{stop_cmd}'
TimeoutStartSec=600
TimeoutStopSec=300
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
    _write_unit(Path("/etc/systemd/system") / unit_name, unit)
    try:
        run_cmd(["systemctl", "enable", "--now", unit_name])
    except subprocess.CalledProcessError as exc:  # same '|| true' as the old shim
        log(f"warning: systemctl enable --now {unit_name} failed ({exc}); continuing")


def stage_backup_cron(ctx: Context) -> None:
    """Nightly (01:15) EM metadata backup cron -> shared volume + blob."""
    cron = (
        "15 1 * * * root /usr/bin/python3 "
        f"{SCRIPT_INSTALL_PATH} --config {NODE_CONFIG_DEFAULT} --run-backup "
        ">> /var/log/ctm-backup.log 2>&1\n"
    )
    path = Path("/etc/cron.d/ctm-em-backup")
    path.write_text(cron, encoding="utf-8")
    os.chmod(path, 0o644)


def _backup_cron_done(ctx: Context) -> bool:
    path = Path("/etc/cron.d/ctm-em-backup")
    return path.is_file() and SCRIPT_INSTALL_PATH in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Agent boot-time stages (v4.1): the Control-M/Agent is BAKED into the
# ctm-agent image with placeholder server settings — at boot we only point
# it at the real Server (CONFIG.dat), start it and register it. No media
# download, no silent install.
# ---------------------------------------------------------------------------


def agent_ctm_home(ctx: Context) -> Path:
    """The baked agent's install dir (silent-agent.xml INSTALL_DIR)."""
    return Path(ctx.product_home) / "ctm"


def agent_config_dat(ctx: Context) -> Path:
    return agent_ctm_home(ctx) / "data" / "CONFIG.dat"


def _require_baked_agent(ctx: Context) -> Path:
    """Fail fast (clear message) when this VM does not run the agent image."""
    config = agent_config_dat(ctx)
    if not config.is_file():
        raise InstallError(
            f"{config} not found: the Control-M/Agent is expected to be BAKED "
            "into this image (gallery definition ctm-agent, built by "
            "packer/ctm-agent.pkr.hcl). This scale set appears to run the "
            "base (OS-only) image — check config.yaml image.agent and the "
            "compute deployment's agentImageId."
        )
    return config


def _agent_configured(ctx: Context) -> bool:
    """True when CONFIG.dat already asserts every boot-time value."""
    try:
        text = agent_config_dat(ctx).read_text(encoding="utf-8")
    except OSError:
        return False
    return update_config_dat(text, agent_config_mapping(ctx.cfg, ctx.hostname)) == text


def stage_configure_agent(ctx: Context) -> None:
    """Point the baked agent at its Server (name/ports/hostname; idempotent)."""
    config = _require_baked_agent(ctx)
    mapping = agent_config_mapping(ctx.cfg, ctx.hostname)
    updated = update_config_dat(config.read_text(encoding="utf-8"), mapping)
    config.write_text(updated, encoding="utf-8")
    shutil.chown(config, user=ctx.product_user, group=ctx.product_user)
    log(
        f"agent configured for {ctx.cfg['ctm_server_name']} "
        f"(server host {ctx.cfg['server_primary_host']}, "
        f"ports {AGENT_TO_SERVER_PORT}/{ctx.cfg['agent_port']})"
    )


AGENT_UNIT = "controlm-agent.service"


def stage_start_agent(ctx: Context) -> None:
    """systemd unit for the baked agent + start it now (idempotent)."""
    _require_baked_agent(ctx)
    user = ctx.product_user
    unit = f"""[Unit]
Description=Control-M/Agent (baked into the ctm-agent image; configured at boot)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User={user}
WorkingDirectory={ctx.product_home}
ExecStart=/bin/bash -lc 'start-ag -u {user} -p ALL'
ExecStop=/bin/bash -lc 'shut-ag -u {user} -p ALL'
TimeoutStartSec=300
TimeoutStopSec=120
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
    _write_unit(Path("/etc/systemd/system") / AGENT_UNIT, unit)
    run_cmd(["systemctl", "enable", "--now", AGENT_UNIT])


# ---------------------------------------------------------------------------
# Agent registration via the EM Automation API (urllib REST, no Node.js).
# Node.js remains in the golden image but is OPTIONAL at runtime now.
# ---------------------------------------------------------------------------


def _em_api_request(
    cfg: Mapping[str, object],
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict | None = None,
) -> dict:
    """One EM Automation API call.

    SECURITY NOTE: TLS verification is disabled (same as the old bootstrap's
    `curl -k` / NODE_TLS_REJECT_UNAUTHORIZED=0) because EM ships a
    self-signed certificate by default. Install a CA-signed certificate and
    make this verify in production.
    Endpoint paths follow the 9.0.21 Automation API; validate them against
    your release's `/automation-api` documentation.
    """
    context = ssl._create_unverified_context()  # noqa: SLF001 - flagged above
    url = f"{cfg['em_api_endpoint']}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    raw = _http(
        method, url, headers=headers, data=data, timeout=60, context=context, retries=3
    )
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _em_login(cfg: Mapping[str, object], password: str) -> str:
    result = _em_api_request(
        cfg,
        "POST",
        "/session/login",
        body={"username": str(cfg["em_api_user"]), "password": password},
    )
    token = str(result.get("token", ""))
    if not token:
        raise InstallError("EM Automation API login returned no token")
    return token


def _em_logout(cfg: Mapping[str, object], token: str) -> None:
    try:
        _em_api_request(cfg, "POST", "/session/logout", token=token)
    except Exception:  # noqa: BLE001 - logout is best-effort
        pass


def stage_register_agent(ctx: Context) -> None:
    """Add this agent to its Server's host group through the EM REST API."""
    cfg = ctx.cfg
    password = ctx.secrets.get("em-admin-password") or kv_secret(cfg, "em-admin-password")
    token = _em_login(cfg, password)
    server = urllib.parse.quote(str(cfg["ctm_server_name"]))
    hostgroup = urllib.parse.quote(str(cfg["hostgroup"]))
    agent = ctx.hostname
    try:
        try:
            _em_api_request(
                cfg,
                "POST",
                f"/config/server/{server}/hostgroup/{hostgroup}/agent",
                token=token,
                body={"host": agent},
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            # Idempotent re-run: an agent already in the host group is success.
            if exc.code in (400, 409) and ("exist" in detail.lower() or "already" in detail.lower()):
                log(f"agent {agent} is already in hostgroup {cfg['hostgroup']}")
            else:
                raise InstallError(
                    f"agent registration failed: HTTP {exc.code} {detail[:300]}"
                ) from exc
    finally:
        _em_logout(cfg, token)
    REGISTERED_MARKER.parent.mkdir(parents=True, exist_ok=True)
    REGISTERED_MARKER.write_text(_utcnow() + "\n", encoding="utf-8")
    log(f"agent {agent} registered to {cfg['ctm_server_name']}/{cfg['hostgroup']}")


def deregister_agent(cfg: Mapping[str, object]) -> None:
    """Remove this agent from the host group + Server (REST; best-effort)."""
    agent = short_hostname()
    server = urllib.parse.quote(str(cfg["ctm_server_name"]))
    hostgroup = urllib.parse.quote(str(cfg["hostgroup"]))
    password = kv_secret(cfg, "em-admin-password")
    token = _em_login(cfg, password)
    try:
        for method, path in (
            ("DELETE", f"/config/server/{server}/hostgroup/{hostgroup}/agent/{agent}"),
            ("DELETE", f"/config/server/{server}/agent/{agent}"),
        ):
            try:
                _em_api_request(cfg, method, path, token=token)
            except Exception as exc:  # noqa: BLE001 - keep going; termination is imminent
                log(f"deregister step {path} failed ({exc}); continuing")
    finally:
        _em_logout(cfg, token)
    # Stop the local agent processes (best-effort, like the old shut-ag call).
    try:
        run_as(str(cfg["agent_user"]), f"shut-ag -u {cfg['agent_user']} -p all || true")
    except subprocess.CalledProcessError:
        pass
    log(f"agent {agent} deregistered from {cfg['ctm_server_name']}")


def stage_scheduled_events_watcher(ctx: Context) -> None:
    """Install + start the Terminate/Preempt watcher (this script, --watch)."""
    unit = f"""[Unit]
Description=Control-M agent scheduled-events watcher (graceful deregistration)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 {SCRIPT_INSTALL_PATH} --config {NODE_CONFIG_DEFAULT} --watch-scheduled-events
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    _write_unit(Path("/etc/systemd/system/ctm-scheduled-events.service"), unit)
    run_cmd(["systemctl", "enable", "--now", "ctm-scheduled-events.service"])


def watch_scheduled_events(cfg: Mapping[str, object]) -> None:
    """Poll Azure Scheduled Events; deregister + approve Terminate/Preempt."""
    self_name = ""
    try:
        raw = _http(
            "GET",
            f"{IMDS_BASE}/instance/compute/name?api-version={IMDS_INSTANCE_API}&format=text",
            headers={"Metadata": "true"},
        )
        self_name = raw.decode("utf-8").strip().lower()
    except Exception as exc:  # noqa: BLE001
        log(f"could not read instance name from IMDS ({exc}); matching all events")
    events_url = f"{IMDS_BASE}/scheduledevents?api-version={IMDS_EVENTS_API}"
    while True:
        try:
            raw = _http("GET", events_url, headers={"Metadata": "true"}, retries=2)
            document = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001 - IMDS hiccup; poll again
            document = {}
        for event in document.get("Events") or []:
            if event.get("EventType") not in ("Terminate", "Preempt"):
                continue
            resources = [
                str(r).lower().lstrip("_") for r in (event.get("Resources") or [])
            ]
            if self_name and self_name not in resources:
                continue
            event_id = event.get("EventId", "")
            log(f"scheduled event {event_id} -> deregistering agent")
            try:
                deregister_agent(cfg)
            except Exception as exc:  # noqa: BLE001 - approve anyway; VM is going away
                log(f"deregistration failed ({exc}); approving the event regardless")
            try:
                _http(
                    "POST",
                    events_url,
                    headers={"Metadata": "true", "Content-Type": "application/json"},
                    data=json.dumps({"StartRequests": [{"EventId": event_id}]}).encode(),
                    retries=2,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"could not approve scheduled event {event_id}: {exc}")
            DEREGISTERED_MARKER.parent.mkdir(parents=True, exist_ok=True)
            DEREGISTERED_MARKER.touch()
        time.sleep(10)


# ---------------------------------------------------------------------------
# EM backup (invoked by the 01:15 cron installed by stage_backup_cron)
# ---------------------------------------------------------------------------


def run_backup(cfg: Mapping[str, object]) -> None:
    """Tar the EM metadata to the shared volume, push to blob, prune old."""
    em_home = Path(f"{cfg['data_mount']}/{cfg['em_user']}")
    backups = SHARED_MOUNT / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = backups / f"em-{short_hostname()}-{stamp}.tar.gz"
    try:
        run_cmd(["tar", "-czf", str(out), "-C", str(em_home), "etc", "data"])
    except subprocess.CalledProcessError:
        run_cmd(["tar", "-czf", str(out), "-C", str(em_home), "etc"])
    blob_put_backup(cfg, out, f"em/{out.name}")
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for old in backups.glob("em-*.tar.gz"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    log(f"EM backup {out.name} uploaded and old backups pruned")


# ---------------------------------------------------------------------------
# Stage plans
# ---------------------------------------------------------------------------


def build_stages(ctx: Context) -> list[Stage]:
    """The concrete stage list for this node's kind (STAGE_PLAN order)."""
    installed = lambda c: c.installed()  # noqa: E731 - tiny predicate
    registered = lambda c: REGISTERED_MARKER.is_file()  # noqa: E731
    # An already-registered agent proved the EM API reachable; EM/Server
    # nodes short-circuit the dependency wait once installed.
    wait_done = registered if ctx.kind == "agent" else installed
    common: dict[str, Stage] = {
        "wait-for-deps": Stage("wait-for-deps", stage_wait_for_deps, done=wait_done),
        "fetch-secrets": Stage(
            "fetch-secrets", stage_fetch_secrets, done=installed, persistent=False
        ),
        "fetch-media": Stage(
            "fetch-media",
            stage_fetch_media,
            # The NFS marker is the real source of truth (survives rebuilds),
            # but ownership must still be re-asserted -> only short-circuit
            # when this node has fully installed already.
            done=installed,
        ),
        "render-silent-xml": Stage(
            "render-silent-xml", stage_render_silent_xml, done=installed, persistent=False
        ),
        "run-setup": Stage("run-setup", stage_run_setup, done=installed),
        "systemd-unit": Stage("systemd-unit", stage_systemd_unit),
        "backup-cron": Stage("backup-cron", stage_backup_cron, done=_backup_cron_done),
        # v4.1 agent stages: configure + start the BAKED agent, then register.
        "configure-agent": Stage(
            "configure-agent", stage_configure_agent, done=_agent_configured
        ),
        "start-agent": Stage("start-agent", stage_start_agent),
        "register-agent": Stage("register-agent", stage_register_agent, done=registered),
        "scheduled-events-watcher": Stage(
            "scheduled-events-watcher", stage_scheduled_events_watcher
        ),
    }
    return [common[name] for name in STAGE_PLAN[ctx.kind]]


def install(cfg: dict) -> int:
    kind = validate_config(cfg)
    _, qualifier = parse_role(str(cfg["role"]))
    ctx = Context(cfg=cfg, kind=kind, qualifier=qualifier)
    ensure_user(ctx.product_user, ctx.product_home)
    state = load_state()
    state["role"] = str(cfg["role"])
    log(f"node install start role={cfg['role']} host={ctx.hostname}")
    run_stages(build_stages(ctx), ctx, state, save_state)
    log(f"node install finished role={cfg['role']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_node_config(path: str) -> dict:
    file_path = Path(path)
    if not file_path.is_file():
        raise InstallError(f"node config not found: {file_path}")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InstallError(f"{file_path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{file_path}: expected a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctm_node_install.py",
        description="Idempotent Control-M node installer (EM / Server / Agent).",
    )
    parser.add_argument(
        "--config", default=NODE_CONFIG_DEFAULT, help="node.json path (names/ids only)"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--run-backup", action="store_true", help="run the EM metadata backup and exit"
    )
    action.add_argument(
        "--watch-scheduled-events",
        action="store_true",
        help="poll Azure Scheduled Events and deregister on Terminate/Preempt",
    )
    action.add_argument(
        "--deregister", action="store_true", help="deregister this agent and exit"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_node_config(args.config)
        if args.run_backup:
            run_backup(cfg)
            return 0
        if args.watch_scheduled_events:
            watch_scheduled_events(cfg)
            return 0
        if args.deregister:
            deregister_agent(cfg)
            return 0
        if os.geteuid() != 0:
            raise InstallError("the install must run as root (writes units, users, mounts)")
        return install(cfg)
    except InstallError as exc:
        log(f"ERROR: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        log(f"ERROR: command failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
