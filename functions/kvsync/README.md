# kvsync — Key Vault → Control-M secret sync (Durable Function)

Mirrors tagged Azure Key Vault secrets into Control-M's secret store through
the EM Automation API, so jobs reference `%%secret-name%%` values that are
managed (rotated, audited) in Key Vault only.

## How it works

Every `SYNC_SCHEDULE` tick (NCRONTAB, default `0 */15 * * * *` = every 15
minutes), a timer starts a durable orchestration:

1. **list_tagged_secrets** — lists the platform vault's secrets and keeps
   the enabled ones tagged `ctm-sync = true`.
2. **upsert_ctm_secret** — durable fan-out, one activity per secret, capped
   at `MAX_PARALLEL_UPSERTS` (4) concurrent activities so a large vault
   never floods the EM web tier with parallel logins. Each activity logs in
   to `CTM_API_BASE/session/login` as `emuser` (password read from the
   `em-admin-password` vault secret at run time), then `POST /run/secret`
   to add; on "already exists" it falls back to `POST /run/secret/<name>`
   (update). Endpoint paths follow the 9.0.21 Automation API — validate
   against your release.
3. **report** — logs a summary of adds/updates/failures. Secret VALUES are
   never logged anywhere; logs carry names, actions and counts only.

## Tagging convention

| Key Vault tag | Meaning                                                      |
|---------------|--------------------------------------------------------------|
| `ctm-sync`    | `true` opts the secret into the sync; anything else skips it |
| `ctm-name`    | optional Control-M secret name override (default: KV name)   |

Disabled Key Vault secrets are always skipped. Example:

```bash
az keyvault secret set --vault-name ctm-prod-kv-p01 --name sap-batch-password \
  --value '<value>' --tags ctm-sync=true ctm-name=SAP_BATCH_PASSWORD
```

## App settings

All of these are set by `bicep/modules/function-kvsync.bicep` (the `access`
phase); change them there, not in the portal.

| Setting           | Value                                                            |
|-------------------|------------------------------------------------------------------|
| `KEY_VAULT_URI`   | platform vault the sync reads, e.g. `https://ctm-prod-kv-p01.vault.azure.net/` |
| `CTM_API_BASE`    | EM Automation API base, e.g. `https://ctm-web.prod.ctm.internal:8443/automation-api` |
| `SYNC_SCHEDULE`   | NCRONTAB timer schedule, from `kv_sync.schedule` in `environments/<env>/config.yaml` |
| `CTM_VERIFY_TLS`  | `false` by default — see the certificate note below              |
| `AzureWebJobsStorage`, `WEBSITE_CONTENT*`, `FUNCTIONS_*` | Functions runtime + Durable Task hub (`ctmkvsync`) state |

## Who touches which secrets

| Identity | Key Vault role | Secrets touched |
|----------|----------------|-----------------|
| **kvsync Function App** system-assigned MI (`<name>-kvsync`, granted in `function-kvsync.bicep`) | Key Vault Secrets User (read-only) | READS `em-admin-password` (to log in as `emuser`) and READS every enabled secret tagged `ctm-sync=true`. It never writes to Key Vault. |
| **Deployer** identity (pipeline workload identity / the operator running `ctmdeploy`) | Key Vault Secrets Officer | SEEDS and rotates the platform secrets (`em-admin-password`, the DB passwords, …) via `ctmdeploy secrets`. Also needs `Microsoft.Web/sites/publish/action` on the Function App for `ctmdeploy kvsync deploy` (zipdeploy) — no data-plane secret access is involved in the deploy itself. |
| **Node UAMI** (`<name>-nodes-id`, shared by the EM/Server VMs and the agent VMSS) | Key Vault Secrets User (read-only) | READS the install-time secrets at boot (including `em-admin-password` for agent registration). This function never uses it. |
| **`emuser`** (Control-M/EM account, not an Azure identity) | none | RECEIVES the synced values over the Automation API into Control-M's own secret store. |

A synced value therefore exists in exactly two stores — Key Vault (the
source of truth) and Control-M's secret store (the destination) — and lives
only in activity memory in between.

## Infrastructure

Provisioned by the `access` phase (`bicep/modules/function-kvsync.bicep`,
called from `bicep/access.bicep` when `kv_sync.enabled` in
`environments/<env>/config.yaml`): an EP1 Linux Elastic Premium plan, a
python 3.11 Function App `<name>-kvsync` with a system-assigned managed
identity, VNet-integrated into the utility VNet's `snet-ctm-func` subnet
with `vnetRouteAllEnabled` so the Key Vault and EM API calls ride the
private network.

EP1 runs warm instances and bills accordingly; nonprod may set
`kv_sync.enabled: false` to save the cost.

## Deploy

```bash
ctmdeploy deploy --env prod --phase access   # Function App infrastructure
ctmdeploy kvsync deploy --env prod           # zip-deploy this folder
```

`ctmdeploy kvsync deploy --env <env>` zips this folder (minus the
`.funcignore`-style exclusions) and POSTs it to the app's Kudu
`zipdeploy` endpoint with a `DefaultAzureCredential` bearer token.

## Local testing

```bash
cd functions/kvsync
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cat > local.settings.json <<'JSON'
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "KEY_VAULT_URI": "https://ctm-nonprod-kv-n01.vault.azure.net/",
    "CTM_API_BASE": "https://ctm-web.nonprod.ctm.internal:8443/automation-api",
    "SYNC_SCHEDULE": "0 */15 * * * *",
    "CTM_VERIFY_TLS": "false"
  }
}
JSON
func start   # Azure Functions Core Tools v4
```

**Azurite note:** `UseDevelopmentStorage=true` points the Functions host —
and the Durable Task hub's queues/tables/blobs — at a local
[Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
emulator. Start it before `func start`:
`npm install -g azurite && azurite --silent`, or
`docker run -p 10000:10000 -p 10001:10001 -p 10002:10002 mcr.microsoft.com/azure-storage/azurite`.
`az login` supplies the identity `DefaultAzureCredential` uses against the
real vault; your account needs Key Vault Secrets User on it. Never commit
`local.settings.json` — `.funcignore`, `ctmdeploy kvsync deploy`'s zip
packaging and the repository root `.gitignore` all exclude it, but treat it
as secret material regardless.

## Certificate note

`CTM_VERIFY_TLS` defaults to `false` because Control-M/EM ships a
self-signed certificate — the same posture as the node bootstrap's
unverified-TLS registration calls. For production: install a CA-signed
certificate on the EM web tier and set `CTM_VERIFY_TLS=true` in
`bicep/modules/function-kvsync.bicep`.
