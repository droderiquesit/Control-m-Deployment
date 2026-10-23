# ---------------------------------------------------------------------------
# Pure-python tests for the v4/v4.1 additions:
#   * node-scripts/ctm_node_install.py — the stdlib-only idempotent node
#     installer: stage gating, role parsing, the 14-placeholder XML render
#     (EM/Server), EM/Server blob-name mapping, the v4.1 agent configure/
#     start/register stages (the agent is BAKED into the ctm-agent image),
#     config validation (node.json NEVER carries secrets);
#   * the cloud-init shims' node.json contract (rendered heredoc == the
#     installer's required key set, secret-free, agent media-blob-free);
#   * the Packer ctm-agent build's silent-agent.xml contract and the CLI's
#     `image build --image agent --media-path` validation;
#   * the deploy engine's access phase (param derivation; appgw backend IPs
#     = BOTH internal LB frontend IPs) and the compute phase's two-image
#     model (imageId = base, agentImageId = agent) + agent pools carrying
#     the shared user-assigned identity for the node-script blob fetch;
#   * kvsync packaging helpers.
# No Azure SDK required: pyyaml only.
# Runnable via `python3 -m pytest` or plain `python3 deploy/tests/test_installer_access.py`.
# ---------------------------------------------------------------------------
"""v4/v4.1 tests: node installer, node.json contract, images, access, kvsync."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any

import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
TEMPLATE_DIR = REPO_ROOT / "cloud-init"
NODE_SCRIPT = REPO_ROOT / "node-scripts" / "ctm_node_install.py"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from ctmdeploy import config as cfgmod  # noqa: E402
from ctmdeploy import kvsync, render  # noqa: E402


def _skip(message: str) -> None:
    raise unittest.SkipTest(message)


def _load_installer():
    """Import node-scripts/ctm_node_install.py as a module (stdlib-only)."""
    if not NODE_SCRIPT.is_file():
        _skip(f"{NODE_SCRIPT} does not exist")
    spec = importlib.util.spec_from_file_location("ctm_node_install", NODE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves field types through sys.modules[cls.__module__].
    sys.modules["ctm_node_install"] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = _load_installer()


def _load_dict(data: dict[str, Any]) -> cfgmod.EnvironmentConfig:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
        path = handle.name
    try:
        return cfgmod.load_config_file(path, allow_placeholders=True)
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Installer: role parsing + stage plan
# ---------------------------------------------------------------------------


def test_parse_role() -> None:
    assert INSTALLER.parse_role("em-primary") == ("em", "primary")
    assert INSTALLER.parse_role("em-secondary") == ("em", "secondary")
    assert INSTALLER.parse_role("server-primary") == ("server", "primary")
    assert INSTALLER.parse_role("server-secondary") == ("server", "secondary")
    assert INSTALLER.parse_role("agent") == ("agent", "")
    assert INSTALLER.parse_role("agent-dev") == ("agent", "dev")
    for bad in ("database", "em-tertiary", "server", ""):
        try:
            INSTALLER.parse_role(bad)
        except INSTALLER.InstallError:
            pass
        else:
            raise AssertionError(f"role {bad!r} was accepted")


def test_stage_plan_matches_contract() -> None:
    assert INSTALLER.STAGE_PLAN["em"] == (
        "wait-for-deps",
        "fetch-secrets",
        "fetch-media",
        "render-silent-xml",
        "run-setup",
        "systemd-unit",
        "backup-cron",  # 01:15 EM metadata backup — em only
    )
    assert INSTALLER.STAGE_PLAN["server"] == (
        "wait-for-deps",
        "fetch-secrets",
        "fetch-media",
        "render-silent-xml",
        "run-setup",
        "systemd-unit",
    )
    # v4.1: the agent is BAKED into the ctm-agent image — no fetch-media /
    # render-silent-xml / run-setup at boot. Configure + start + register only
    # (registration fetches em-admin-password on demand, in memory).
    assert INSTALLER.STAGE_PLAN["agent"] == (
        "wait-for-deps",
        "configure-agent",
        "start-agent",
        "register-agent",
        "scheduled-events-watcher",
    )


# ---------------------------------------------------------------------------
# Installer: stage machine gating (pure)
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal context stand-in for gating tests."""


def _stage(name, log, *, done=None, persistent=True, fail=False):
    def run(ctx):
        if fail:
            raise RuntimeError(f"{name} boom")
        log.append(name)

    return INSTALLER.Stage(name=name, run=run, done=done, persistent=persistent)


def test_stage_gating_runs_in_order_and_records() -> None:
    log: list[str] = []
    saves: list[dict] = []
    state: dict = {"completed": {}}
    stages = [_stage("a", log), _stage("b", log), _stage("c", log)]
    executed = INSTALLER.run_stages(stages, _Ctx(), state, saves.append, logger=lambda m: None)
    assert executed == ["a", "b", "c"] and log == ["a", "b", "c"]
    assert set(state["completed"]) == {"a", "b", "c"}
    assert len(saves) == 3  # state saved after every persistent stage


def test_stage_gating_skips_recorded_and_done_stages() -> None:
    log: list[str] = []
    state: dict = {"completed": {"a": "2026-01-01T00:00:00Z"}}
    stages = [
        _stage("a", log, fail=True),  # recorded -> must NOT run (would raise)
        _stage("b", log, done=lambda ctx: True, fail=True),  # done -> skip + record
        _stage("c", log),
    ]
    executed = INSTALLER.run_stages(stages, _Ctx(), state, lambda s: None, logger=lambda m: None)
    assert executed == ["c"] and log == ["c"]
    assert "b" in state["completed"]  # the satisfied stage was recorded


