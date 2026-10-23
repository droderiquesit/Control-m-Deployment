# ---------------------------------------------------------------------------
# Pure-python tests for ctmdeploy.render: token sets per role, substitution
# with leftover-token detection, and (when cloud-init/ templates exist) full
# render() token completeness for all 4 templates (common + em/server/agent).
# Runnable via `python3 -m pytest` or plain `python3 deploy/tests/test_render.py`.
# ---------------------------------------------------------------------------
"""Cloud-init rendering tests; no Azure SDK required."""

from __future__ import annotations

import sys
import traceback
import unittest
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
TEMPLATE_DIR = REPO_ROOT / "cloud-init"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from ctmdeploy import render  # noqa: E402


def _skip(message: str) -> None:
    raise unittest.SkipTest(message)


# ---------------------------------------------------------------------------
# Token sets (MUST match the design contract exactly)
# ---------------------------------------------------------------------------


def test_common_token_set_matches_contract() -> None:
    assert render.COMMON_TOKENS == (
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
    )


def test_role_token_sets_match_contract() -> None:
    assert render.EM_TOKENS == (
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
    assert render.SERVER_TOKENS == (
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
    assert render.AGENT_TOKENS == (
        "AGENT_USER",
        "EM_API_ENDPOINT",
        "EM_API_USER",
        "CTM_SERVER_NAME",
        "HOSTGROUP",
        "AGENT_PORT",
        "PROVISION_IMAGE",
    )


# ---------------------------------------------------------------------------
# substitute(): replacement + leftover detection
# ---------------------------------------------------------------------------


def test_substitute_replaces_tokens() -> None:
    out = render.substitute("kv=__KEY_VAULT_NAME__ port=__DB_PORT__", {
        "KEY_VAULT_NAME": "ctm-prod-kv-p01",
        "DB_PORT": 5432,  # non-str values are coerced
    })
    assert out == "kv=ctm-prod-kv-p01 port=5432"


def test_substitute_rejects_leftover_tokens() -> None:
    try:
        render.substitute("echo __NOT_PROVIDED__", {"ROLE": "em-primary"})
    except render.RenderError as exc:
        assert "__NOT_PROVIDED__" in str(exc)
    else:
        raise AssertionError("leftover __TOKEN__ was not rejected")


def test_substitute_ignores_lowercase_and_partial_markers() -> None:
    text = "path=/ctm/__pycache__ x=${HOME} __not_a_token__"
    # __pycache__/__not_a_token__ are lowercase: not tokens, must pass through.
    assert render.substitute(text, {}) == text


def test_render_rejects_unknown_role() -> None:
    try:
        render.render("database", {})
    except render.RenderError as exc:
        assert "role" in str(exc)
    else:
        raise AssertionError("unknown role was accepted")


# ---------------------------------------------------------------------------
# Token builders: exactly the contract key sets
# ---------------------------------------------------------------------------


def _dummy_common(role: str = "em-primary") -> dict[str, str]:
    return render.common_tokens(
        role=role,
        environment="prod",
        key_vault_name="ctm-prod-kv-p01",
        identity_client_id="00000000-0000-0000-0000-000000000000",
        blob_account="ctmprodblobp01",
        nfs_server="ctmprodfilesp01.file.core.windows.net",
        nfs_export="/ctmprodfilesp01/ctm-shared",
        ctm_version="9.0.21.300",
        dns_zone="prod.ctm.internal",
        attach_data_disk=True,
    )


def _dummy_em() -> dict[str, str]:
    return render.em_tokens(
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


def _dummy_server() -> dict[str, str]:
    return render.server_tokens(
        server_role="primary",
        ctm_user="controlm",
        server_installer_blob="server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z",
        server_silent_blob="server/silent-server-primary.xml",
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


def _dummy_agent() -> dict[str, str]:
    return render.agent_tokens(
        agent_user="ctmagent",
        em_api_endpoint="https://ctm-web.prod.ctm.internal:8443/automation-api",
        em_api_user="emuser",
        ctm_server_name="CTM_PROD",
        hostgroup="prod-agents",
        agent_port=7006,
        provision_image="Agent.Linux",
    )


def test_token_builders_produce_contract_keys() -> None:
    assert set(_dummy_common()) == set(render.COMMON_TOKENS)
    assert set(_dummy_em()) == set(render.EM_TOKENS)
    assert set(_dummy_server()) == set(render.SERVER_TOKENS)
    assert set(_dummy_agent()) == set(render.AGENT_TOKENS)
    assert _dummy_common()["ATTACH_DATA_DISK"] == "true"
    assert _dummy_em()["DB_PORT"] == "5432"


# ---------------------------------------------------------------------------
# Real templates (skip gracefully until the packer-cloudinit agent lands them)
# ---------------------------------------------------------------------------

_ROLE_BUILDERS = {
    "em": _dummy_em,
    "server": _dummy_server,
    "agent": _dummy_agent,
}


def _require_templates() -> None:
    if not TEMPLATE_DIR.is_dir():
        _skip(f"{TEMPLATE_DIR} does not exist yet (written by the packer-cloudinit agent)")
    missing = [
        name
        for name in ("common.sh.tmpl", "em.sh.tmpl", "server.sh.tmpl", "agent.sh.tmpl")
        if not (TEMPLATE_DIR / name).is_file()
    ]
    if missing:
        _skip(f"cloud-init templates missing: {missing} (written by the packer-cloudinit agent)")


def test_templates_reference_only_contract_tokens() -> None:
    _require_templates()
    common_used = render.tokens_in((TEMPLATE_DIR / "common.sh.tmpl").read_text(encoding="utf-8"))
    assert common_used <= set(render.COMMON_TOKENS), sorted(
        common_used - set(render.COMMON_TOKENS)
    )
    for role in render.ROLES:
        used = render.tokens_in((TEMPLATE_DIR / f"{role}.sh.tmpl").read_text(encoding="utf-8"))
        allowed = render.expected_tokens(role)
        assert used <= allowed, f"{role}.sh.tmpl uses unknown tokens: {sorted(used - allowed)}"


def test_render_token_completeness_all_roles() -> None:
    """render() with the full builder token set leaves no __TOKEN__ behind."""
    _require_templates()
    for role, builder in _ROLE_BUILDERS.items():
        tokens = _dummy_common(role=f"{role}-primary") | builder()
        rendered = render.render(role, tokens, TEMPLATE_DIR)
        assert rendered.strip(), f"{role}: rendered script is empty"
        assert not render.tokens_in(rendered), f"{role}: leftover tokens survived"


def test_render_fails_on_incomplete_tokens() -> None:
    _require_templates()
    tokens = _dummy_common(role="em-primary")  # role tokens deliberately missing
    em_used = render.tokens_in((TEMPLATE_DIR / "em.sh.tmpl").read_text(encoding="utf-8"))
    if not (em_used - set(render.COMMON_TOKENS)):
        _skip("em.sh.tmpl uses no role-specific tokens; nothing to assert")
    try:
        render.render("em", tokens, TEMPLATE_DIR)
    except render.RenderError:
        pass
    else:
        raise AssertionError("render() accepted an incomplete token set")


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
