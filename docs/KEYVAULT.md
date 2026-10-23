# Key Vault contents and handling

One RBAC-mode Key Vault per platform region holds every platform credential.
Nothing else ever stores a secret: not git, not `config.yaml`, not the golden
image, not cloud-init/`customData`, not a state file (none exists), not the
deployment logs.

## The vaults

| Vault | Platform / region | Purge protection | Seeded by |
|-------|-------------------|------------------|-----------|
| `ctm-nonprod-kv-n01` | nonprod, primary (eastus2) | no | `ctmdeploy deploy --env nonprod --phase secrets` |
| `ctm-prod-kv-p01` | prod, primary (eastus2) | **yes** (prod only; cannot be disabled once on) | `ctmdeploy deploy --env prod --phase secrets` |
| `ctm-prod-dr-kv-p01` | prod, DR (centralus) | **yes** | same run — both regions are seeded with **identical values**, so a failed-over node resolves the same secrets |

Names derive from `ctm-<env>[-dr]-kv-<unique_suffix>`; change
`unique_suffix` in `environments/<env>/config.yaml` on a vault-name
collision. All three vaults share the same posture:

* **RBAC authorization** (no access policies), soft delete 30 days.
* **Network**: firewall `defaultAction: Deny` with `bypass: AzureServices`;
  only `deployer_ip_cidrs` pass the public endpoint, and
  `key_vault_public_access: false` closes the public endpoint entirely (then
  seed from inside the VNets). Nodes never use the public endpoint: a
  **private endpoint** (`ctm-<env>[-dr]-kv-pe`) in the utility pe subnet plus
  the `privatelink.vaultcore.azure.net` zone serves all VM traffic.
* **RBAC model** — exactly two data-plane grants, both created by the
  foundation deployment with deterministic GUID names:
  * **Key Vault Secrets Officer** → the *deployer* principal
    (`--deployer-object-id` / `CTM_DEPLOYER_OBJECT_ID`; the pipeline resolves
    its WIF service connection's object id automatically) — lets `ctmdeploy`
    seed and update secrets.
  * **Key Vault Secrets User** → the *node* user-assigned identity
    (`ctm-<env>[-dr]-nodes-id`) — read-only; every VM and VMSS instance
    fetches secrets at boot with an IMDS token for this identity.

## Secret inventory

Seeding is **create-if-absent**: `ctmdeploy` resolves each value as *explicit
override → value already in a region vault (primary read first, so regions
stay identical) → freshly generated*, then writes only what is missing. An
existing secret is **never rotated silently**; only an explicit
`CTM_PLATFORM_SECRETS` override that differs from the stored value updates
it. Overrides arrive as a JSON map in the `CTM_PLATFORM_SECRETS` environment
variable — the pipeline fills it from **Delinea DSV**
(`control-m/<env>/platform`, see `docs/AZURE-DEVOPS.md` §2), and unknown keys
are rejected. Generated passwords use only XML/sed-safe characters
(`A-Za-z0-9!#%^*-_=+`).

| Secret name | Created by | Consumed by (who, when) | Rotation (details: `docs/OPERATIONS.md`) | Never appears in |
|---|---|---|---|---|
| `db-admin-password` (32 chars) | `ctmdeploy` seed, or DSV override | the **foundation deployment** as the `@secure()` parameter `databaseAdminPassword` (PostgreSQL admin login, the one secret that crosses an ARM deployment boundary — never templated or logged); **EM nodes** and **Server nodes** at install time only (`kv_secret` in cloud-init, admin connection to create/own their databases) | seed the new value into **both** region vaults (override or `az keyvault secret set`), `ALTER ROLE ctmadmin` in PostgreSQL, re-run `--phase foundation` so ARM holds the same value | bicep files / parameter files, cloud-init text, rendered `customData`, logs, git, state (none exists) |
| `em-db-password` (32 chars) | `ctmdeploy` seed, or DSV override | **EM nodes** at install time (silent-install XML placeholder; the `emuser` database role) | vault(s) + `ALTER ROLE emuser` + EM password utilities, restart secondary EM | same |
| `em-admin-password` (24 chars) | `ctmdeploy` seed, or DSV override | **EM nodes** at install time (EM administrator account); **every agent VMSS instance at every first boot** — `ctm env add` login to the Automation API (`emuser`) for provisioning, host-group registration and scale-in deregistration | vault(s) + change in EM (`em` utilities); agents pick the new value up on their next instance creation (roll with `az vmss update-instances`) | same |
| `ctm-db-password-<stage>` (32 chars; `-dev`/`-qa` on nonprod, `-uat`/`-prod` on prod) | `ctmdeploy` seed, or DSV override | that stage's **Control-M/Server nodes** at install time (silent XML; the `ctm_<stage>` database role) | vault(s) + `ALTER ROLE ctm_<stage>` + `ctm_menu` password update on the primary node, restart the secondary | same |
| `ssh-private-key` (RSA-4096, PKCS#1 PEM) | `ctmdeploy` seed (generated), or DSV override | **`ctmdeploy` itself** during the compute phase: the OpenSSH **public** key is derived from it and passed to every VM/VMSS (`ctmops` admin user). The private key never reaches a node; operators fetch it from the vault to SSH in | seed a new PEM (override); the public key is fixed at deploy time, so redeploy the compute phase (or update `authorized_keys` manually) | same — only the derived *public* key appears in ARM parameters |

Per environment that is six secrets (nonprod: `…-dev`, `…-qa`; prod:
`…-uat`, `…-prod`), written to one vault on nonprod and to both the primary
and DR vaults on prod.

## How nodes read secrets

Cloud-init never carries a secret. Each node holds the vault **name** and the
identity **client id** only; at boot it obtains an IMDS token for
`https://vault.azure.net` and calls
`https://<vault>.vault.azure.net/secrets/<name>?api-version=7.4` (the
`kv_secret` helper in `cloud-init/common.sh.tmpl`). EM/Server nodes do this
once, inside the not-yet-installed branch — a rebuilt node that reattaches
its installed data disk reads nothing. Agent instances read
`em-admin-password` on every instance's first boot. All calls resolve through
the Key Vault private endpoint.

## Operational notes

* **Adding a stage** adds `ctm-db-password-<stage>` automatically on the next
  `--phase secrets` run.
* **DSV is the system of record when the pipeline owns secrets**: change the
  value there; the next run's `CTM_PLATFORM_SECRETS` override updates the
  vaults. Manual `az keyvault secret set` works too but must be done in
  *every* region vault, or the regions drift.
* **DR**: because both vaults hold identical values and the DR nodes use the
  DR vault's private endpoint, failover needs no secret handling at all.
* **Deleting prod vaults** requires removing the resource lock and waiting
  out soft delete; purge protection makes an early purge impossible by
  design.