def test_stage_gating_ephemeral_stages_are_never_recorded() -> None:
    log: list[str] = []
    state: dict = {"completed": {}}
    stages = [_stage("fetch-secrets", log, done=lambda ctx: False, persistent=False)]
    INSTALLER.run_stages(stages, _Ctx(), state, lambda s: None, logger=lambda m: None)
    assert log == ["fetch-secrets"]
    assert state["completed"] == {}  # in-memory results cannot be claimed done

    # ...but their done() probe (install marker) still short-circuits them.
    log.clear()
    stages = [_stage("fetch-secrets", log, done=lambda ctx: True, persistent=False, fail=True)]
    INSTALLER.run_stages(stages, _Ctx(), state, lambda s: None, logger=lambda m: None)
    assert log == [] and state["completed"] == {}


def test_stage_gating_failure_stops_and_keeps_progress() -> None:
    log: list[str] = []
    state: dict = {"completed": {}}
    stages = [_stage("a", log), _stage("b", log, fail=True), _stage("c", log)]
    try:
        INSTALLER.run_stages(stages, _Ctx(), state, lambda s: None, logger=lambda m: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("stage failure did not propagate")
    assert log == ["a"]
    assert set(state["completed"]) == {"a"}  # resume point for the re-run


# ---------------------------------------------------------------------------
# Installer: XML render (14-placeholder contract) + blob mapping
# ---------------------------------------------------------------------------

_EM_CFG = {
    "role": "em-primary",
    "environment": "prod",
    "key_vault_name": "ctm-prod-kv-p01",
    "identity_client_id": "00000000-0000-0000-0000-000000000000",
    "blob_account": "ctmprodblobp01",
    "installers_container": "installers",
    "backups_container": "backups",
    "nfs_server": "ctmprodfilesp01.file.core.windows.net",
    "nfs_export": "/ctmprodfilesp01/ctm-shared",
    "data_mount": "/ctm",
    "ctm_version": "9.0.21.300",
    "dns_zone": "prod.ctm.internal",
    "em_role": "primary",
    "em_user": "emuser",
    "em_installer_blob": "em/DREM.9.0.21.300_Linux-x86_64.tar.Z",
    "em_silent_blob": "em/silent-em-primary.xml",
    "db_host": "ctm-db.prod.ctm.internal",
    "db_port": "5432",
    "em_db_name": "emdb",
    "em_db_user": "emuser",
    "db_admin_user": "ctmadmin",
    "em_primary_host": "ctm-em-a",
    "em_secondary_host": "ctm-em-b",
    "web_port": "8443",
}

_SERVER_CFG = {
    "role": "server-secondary",
    **{k: _EM_CFG[k] for k in INSTALLER.COMMON_CONFIG_KEYS if k != "role"},
    "server_role": "secondary",
    "ctm_user": "controlm",
    "server_installer_blob": "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z",
    "server_silent_blob": "server/silent-server-secondary.xml",
    "db_host": "ctm-db.prod.ctm.internal",
    "db_port": "5432",
    "ctm_db_name": "ctm_prod",
    "ctm_db_user": "ctm_prod",
    "ctm_db_secret_name": "ctm-db-password-prod",
    "db_admin_user": "ctmadmin",
    "ctm_server_name": "CTM_PROD",
    "server_primary_host": "ctm-srv-prod-a",
    "server_secondary_host": "ctm-srv-prod-b",
    "em_host": "ctm-em-a",
}

# v4.1: the agent node.json carries configure/register inputs only — no
# media blob names (the agent is baked into the ctm-agent image).
_AGENT_CFG = {
    "role": "agent-prod",
    **{k: _EM_CFG[k] for k in INSTALLER.COMMON_CONFIG_KEYS if k != "role"},
    "agent_user": "ctmagent",
    "em_api_endpoint": "https://ctm-web.prod.ctm.internal:8443/automation-api",
    "em_api_user": "emuser",
    "ctm_server_name": "CTM_PROD",
    "hostgroup": "prod-agents",
    "agent_port": "7006",
    "provision_image": "Agent.Linux",
    "server_primary_host": "ctm-srv-prod-a",
}

_SECRETS = {
    "db-admin-password": "da-value",
    "em-db-password": "emdb-value",
    "em-admin-password": "ea-value",
    "ctm-db-password-prod": "ctm-value",
}


def test_xml_mappings_carry_exactly_the_contract_placeholders() -> None:
    em = INSTALLER.em_xml_mapping(_EM_CFG, _SECRETS, "ctm-em-a")
    assert len(em) == 14  # the 14-placeholder EM contract
    assert set(em) == {
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD",
        "EM_DB_USER", "EM_DB_PASSWORD", "EM_HOME", "EM_USER", "EM_ADMIN_PASSWORD",
        "HOSTNAME", "PRIMARY_HOST", "SECONDARY_HOST", "WEB_PORT",
    }
    assert em["EM_HOME"] == "/ctm/emuser"

    server = INSTALLER.server_xml_mapping(_SERVER_CFG, _SECRETS, "ctm-srv-prod-b")
    assert len(server) == 14  # the 14-placeholder Server contract
    assert set(server) == {
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD",
        "CTM_DB_USER", "CTM_DB_PASSWORD", "CTM_HOME", "CTM_USER", "CTM_SERVER_NAME",
        "HOSTNAME", "PRIMARY_HOST", "SECONDARY_HOST", "EM_HOST",
    }
    assert server["CTM_DB_PASSWORD"] == "ctm-value"  # per-stage secret name

    # v4.1: the agent renders no XML at boot — it asserts CONFIG.dat values
    # into the BAKED agent instead (server host/name, ports, logical name).
    agent = INSTALLER.agent_config_mapping(_AGENT_CFG, "agent-host")
    assert set(agent) == {
        "CTMSHOST", "CTMPERMHOSTS", "CTMSPORT", "AGTOSPORT", "LOGICAL_AGENT_NAME",
    }
    assert agent["CTMSHOST"] == "ctm-srv-prod-a"
    assert agent["CTMSPORT"] == "7005"  # agent -> server (BMC default)
    assert agent["AGTOSPORT"] == "7006"  # server -> agent (config agent_port)
    assert agent["LOGICAL_AGENT_NAME"] == "agent-host"


def test_render_xml_replaces_and_rejects_leftovers() -> None:
    out = INSTALLER.render_xml("<p a='__A__' b='__B__'/>", {"A": "x", "B": 2})
    assert out == "<p a='x' b='2'/>"
    try:
        INSTALLER.render_xml("<p a='__A__' c='__MISSING__'/>", {"A": "x"})
    except INSTALLER.InstallError as exc:
        assert "__MISSING__" in str(exc)
    else:
        raise AssertionError("leftover placeholder was not rejected")


#: The silent-agent.xml placeholder contract, now substituted at BUILD time
#: by packer/scripts/30-agent-install.sh (placeholder server config; the
#: node installer rewrites the real values into CONFIG.dat at boot).
_AGENT_BUILD_XML_MAPPING = {
    "AGENT_HOME": "/home/ctmagent/ctm",
    "AGENT_USER": "ctmagent",
    "HOSTNAME": "pkr-build-vm",
    "CTM_SERVER_NAME": "CTM_PLACEHOLDER",
    "PRIMARY_HOST": "ctm-srv-placeholder-a",
    "AGENT_TO_SERVER_PORT": "7005",
    "SERVER_TO_AGENT_PORT": "7006",
}


def test_repo_silent_xml_examples_render_cleanly() -> None:
    """The committed example XMLs and their consumers' mappings agree."""
    cases = [
        ("silent-em-primary.xml", INSTALLER.em_xml_mapping(_EM_CFG, _SECRETS, "h")),
        ("silent-server-primary.xml", INSTALLER.server_xml_mapping(_SERVER_CFG, _SECRETS, "h")),
        # v4.1: silent-agent.xml is consumed by the PACKER agent-image build.
        ("silent-agent.xml", _AGENT_BUILD_XML_MAPPING),
    ]
    for filename, mapping in cases:
        path = REPO_ROOT / "scripts" / "silent-install" / filename
        if not path.is_file():
            _skip(f"{path} does not exist yet")
        rendered = INSTALLER.render_xml(path.read_text(encoding="utf-8"), mapping)
        assert "__" not in re.sub(r"<!--.*?-->", "", rendered, flags=re.S), filename


def test_packer_agent_script_covers_the_silent_xml_contract() -> None:
    """30-agent-install.sh substitutes every silent-agent.xml placeholder,
    never starts/registers the agent, and cleans the media."""
    script_path = REPO_ROOT / "packer" / "scripts" / "30-agent-install.sh"
    xml_path = REPO_ROOT / "scripts" / "silent-install" / "silent-agent.xml"
    if not script_path.is_file() or not xml_path.is_file():
        _skip("packer agent script / silent-agent.xml not present")
    script = script_path.read_text(encoding="utf-8")
    xml_placeholders = set(re.findall(r"__([A-Z0-9_]+)__", xml_path.read_text(encoding="utf-8")))
    assert xml_placeholders == set(_AGENT_BUILD_XML_MAPPING), xml_placeholders
    for name in xml_placeholders:
        assert f"__{name}__" in script, f"30-agent-install.sh never substitutes __{name}__"
    # DO NOT start or register at build time; media is deleted afterwards.
    assert 'START_AGENT"' in script and "no" in script
    assert "shut-ag" in script
    assert "rm -rf" in script and "MEDIA_DIR" in script
    # The BASE image build must not install any BMC software.
    base = (REPO_ROOT / "packer" / "ctm-rhel9.pkr.hcl").read_text(encoding="utf-8")
    assert "30-agent-install" not in base
    agent = (REPO_ROOT / "packer" / "ctm-agent.pkr.hcl").read_text(encoding="utf-8")
    assert "30-agent-install.sh" in agent and "media_path" in agent


def test_installer_blob_mapping() -> None:
    assert INSTALLER.installer_blob("em", _EM_CFG) == "em/DREM.9.0.21.300_Linux-x86_64.tar.Z"
    assert INSTALLER.silent_blob("em", _EM_CFG) == "em/silent-em-primary.xml"
    assert (
        INSTALLER.installer_blob("server", _SERVER_CFG)
        == "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z"
    )
    # v4.1: there is NO agent media blob — the agent is image-baked.
    for fn in (INSTALLER.installer_blob, INSTALLER.silent_blob):
        try:
            fn("agent", _AGENT_CFG)
        except INSTALLER.InstallError as exc:
            assert "image-baked" in str(exc)
        else:
            raise AssertionError("agent blob mapping should no longer exist")
    cache = INSTALLER.media_cache_dir("server", "9.0.21.300")
    assert str(cache) == "/mnt/ctm-shared/installers/server/9.0.21.300"


def test_agent_configure_helpers_are_idempotent() -> None:
    """update_config_dat asserts KEY VALUE lines in place (pure, re-runnable)."""
    mapping = INSTALLER.agent_config_mapping(_AGENT_CFG, "vmss-000001")
    baked = (
        "AGCMNDATA /home/ctmagent/ctm/data\n"
        "CTMSHOST ctm-srv-placeholder-a\n"
        "CTMPERMHOSTS ctm-srv-placeholder-a\n"
        "CTMSPORT 7005\n"
        "AGTOSPORT 7006\n"
    )
    configured = INSTALLER.update_config_dat(baked, mapping)
    lines = configured.splitlines()
    assert "CTMSHOST ctm-srv-prod-a" in lines
    assert "CTMPERMHOSTS ctm-srv-prod-a" in lines
    assert "CTMSPORT 7005" in lines and "AGTOSPORT 7006" in lines
    assert "LOGICAL_AGENT_NAME vmss-000001" in lines  # appended (missing key)
    assert "AGCMNDATA /home/ctmagent/ctm/data" in lines  # untouched
    # Idempotent: asserting the same mapping again is a byte-for-byte no-op.
    assert INSTALLER.update_config_dat(configured, mapping) == configured


def test_secret_names_per_role() -> None:
    assert INSTALLER.secret_names("em", _EM_CFG) == (
        "db-admin-password",
        "em-db-password",
        "em-admin-password",
    )
    assert INSTALLER.secret_names("server", _SERVER_CFG) == (
        "db-admin-password",
        "ctm-db-password-prod",
    )
    assert INSTALLER.secret_names("agent", _AGENT_CFG) == ("em-admin-password",)


def test_blob_403_fails_fast_with_role_hint() -> None:
    hint = INSTALLER.blob_forbidden_hint("acct", "installers", "agent/x.tar.Z", "client-id")
    assert "403" in hint
    assert "Storage Blob Data Reader" in hint
    assert "agent/x.tar.Z" in hint and "client-id" in hint


def test_validate_config_accepts_roles_and_rejects_secret_keys() -> None:
    assert INSTALLER.validate_config(_EM_CFG) == "em"
    assert INSTALLER.validate_config(_SERVER_CFG) == "server"
    assert INSTALLER.validate_config(_AGENT_CFG) == "agent"

    incomplete = dict(_EM_CFG)
    del incomplete["em_installer_blob"]
    try:
        INSTALLER.validate_config(incomplete)
    except INSTALLER.InstallError as exc:
        assert "em_installer_blob" in str(exc)
    else:
        raise AssertionError("missing key was accepted")

    # A secret-looking key is rejected outright: node.json NEVER holds secrets.
    poisoned = dict(_AGENT_CFG)
    poisoned["em_admin_password"] = "oops"
    try:
        INSTALLER.validate_config(poisoned)
    except INSTALLER.InstallError as exc:
        assert "secret" in str(exc).lower()
    else:
        raise AssertionError("a *_password key in node.json was accepted")

    # Empty secondary hosts are fine (single-node platforms).
    single = dict(_EM_CFG)
    single["em_secondary_host"] = ""
    assert INSTALLER.validate_config(single) == "em"


# ---------------------------------------------------------------------------
# Cloud-init shims: the rendered node.json matches the installer's contract
# and never carries a secret
# ---------------------------------------------------------------------------

_NODEJSON_RE = re.compile(r"cat > /etc/ctm/node\.json <<'NODEJSON'\n(.*?)\nNODEJSON", re.S)


def _dummy_tokens(role: str) -> dict[str, str]:
    common = render.common_tokens(
        role={"em": "em-primary", "server": "server-secondary", "agent": "agent-prod"}[role],
        environment="prod",
        key_vault_name="ctm-prod-kv-p01",
        identity_client_id="11111111-1111-1111-1111-111111111111",
        blob_account="ctmprodblobp01",
        nfs_server="ctmprodfilesp01.file.core.windows.net",
        nfs_export="/ctmprodfilesp01/ctm-shared",
        ctm_version="9.0.21.300",
        dns_zone="prod.ctm.internal",
        attach_data_disk=role != "agent",
    )
    if role == "em":
        extra = render.em_tokens(
            em_role="primary",
            em_user="emuser",
            em_installer_blob="em/DREM.9.0.21.300_Linux-x86_64.tar.Z",
            em_silent_blob="em/silent-em-primary.xml",
            db_host="ctm-db.prod.ctm.internal",
            db_port=5432,
            em_db_name="emdb",
            em_db_user="emuser",
            db_admin_user="ctmadmin",
            em_primary_host="ctm-em-a",
            em_secondary_host="ctm-em-b",
            web_port=8443,
        )
    elif role == "server":
        extra = render.server_tokens(
            server_role="secondary",
            ctm_user="controlm",
            server_installer_blob="server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z",
            server_silent_blob="server/silent-server-secondary.xml",
            db_host="ctm-db.prod.ctm.internal",
            db_port=5432,
            ctm_db_name="ctm_prod",
            ctm_db_user="ctm_prod",
            ctm_db_secret_name="ctm-db-password-prod",
            db_admin_user="ctmadmin",
            ctm_server_name="CTM_PROD",
            server_primary_host="ctm-srv-prod-a",
            server_secondary_host="ctm-srv-prod-b",
            em_host="ctm-em-a",
        )
    else:
        extra = render.agent_tokens(
            agent_user="ctmagent",
            em_api_endpoint="https://ctm-web.prod.ctm.internal:8443/automation-api",
            em_api_user="emuser",
            ctm_server_name="CTM_PROD",
            hostgroup="prod-agents",
            agent_port=7006,
            provision_image="Agent.Linux",
            server_primary_host="ctm-srv-prod-a",
        )
    return common | extra


def _rendered_node_json(role: str) -> dict[str, Any]:
    if not TEMPLATE_DIR.is_dir():
        _skip(f"{TEMPLATE_DIR} does not exist")
    script = render.render(role, _dummy_tokens(role), TEMPLATE_DIR)
    match = _NODEJSON_RE.search(script)
    assert match, f"{role}.sh.tmpl writes no /etc/ctm/node.json heredoc"
    return json.loads(match.group(1))


def test_node_json_matches_installer_contract_and_has_no_secrets() -> None:
    for role in ("em", "server", "agent"):
        node = _rendered_node_json(role)
        kind, _ = INSTALLER.parse_role(node["role"])
        assert kind == role
        # EXACT key set: the shim and the installer agree, no drift.
        assert set(node) == set(INSTALLER.required_config_keys(role)), role
        # The installer's own validation accepts the shim's output.
        assert INSTALLER.validate_config(node) == role
        # No secret-looking keys AND no secret-looking values (names/ids only).
        for key, value in node.items():
            lowered = key.lower()
            assert "password" not in lowered and "secret_value" not in lowered, key
            assert "PASSWORD" not in str(value), (role, key)
        # Nothing render-shaped survived.
        assert not render.tokens_in(json.dumps(node)), role


def test_shims_fetch_and_run_the_node_installer() -> None:
    """The thin shims fetch scripts/ctm_node_install.py via MI and run it."""
    for role in ("em", "server", "agent"):
        script = render.render(role, _dummy_tokens(role), TEMPLATE_DIR)
        assert "scripts/ctm_node_install.py" in script, role
        assert "ctm-install.service" in script, role  # manual re-run oneshot
        assert "run_node_installer" in script, role
        # The shim carries NO install logic any more (it lives in python):
        # no Key Vault access, no XML sed, no setup.sh invocation, no npm.
        assert "kv_secret" not in script, role
        assert "sed -i" not in script, role
        assert "-silent" not in script, role
        assert "npm install" not in script, role
    # v4.1: the agent shim names no media blobs at all — the agent is baked
    # into the ctm-agent image; boot only configures/starts/registers it.
    agent_script = render.render("agent", _dummy_tokens("agent"), TEMPLATE_DIR)
    assert "agent_installer_blob" not in agent_script
    assert "agent_silent_blob" not in agent_script
    assert "DRKAI" not in agent_script


# ---------------------------------------------------------------------------
# Deploy engine: access phase + agent pools carry the managed identity
# ---------------------------------------------------------------------------


def _synthetic_prod_config() -> cfgmod.EnvironmentConfig:
    """A prod config with DR + access + kv_sync + build_agents + the network
    plan entries the access phase derives its subnet ids from."""
    data: dict[str, Any] = {
        "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
        "environment": "prod",
        "location": "eastus2",
        "unique_suffix": "p01",
        "tags": {},
        "network": {
            "utility": {
                "vnet_name": "vnet-prod-utility",
                "vnet_resource_group_name": "rg-net",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
                "pe_subnet_name": "snet-pe",
            },
        },
        "em": {"vm_size": "Standard_D4s_v5"},
        "servers": {
            "prod": {
                "vm_size": "Standard_D4s_v5",
                "agents": {"vm_size": "Standard_D2s_v5", "min": 1, "max": 2, "default": 1},
                "network": {
                    "vnet_name": "vnet-prod",
                    "vnet_resource_group_name": "rg-net",
                    "server_subnet_name": "snet-server",
                    "agents_subnet_name": "snet-agents",
                },
            },
        },
        "database": {"sku_name": "GP_Standard_D2ds_v5"},
        "dr": {
            "enabled": True,
            "location": "centralus",
            "network": {
                "utility": {
                    "vnet_name": "vnet-dr-utility",
                    "vnet_resource_group_name": "rg-net-dr",
                    "em_subnet_name": "snet-em",
                    "data_subnet_name": "snet-data",
                },
            },
            "server_networks": {
                "prod": {
                    "vnet_name": "vnet-prod-dr",
                    "vnet_resource_group_name": "rg-net-dr",
                    "server_subnet_name": "snet-server",
                },
            },
        },
        "network_infrastructure": {
            "create": False,  # inert plan; the access helpers still read it
            "vnets": [
                {
                    "name": "vnet-prod-utility",
                    "resource_group_name": "rg-net",
                    "location": "eastus2",
                    "role": "utility",
                    "peer_to": ["vnet-dr-utility"],
                    "address_space": "10.70.0.0/22",
                    "subnets": [
                        {"name": "snet-em", "prefix": "10.70.0.0/24", "role": "em"},
                        {"name": "snet-data", "prefix": "10.70.1.0/27", "role": "data"},
                        {"name": "snet-pe", "prefix": "10.70.1.64/26", "role": "pe"},
                        {"name": "AzureBastionSubnet", "prefix": "10.70.2.0/26", "role": "bastion"},
                        {"name": "snet-appgw", "prefix": "10.70.2.64/26", "role": "appgw"},
                        {"name": "snet-func", "prefix": "10.70.2.128/26", "role": "func"},
                    ],
                },
                {
                    "name": "vnet-build",
                    "resource_group_name": "rg-net",
                    "location": "eastus2",
                    "role": "build",
                    "peer_to": ["vnet-dev-utility", "vnet-prod-utility", "vnet-dr-utility"],
                    "address_space": "10.90.0.0/24",
                    "subnets": [
                        {"name": "snet-build", "prefix": "10.90.0.0/26", "role": "build"},
                    ],
                },
            ],
        },
        "access": {"bastion": {"enabled": True}, "appgw": {"enabled": True}},
        "kv_sync": {"enabled": True, "schedule": "0 */15 * * * *"},
        "build_agents": {
            "enabled": True,
            "resource_group": "ctm-build-rg",
            "vm_size": "Standard_D4s_v5",
            "instances": 2,
            "network": {
                "vnet_name": "vnet-build",
                "vnet_resource_group_name": "rg-net",
                "subnet_name": "snet-build",
            },
        },
    }
    return _load_dict(data)


def _foundation_outputs(prefix: str) -> dict[str, Any]:
    return {
        "keyVaultName": f"{prefix}-kv",
        "identityClientId": f"{prefix}-client-id",
        "blobAccountName": f"{prefix}blob",
        "nfsServer": f"{prefix}files.file.core.windows.net",
        "nfsExportPath": f"/{prefix}files/ctm-shared",
        "dnsZoneName": "prod.ctm.internal",
        "identityId": f"/subscriptions/x/.../{prefix}-identity",
        "dataCollectionRuleId": f"/{prefix}/dcr",
        "lbBackendPoolId": f"/{prefix}/lb/backend",
        "actionGroupId": f"/{prefix}/ag",
        "filesAccountId": f"/{prefix}/files",
        "shareName": "ctm-shared",
        "lbFrontendIp": {"primary": "10.70.0.10", "dr": "10.80.0.10"}[
            "dr" if prefix.endswith("dr") else "primary"
        ],
    }


def _engine_with_outputs(cfg):
    from ctmdeploy import deploy as deploymod

    engine = deploymod.Engine(cfg, REPO_ROOT)
    engine._outputs[f"{cfg.name}-foundation"] = _foundation_outputs("primary")
    engine._outputs[f"{cfg.name}-foundation-dr"] = _foundation_outputs("primarydr")
    engine._ssh_public_key = lambda: "ssh-rsa AAAATEST"  # type: ignore[method-assign]
    return engine


def test_access_phase_param_derivation_prod() -> None:
    from ctmdeploy.azure import SubscriptionScope

    cfg = _synthetic_prod_config()
    engine = _engine_with_outputs(cfg)
    captured: dict[str, Any] = {}

    def fake_deploy(scope, name, template, params, what_if):
        captured.update(scope=scope, name=name, template=template, params=params)
        return []

    engine._deploy = fake_deploy  # type: ignore[method-assign]
    engine.phase_access()

    assert captured["name"] == "ctm-prod-access"
    assert captured["template"] == "access.bicep"
    assert captured["scope"] == SubscriptionScope("eastus2")
    params = captured["params"]
    assert set(params) == {
        "name",
        "location",
        "platformRgName",
        "bastionEnabled",
        "bastionSubnetId",
        "appgwEnabled",
        "appgwSubnetId",
        "appgwBackendIps",
        "dnsZoneName",
        "kvSyncEnabled",
        "funcSubnetId",
        "keyVaultId",
        "keyVaultUri",
        "ctmApiBase",
        "syncSchedule",
        "buildAgents",
        "tags",
    }
    assert params["platformRgName"] == "ctm-prod-rg"
    assert params["bastionEnabled"] is True
    assert params["bastionSubnetId"].endswith(
        "/virtualNetworks/vnet-prod-utility/subnets/AzureBastionSubnet"
    )
    assert params["appgwEnabled"] is True
    assert params["appgwSubnetId"].endswith("/subnets/snet-appgw")
    # The gateway's one backend pool holds BOTH internal LB frontend IPs:
    # primary first, DR second (healthy-only routing = automatic failover).
    assert params["appgwBackendIps"] == ["10.70.0.10", "10.80.0.10"]
    assert params["dnsZoneName"] == "prod.ctm.internal"
    assert params["kvSyncEnabled"] is True
    assert params["funcSubnetId"].endswith("/subnets/snet-func")
    assert params["keyVaultId"].endswith("/vaults/ctm-prod-kv-p01")
    assert params["keyVaultUri"] == "https://ctm-prod-kv-p01.vault.azure.net/"
    assert params["ctmApiBase"] == "https://ctm-web.prod.ctm.internal:8443/automation-api"
    assert params["syncSchedule"] == "0 */15 * * * *"
    build = params["buildAgents"]
    assert build["enabled"] is True
    assert build["resourceGroupName"] == "ctm-build-rg"
    assert build["vmSize"] == "Standard_D4s_v5" and build["instances"] == 2
    assert build["subnetId"].endswith("/virtualNetworks/vnet-build/subnets/snet-build")
    assert build["sshPublicKey"] == "ssh-rsa AAAATEST"
    assert params["tags"]["role"] == "access"


def test_access_phase_nonprod_is_bastion_and_kvsync_only() -> None:
    """appgw is honored on prod only; nonprod gets Bastion (+ kvsync)."""
    raw = yaml.safe_load(
        yaml.safe_dump(
            {
                "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
                "environment": "nonprod",
                "location": "eastus2",
                "unique_suffix": "n01",
                "tags": {},
                "network": {
                    "utility": {
                        "vnet_name": "vnet-util",
                        "vnet_resource_group_name": "rg-net",
                        "em_subnet_name": "snet-em",
                        "data_subnet_name": "snet-data",
                    },
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
                "network_infrastructure": {
                    "create": False,
                    "vnets": [
                        {
                            "name": "vnet-util",
                            "resource_group_name": "rg-net",
                            "location": "eastus2",
                            "role": "utility",
                            "address_space": "10.60.0.0/22",
                            "subnets": [
                                {"name": "snet-em", "prefix": "10.60.0.0/24", "role": "em"},
                                {"name": "snet-data", "prefix": "10.60.1.0/27", "role": "data"},
                                {
                                    "name": "AzureBastionSubnet",
                                    "prefix": "10.60.2.0/26",
                                    "role": "bastion",
                                },
                                {"name": "snet-func", "prefix": "10.60.2.64/26", "role": "func"},
                            ],
                        },
                    ],
                },
                "access": {"bastion": {"enabled": True}, "appgw": {"enabled": True}},
                "kv_sync": {"enabled": True},
            }
        )
    )
    cfg = _load_dict(raw)
    engine = _engine_with_outputs(cfg)
    captured: dict[str, Any] = {}

    def fake_deploy(scope, name, template, params, what_if):
        captured.update(params=params)
        return []

    engine._deploy = fake_deploy  # type: ignore[method-assign]
    engine.phase_access()
    params = captured["params"]
    assert params["bastionEnabled"] is True
    assert params["appgwEnabled"] is False  # honored on prod only
    assert params["appgwBackendIps"] == [] and params["appgwSubnetId"] == ""
    assert params["kvSyncEnabled"] is True
    assert params["buildAgents"]["enabled"] is False
    assert params["buildAgents"]["sshPublicKey"] == ""  # nothing resolved


def test_access_phase_skips_when_everything_disabled() -> None:
    data = {
        "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
        "environment": "nonprod",
        "location": "eastus2",
        "unique_suffix": "n01",
        "tags": {},
        "network": {
            "utility": {
                "vnet_name": "vnet-util",
                "vnet_resource_group_name": "rg-net",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
            },
        },
        "em": {"vm_size": "Standard_D4s_v5"},
        "servers": {
            "dev": {
                "vm_size": "Standard_D4s_v5",
                "agents": {"vm_size": "Standard_D2s_v5", "min": 1, "max": 2, "default": 1},
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
    cfg = _load_dict(data)
    from ctmdeploy import deploy as deploymod

    engine = deploymod.Engine(cfg, REPO_ROOT)
    results, has_changes = engine.run("access")
    assert results == [] and has_changes is False


def test_phase_order_puts_access_between_compute_and_dr() -> None:
    from ctmdeploy import deploy as deploymod

    phases = list(deploymod.PHASES)
    assert phases.index("access") == phases.index("compute") + 1
    assert phases.index("dr") == phases.index("access") + 1


def test_agent_pools_carry_identity_and_boot_from_the_agent_image() -> None:
    """v4.1: the agent VMSS (every pool) still rides the shared UAMI for blob
    access — instances fetch scripts/ctm_node_install.py at boot — and the
    compute params resolve TWO image ids: imageId from image.base (EM/Server
    VMs) and agentImageId from image.agent (the ctm-agent image with the
    agent baked in). The agent node.json carries NO media blobs and no
    secrets anywhere."""
    cfg = _synthetic_prod_config()
    engine = _engine_with_outputs(cfg)
    engine._existing_compute = lambda rg: (set(), set())  # type: ignore[method-assign]
    params = engine._compute_params("primary")
    assert params["identityId"] == "/subscriptions/x/.../primary-identity"
    # Two-image model: nodePlan boots base, agentPools boot the agent image.
    assert params["imageId"] == cfg.image_id("base")
    assert params["imageId"].endswith("/images/ctm-rhel9/versions/latest")
    assert params["agentImageId"] == cfg.image_id("agent")
    assert params["agentImageId"].endswith("/images/ctm-agent/versions/latest")
    pools = params["agentPools"]
    assert len(pools) == 1
    custom = pools[0]["customData"]
    assert '"identity_client_id": "primary-client-id"' in custom
    # VMSS blob access remains for the node-script fetch (fail-fast on 403
    # lives in the shim's curl -f + the installer's blob_forbidden_hint).
    assert "scripts/ctm_node_install.py" in custom
    # ...but the media blobs are GONE: the agent is baked into the image.
    assert "agent_installer_blob" not in custom
    assert "agent_silent_blob" not in custom
    assert "DRKAI" not in custom
    # Agent customData never mentions a password at all (secrets live in KV).
    assert "password" not in custom.lower()
    node = json.loads(_NODEJSON_RE.search(custom).group(1))
    assert INSTALLER.validate_config(node) == "agent"
    assert set(node) == set(INSTALLER.required_config_keys("agent"))
    # EM/Server nodes also carry only names/ids in their node.json.
    for item in params["nodePlan"]:
        node = json.loads(_NODEJSON_RE.search(item["customData"]).group(1))
        for key in node:
            assert "password" not in key.lower(), (item["name"], key)


# ---------------------------------------------------------------------------
# CLI: `image build --image agent` media validation (pure, no packer)
# ---------------------------------------------------------------------------


def test_cli_validate_agent_media() -> None:
    from ctmdeploy import cli

    assert cli.PACKER_TEMPLATES == {"base": "ctm-rhel9.pkr.hcl", "agent": "ctm-agent.pkr.hcl"}
    for name in cli.PACKER_TEMPLATES.values():
        assert (REPO_ROOT / "packer" / name).is_file(), name

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Missing archive -> clear error naming the package.
        try:
            cli.validate_agent_media(root)
        except cfgmod.ConfigError as exc:
            assert "DRKAI" in str(exc) and "control-m-agent" in str(exc)
        else:
            raise AssertionError("empty media dir was accepted")
        (root / "DRKAI.9.0.21.300_Linux-x86_64.tar.Z").write_bytes(b"media")
        # Archive but no answer file -> clear error naming silent-agent.xml.
        try:
            cli.validate_agent_media(root)
        except cfgmod.ConfigError as exc:
            assert "silent-agent" in str(exc)
        else:
            raise AssertionError("media dir without silent-agent.xml was accepted")
        (root / "silent-agent.xml").write_text("<x/>", encoding="utf-8")
        assert cli.validate_agent_media(root) == root
        # Not a directory at all.
        try:
            cli.validate_agent_media(root / "nope")
        except cfgmod.ConfigError as exc:
            assert "not a directory" in str(exc)
        else:
            raise AssertionError("missing dir was accepted")


# ---------------------------------------------------------------------------
# kvsync packaging helpers (no Azure, no network)
# ---------------------------------------------------------------------------


def test_kvsync_zip_and_names() -> None:
    cfg = _synthetic_prod_config()
    assert cfg.kvsync_function_app_name == "ctm-prod-kvsync"
    assert kvsync.zipdeploy_url("ctm-prod-kvsync") == (
        "https://ctm-prod-kvsync.scm.azurewebsites.net/api/zipdeploy?isAsync=false"
    )
    source = REPO_ROOT / "functions" / "kvsync"
    if not source.is_dir():
        _skip(f"{source} does not exist yet")
    payload = kvsync.zip_source(source)
    import io
    import zipfile

    names = set(zipfile.ZipFile(io.BytesIO(payload)).namelist())
    assert {"host.json", "function_app.py", "requirements.txt"} <= names
    host = json.loads((source / "host.json").read_text(encoding="utf-8"))
    assert host["extensions"]["durableTask"]["hubName"] == "ctmkvsync"

    try:
        kvsync.zip_source(source / "does-not-exist")
    except kvsync.KvSyncError:
        pass
    else:
        raise AssertionError("missing source dir was accepted")


def test_kvsync_zip_never_ships_local_settings() -> None:
    """local.settings.json (and other .funcignore entries) must never reach
    the Function App: it can hold real connection strings from local runs."""
    import io
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "kvsync"
        src.mkdir()
        for name in ("host.json", "function_app.py", "requirements.txt"):
            (src / name).write_text("{}\n", encoding="utf-8")
        (src / "README.md").write_text("docs\n", encoding="utf-8")
        (src / "local.settings.json").write_text(
            '{"Values": {"AzureWebJobsStorage": "NOT-A-REAL-SECRET"}}', encoding="utf-8"
        )
        (src / "stray.pyc").write_bytes(b"\x00")
        for directory in (".vscode", ".pytest_cache", ".git", "__pycache__", "tests", ".venv"):
            (src / directory).mkdir()
            (src / directory / "x.json").write_text("{}\n", encoding="utf-8")
        names = set(zipfile.ZipFile(io.BytesIO(kvsync.zip_source(src))).namelist())
        assert names == {"host.json", "function_app.py", "requirements.txt", "README.md"}


def test_kvsync_deploy_requires_enabled() -> None:
    assert _synthetic_prod_config().kv_sync.enabled is True
    cfg_data = {
        "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
        "environment": "nonprod",
        "location": "eastus2",
        "unique_suffix": "n01",
        "tags": {},
        "network": {
            "utility": {
                "vnet_name": "vnet-util",
                "vnet_resource_group_name": "rg-net",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
            },
        },
        "em": {"vm_size": "Standard_D4s_v5"},
        "servers": {
            "dev": {
                "vm_size": "Standard_D4s_v5",
                "agents": {"vm_size": "Standard_D2s_v5", "min": 1, "max": 2, "default": 1},
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
    cfg = _load_dict(cfg_data)
    try:
        kvsync.deploy(cfg, REPO_ROOT)
    except kvsync.KvSyncError as exc:
        assert "kv_sync.enabled" in str(exc)
    else:
        raise AssertionError("kvsync deploy ran with kv_sync disabled")


# ---------------------------------------------------------------------------
# media: v4.1 — NO agent blob mapping + node-script constant agreement
# ---------------------------------------------------------------------------


def test_media_agent_mapping_is_gone_and_node_script_constant_agrees() -> None:
    """v4.1: agent media never maps to a platform blob — it feeds the Packer
    ctm-agent build. The node-script blob constant stays in lock-step."""
    from ctmdeploy import media

    assert media.blob_name_for("DRKAI.9.0.21.300_Linux-x86_64.tar.Z") is None
    assert media.blob_name_for("silent-agent.xml") is None
    assert media.blob_name_for("silent-agent-custom.xml") is None
    assert media.blob_name_for("Agent.Linux.tar.gz") is None
    assert media.blob_name_for("agent/nested/file.bin") is None  # no verbatim agent/ tree
    # EM/Server mappings are untouched.
    assert media.blob_name_for("silent_xml/silent-em-primary.xml") == "em/silent-em-primary.xml"
    # The constants that keep deploy.py, media.py and the shims in lock-step.
    assert media.NODE_SCRIPT_BLOB == render.NODE_SCRIPT_BLOB == "scripts/ctm_node_install.py"
    assert (REPO_ROOT / media.NODE_SCRIPT_SOURCE).is_file()


def test_installer_blobs_have_no_agent_entries() -> None:
    """The installers container holds EM/Server media only (six keys), and
    the old agent overrides are rejected with a pointer to the image model."""
    assert set(cfgmod.DEFAULT_INSTALLER_BLOBS) == {
        "em_installer",
        "server_installer",
        "em_silent_primary",
        "em_silent_secondary",
        "server_silent_primary",
        "server_silent_secondary",
    }
    cfg = _synthetic_prod_config()
    assert not hasattr(cfg.installer_blobs, "agent_installer")
    assert not hasattr(cfg.installer_blobs, "agent_silent")

    data: dict[str, Any] = yaml.safe_load(
        yaml.safe_dump(
            {
                "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
                "environment": "nonprod",
                "location": "eastus2",
                "unique_suffix": "n01",
                "tags": {},
                "network": {
                    "utility": {
                        "vnet_name": "vnet-util",
                        "vnet_resource_group_name": "rg-net",
                        "em_subnet_name": "snet-em",
                        "data_subnet_name": "snet-data",
                    },
                },
                "em": {"vm_size": "Standard_D4s_v5"},
                "servers": {
                    "dev": {
                        "vm_size": "Standard_D4s_v5",
                        "agents": {"vm_size": "Standard_D2s_v5", "min": 1, "max": 2, "default": 1},
                        "network": {
                            "vnet_name": "vnet-dev",
                            "vnet_resource_group_name": "rg-net",
                            "server_subnet_name": "snet-server",
                        },
                    },
                },
                "database": {"sku_name": "GP_Standard_D2ds_v5"},
                "dr": {"enabled": False},
                "installer_blobs": {"agent_installer": "agent/DRKAI.tar.Z"},
            }
        )
    )
    try:
        _load_dict(data)
    except cfgmod.ConfigError as exc:
        assert "agent_installer" in str(exc) and "ctm-agent" in str(exc)
    else:
        raise AssertionError("an agent_installer override was accepted")


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
