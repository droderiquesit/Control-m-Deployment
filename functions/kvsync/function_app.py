# ---------------------------------------------------------------------------
# Key Vault -> Control-M secret sync: an Azure Durable Function (python v2
# programming model) that mirrors tagged Key Vault secrets into Control-M's
# secret store through the EM Automation API.
#
# Flow (every %SYNC_SCHEDULE% tick):
#   timer -> orchestrator -> list_tagged_secrets (KV secrets tagged
#   ctm-sync=true; name override via the ctm-name tag; disabled secrets are
#   skipped) -> fan-out upsert_ctm_secret per secret, capped at
#   MAX_PARALLEL_UPSERTS concurrent activities (login with emuser + the
#   em-admin-password KV secret, `secret add`, fall back to update on
#   'already exists') -> report (a summary that NEVER contains values).
#
# Identity & settings (provisioned by bicep/modules/function-kvsync.bicep):
#   system-assigned MI with Key Vault Secrets User on the platform vault;
#   app settings KEY_VAULT_URI, CTM_API_BASE, SYNC_SCHEDULE, CTM_VERIFY_TLS.
#
# Secret values only ever live in activity memory; logs carry names/actions.
# ---------------------------------------------------------------------------
"""KV -> Control-M sync Durable Function (timer -> orchestrator -> fan-out)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.durable_functions as df
import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

#: KV tag that opts a secret into the sync.
SYNC_TAG = "ctm-sync"
#: KV tag that overrides the Control-M secret name (default: the KV name).
NAME_TAG = "ctm-name"
#: The EM Automation API account used for the sync (same as agent bootstrap).
EM_API_USER = "emuser"
#: KV secret holding that account's password (seeded by `ctmdeploy secrets`).
EM_PASSWORD_SECRET = "em-admin-password"
#: Fan-out concurrency cap: each upsert logs in to the EM Automation API, so
#: batches stay modest and a large vault never floods the EM web tier.
MAX_PARALLEL_UPSERTS = 4


def _verify_tls() -> bool:
    """TLS verification toward the EM API (app setting CTM_VERIFY_TLS).

    SECURITY NOTE: defaults to FALSE because EM ships a self-signed
    certificate; install a CA-signed certificate on the EM web tier and set
    CTM_VERIFY_TLS=true in production.
    """
    return os.environ.get("CTM_VERIFY_TLS", "false").strip().lower() == "true"


def _secret_client() -> SecretClient:
    vault_uri = os.environ["KEY_VAULT_URI"]
    return SecretClient(vault_url=vault_uri, credential=DefaultAzureCredential())


# ---------------------------------------------------------------------------
# Timer trigger -> orchestration
# ---------------------------------------------------------------------------


@app.timer_trigger(schedule="%SYNC_SCHEDULE%", arg_name="timer", run_on_startup=False)
@app.durable_client_input(client_name="client")
async def kvsync_timer(timer: func.TimerRequest, client) -> None:
    instance_id = await client.start_new("kvsync_orchestrator")
    logging.info("kvsync orchestration started: %s", instance_id)


@app.orchestration_trigger(context_name="context")
def kvsync_orchestrator(context: df.DurableOrchestrationContext):
    secrets: list[dict[str, str]] = yield context.call_activity("list_tagged_secrets", None)
    results: list[dict[str, str]] = []
    # Bounded fan-out: slice the activity result into batches of
    # MAX_PARALLEL_UPSERTS and await each batch before scheduling the next.
    # Plain list slicing keeps the orchestrator deterministic — every replay
    # schedules the same tasks in the same order.
    for start in range(0, len(secrets), MAX_PARALLEL_UPSERTS):
        batch = secrets[start : start + MAX_PARALLEL_UPSERTS]
        tasks = [context.call_activity("upsert_ctm_secret", item) for item in batch]
        batch_results = yield context.task_all(tasks)
        results.extend(batch_results or [])
    summary = yield context.call_activity("report", results)
    return summary


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@app.activity_trigger(input_name="payload")
def list_tagged_secrets(payload: Any) -> list[dict[str, str]]:
    """KV secrets tagged ctm-sync=true -> [{kv_name, ctm_name}] (names only)."""
    client = _secret_client()
    items: list[dict[str, str]] = []
    for prop in client.list_properties_of_secrets():
        if prop.enabled is False:
            continue  # disabled secrets are skipped by design
        tags = prop.tags or {}
        if str(tags.get(SYNC_TAG, "")).strip().lower() != "true":
            continue
        ctm_name = str(tags.get(NAME_TAG) or prop.name)
        items.append({"kv_name": prop.name, "ctm_name": ctm_name})
    logging.info("list_tagged_secrets: %d secret(s) tagged %s=true", len(items), SYNC_TAG)
    return items


def _em_login(session: requests.Session, api_base: str, verify: bool) -> str:
    client = _secret_client()
    password = client.get_secret(EM_PASSWORD_SECRET).value
    response = session.post(
        f"{api_base}/session/login",
        json={"username": EM_API_USER, "password": password},
        verify=verify,
        timeout=60,
    )
    response.raise_for_status()
    token = response.json().get("token", "")
    if not token:
        raise RuntimeError("EM Automation API login returned no token")
    return token


@app.activity_trigger(input_name="item")
def upsert_ctm_secret(item: dict[str, str]) -> dict[str, str]:
    """Add one KV secret to Control-M; fall back to update when it exists.

    Automation API endpoints (9.0.21): POST /run/secret adds a secret;
    POST /run/secret/{name} updates it. Validate against your release.
    The returned result carries names and actions only — never values.
    """
    kv_name, ctm_name = item["kv_name"], item["ctm_name"]
    verify = _verify_tls()
    api_base = os.environ["CTM_API_BASE"]
    try:
        value = _secret_client().get_secret(kv_name).value
        session = requests.Session()
        token = _em_login(session, api_base, verify)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = session.post(
                f"{api_base}/run/secret",
                json={"name": ctm_name, "value": value},
                headers=headers,
                verify=verify,
                timeout=60,
            )
            if response.ok:
                return {"name": ctm_name, "action": "added"}
            body = response.text or ""
            if response.status_code == 400 and "exist" in body.lower():
                update = session.post(
                    f"{api_base}/run/secret/{ctm_name}",
                    json={"value": value},
                    headers=headers,
                    verify=verify,
                    timeout=60,
                )
                update.raise_for_status()
                return {"name": ctm_name, "action": "updated"}
            response.raise_for_status()
            return {"name": ctm_name, "action": "added"}
        finally:
            try:
                session.post(
                    f"{api_base}/session/logout", headers=headers, verify=verify, timeout=30
                )
            except requests.RequestException:
                pass
    except Exception as exc:  # noqa: BLE001 - one failure must not sink the fan-out
        logging.error("upsert_ctm_secret %s failed: %s", ctm_name, type(exc).__name__)
        return {"name": ctm_name, "action": f"failed: {type(exc).__name__}"}


@app.activity_trigger(input_name="results")
def report(results: list[dict[str, str]]) -> str:
    """Log a per-run summary. Secret VALUES never appear here."""
    counts: dict[str, int] = {}
    for result in results or []:
        action = result.get("action", "unknown").split(":", 1)[0]
        counts[action] = counts.get(action, 0) + 1
    summary = json.dumps({"synced": len(results or []), "actions": counts})
    logging.info("kvsync run summary: %s", summary)
    for result in results or []:
        logging.info("kvsync secret %s: %s", result.get("name"), result.get("action"))
    return summary
