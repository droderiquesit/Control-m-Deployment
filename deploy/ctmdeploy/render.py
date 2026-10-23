# ---------------------------------------------------------------------------
# Cloud-init rendering: concatenates cloud-init/common.sh.tmpl with the role
# template and replaces __TOKEN__ placeholders. Pure python, unit-testable;
# this module MUST stay importable without any Azure SDK installed.
# ---------------------------------------------------------------------------
"""Render cloud-init bootstrap scripts from __TOKEN__ templates.

The templates carry environment-specific values only — OS preparation is
baked into the Packer golden image. ``customData`` built here contains NO
secrets by design: nodes fetch credentials from Key Vault at boot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

TOKEN_RE = re.compile(r"__([A-Z0-9_]+)__")

ROLES = ("em", "server", "agent")

COMMON_TEMPLATE = "common.sh.tmpl"

#: Blob name (inside the installers container) of the idempotent node
#: installer; deploy.py uploads node-scripts/ctm_node_install.py there on
#: every compute deployment and the cloud-init shim fetches it at boot.
NODE_SCRIPT_BLOB = "scripts/ctm_node_install.py"

# Token names (without the __ delimiters) per template, exactly as the
# cloud-init templates declare them.
COMMON_TOKENS: tuple[str, ...] = (
    "ROLE",
    "ENVIRONMENT",
    "KEY_VAULT_NAME",
    "IDENTITY_CLIENT_ID",
    "BLOB_ACCOUNT",
    "INSTALLERS_CONTAINER",
    "BACKUPS_CONTAINER",
    "NFS_SERVER",
    "NFS_EXPORT",
    "DATA_MOUNT",
    "ATTACH_DATA_DISK",
    "CTM_VERSION",
    "DNS_ZONE",
    "NODE_SCRIPT_BLOB",
)

EM_TOKENS: tuple[str, ...] = (
    "EM_ROLE",
    "EM_USER",
    "EM_INSTALLER_BLOB",
    "EM_SILENT_BLOB",
    "DB_HOST",
    "DB_PORT",
    "EM_DB_NAME",
    "EM_DB_USER",
    "DB_ADMIN_USER",
    "EM_PRIMARY_HOST",
    "EM_SECONDARY_HOST",
    "WEB_PORT",
)

SERVER_TOKENS: tuple[str, ...] = (
    "SERVER_ROLE",
    "CTM_USER",
    "SERVER_INSTALLER_BLOB",
    "SERVER_SILENT_BLOB",
    "DB_HOST",
    "DB_PORT",
    "CTM_DB_NAME",
    "CTM_DB_USER",
    "CTM_DB_SECRET_NAME",
    "DB_ADMIN_USER",
    "CTM_SERVER_NAME",
    "SERVER_PRIMARY_HOST",
    "SERVER_SECONDARY_HOST",
    "EM_HOST",
)

AGENT_TOKENS: tuple[str, ...] = (
    "AGENT_USER",
    "EM_API_ENDPOINT",
    "EM_API_USER",
    "CTM_SERVER_NAME",
    "HOSTGROUP",
    "AGENT_PORT",
    "PROVISION_IMAGE",
    # v4.1: the agent is BAKED into the ctm-agent image — boot only
    # configures, starts and registers it (no media blob tokens).
    "SERVER_PRIMARY_HOST",
)

ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "em": EM_TOKENS,
    "server": SERVER_TOKENS,
    "agent": AGENT_TOKENS,
}


class RenderError(ValueError):
    """A template could not be rendered (missing file or unreplaced token)."""


def expected_tokens(role: str) -> frozenset[str]:
    """Full token set (common + role) a rendered script may reference."""
    if role not in ROLES:
        raise RenderError(f"role must be one of {ROLES}, got {role!r}")
    return frozenset(COMMON_TOKENS) | frozenset(ROLE_TOKENS[role])


def tokens_in(text: str) -> set[str]:
    """All __TOKEN__ names referenced by a template text."""
    return set(TOKEN_RE.findall(text))


def substitute(text: str, tokens: Mapping[str, object]) -> str:
    """Replace every ``__KEY__`` with ``tokens[KEY]`` (values are str()-ed).

    Raises :class:`RenderError` when any ``__[A-Z0-9_]+__`` placeholder is
    left unreplaced, so a template/token drift never reaches a VM.
    """
    rendered = text
    for key, value in tokens.items():
        rendered = rendered.replace(f"__{key.upper()}__", str(value))
    leftovers = sorted(tokens_in(rendered))
    if leftovers:
        pretty = ", ".join(f"__{name}__" for name in leftovers)
        raise RenderError(f"unreplaced tokens after rendering: {pretty}")
    return rendered


def default_template_dir() -> Path:
    """cloud-init/ directory at the repository root."""
    from ctmdeploy.config import find_repo_root  # local import: keeps module surface small

    return find_repo_root() / "cloud-init"


def render(
    role: str,
    tokens: Mapping[str, object],
    template_dir: str | Path | None = None,
) -> str:
    """Render the bootstrap script for one role.

    Reads ``cloud-init/common.sh.tmpl`` plus ``cloud-init/<role>.sh.tmpl``,
    concatenates them with a newline (same joining as the original templates) and
    substitutes the ``__TOKEN__`` placeholders.
    """
    if role not in ROLES:
        raise RenderError(f"role must be one of {ROLES}, got {role!r}")
    directory = Path(template_dir) if template_dir is not None else default_template_dir()
    parts: list[str] = []
    for filename in (COMMON_TEMPLATE, f"{role}.sh.tmpl"):
        path = directory / filename
        if not path.is_file():
            raise RenderError(f"cloud-init template not found: {path}")
        parts.append(path.read_text(encoding="utf-8"))
    return substitute("\n".join(parts), tokens)


# ---------------------------------------------------------------------------
# Token builders (pure; deploy.py fills them from config + foundation outputs)
# ---------------------------------------------------------------------------


def common_tokens(
    *,
    role: str,
    environment: str,
    key_vault_name: str,
    identity_client_id: str,
    blob_account: str,
    nfs_server: str,
    nfs_export: str,
    ctm_version: str,
    dns_zone: str,
    attach_data_disk: bool,
    installers_container: str = "installers",
    backups_container: str = "backups",
    data_mount: str = "/ctm",
    node_script_blob: str = NODE_SCRIPT_BLOB,
) -> dict[str, str]:
    """Tokens of common.sh.tmpl. `role` is the composite value, e.g. em-primary."""
    return {
        "ROLE": role,
        "ENVIRONMENT": environment,
        "KEY_VAULT_NAME": key_vault_name,
        "IDENTITY_CLIENT_ID": identity_client_id,
        "BLOB_ACCOUNT": blob_account,
        "INSTALLERS_CONTAINER": installers_container,
        "BACKUPS_CONTAINER": backups_container,
        "NFS_SERVER": nfs_server,
        "NFS_EXPORT": nfs_export,
        "DATA_MOUNT": data_mount,
        "ATTACH_DATA_DISK": "true" if attach_data_disk else "false",
        "CTM_VERSION": ctm_version,
        "DNS_ZONE": dns_zone,
        "NODE_SCRIPT_BLOB": node_script_blob,
    }


def em_tokens(
    *,
    em_role: str,
    em_user: str,
    em_installer_blob: str,
    em_silent_blob: str,
    db_host: str,
    db_port: int,
    em_db_name: str,
    em_db_user: str,
    db_admin_user: str,
    em_primary_host: str,
    em_secondary_host: str,
    web_port: int,
) -> dict[str, str]:
    """Tokens of em.sh.tmpl (em_secondary_host is '' for single-EM platforms)."""
    return {
        "EM_ROLE": em_role,
        "EM_USER": em_user,
        "EM_INSTALLER_BLOB": em_installer_blob,
        "EM_SILENT_BLOB": em_silent_blob,
        "DB_HOST": db_host,
        "DB_PORT": str(db_port),
        "EM_DB_NAME": em_db_name,
        "EM_DB_USER": em_db_user,
        "DB_ADMIN_USER": db_admin_user,
        "EM_PRIMARY_HOST": em_primary_host,
        "EM_SECONDARY_HOST": em_secondary_host,
        "WEB_PORT": str(web_port),
    }


def server_tokens(
    *,
    server_role: str,
    ctm_user: str,
    server_installer_blob: str,
    server_silent_blob: str,
    db_host: str,
    db_port: int,
    ctm_db_name: str,
    ctm_db_user: str,
    ctm_db_secret_name: str,
    db_admin_user: str,
    ctm_server_name: str,
    server_primary_host: str,
    server_secondary_host: str,
    em_host: str,
) -> dict[str, str]:
    """Tokens of server.sh.tmpl (server_secondary_host '' without a b node)."""
    return {
        "SERVER_ROLE": server_role,
        "CTM_USER": ctm_user,
        "SERVER_INSTALLER_BLOB": server_installer_blob,
        "SERVER_SILENT_BLOB": server_silent_blob,
        "DB_HOST": db_host,
        "DB_PORT": str(db_port),
        "CTM_DB_NAME": ctm_db_name,
        "CTM_DB_USER": ctm_db_user,
        "CTM_DB_SECRET_NAME": ctm_db_secret_name,
        "DB_ADMIN_USER": db_admin_user,
        "CTM_SERVER_NAME": ctm_server_name,
        "SERVER_PRIMARY_HOST": server_primary_host,
        "SERVER_SECONDARY_HOST": server_secondary_host,
        "EM_HOST": em_host,
    }


def agent_tokens(
    *,
    agent_user: str,
    em_api_endpoint: str,
    em_api_user: str,
    ctm_server_name: str,
    hostgroup: str,
    agent_port: int,
    provision_image: str,
    server_primary_host: str,
) -> dict[str, str]:
    """Tokens of agent.sh.tmpl (v4.1: the agent is baked into the ctm-agent
    image; boot only configures, starts and registers it via REST)."""
    return {
        "AGENT_USER": agent_user,
        "EM_API_ENDPOINT": em_api_endpoint,
        "EM_API_USER": em_api_user,
        "CTM_SERVER_NAME": ctm_server_name,
        "HOSTGROUP": hostgroup,
        "AGENT_PORT": str(agent_port),
        "PROVISION_IMAGE": provision_image,
        "SERVER_PRIMARY_HOST": server_primary_host,
    }
