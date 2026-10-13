# Azure DevOps pipeline

`azure-pipelines.yml` deploys every environment from one definition:

```
validate ──► security (Checkmarx One) ──► plan dev ──► apply dev ──► apply qa ──► apply uat ──► apply prod
                                      ├─► plan qa  ───────┘            ▲             ▲             ▲
                                      ├─► plan uat ────────────────────┘             │             │
                                      └─► plan prod ─────────────────────────────────┴─────────────┘
```

* **Pull requests** run validate, Checkmarx and a plan for every environment
  (the plan text and JSON are published as artifacts).
* **main** additionally applies, in promotion order. Each apply stage is a
  `deployment` job bound to an Azure DevOps **environment** (`ctm-dev`,
  `ctm-qa`, `ctm-uat`, `ctm-prod`); approvals, business-hours windows and the
  exclusive lock are configured on those environments, not in YAML.
* An apply is skipped when its plan shows no changes; the next environment
  still proceeds. A failed apply stops the promotion.
* The saved plan from the plan stage is what gets applied. Terraform rejects a
  stale plan if the state changed in between, so plan and apply always match.

## One-time setup

### 1. Azure: Workload Identity Federation service connection

1. Create an app registration / managed identity for the pipeline and grant it
   `Contributor` and `User Access Administrator` on the subscription (role
   assignments are created by Terraform) and `Storage Blob Data Contributor`
   on the state storage account.
2. Project settings → Service connections → New → Azure Resource Manager →
   **Workload Identity federation (automatic)**. Name it `sc-ctm-platform-wif`.
3. Copy the connection's resource id (GUID in the URL) into
   `pipelines/variables/common.yml` → `azureServiceConnectionId`.

The Terraform steps use the AzureRM provider's native Azure DevOps OIDC
support (`ARM_OIDC_REQUEST_TOKEN` / `ARM_OIDC_REQUEST_URL` /
`ARM_ADO_PIPELINE_SERVICE_CONNECTION_ID`), so tokens are minted on demand and
long applies never hit the short lifetime of a one-off `idToken`.

### 2. Delinea DevOps Secrets Vault

Create the secrets (one per environment plus the shared Checkmarx client):

```bash
dsv secret create --path control-m/dev/platform --data '{
  "db-admin-password": "...", "em-db-password": "...",
  "ctm-db-password": "...",   "em-admin-password": "..." }'
# repeat for qa, uat, prod (ssh-private-key is optional; Terraform generates one otherwise)

dsv secret create --path control-m/shared/checkmarx --data '{
  "client-id": "<Checkmarx One OAuth client>", "client-secret": "..." }'
```

Create a DSV **client** bound to a role whose policy allows `read` on
`secrets:control-m:<.*>`:

```bash
dsv role create --name ctm-azure-devops
dsv policy create --path secrets:control-m --actions read --subjects roles:ctm-azure-devops
dsv client create --role ctm-azure-devops   # prints clientId / clientSecret once
```

In Azure DevOps create the **variable group `ctm-dsv-auth`** (Pipelines →
Library) with `DSV_TENANT`, `DSV_CLIENT_ID` and `DSV_CLIENT_SECRET` (mark the
secret as secret) and authorize it for the pipeline. On self-hosted agents
that run with an Azure managed identity you can instead set
`dsvAuthType: azure` and register that identity in DSV
(`dsv auth-provider create --type azure ...`), leaving the variable group
empty.

How secrets flow: `steps-dsv-secrets.yml` installs the `dsv` CLI, reads the
environment's secret as JSON, filters it to the keys Terraform understands and
exposes it as the secret pipeline variable `platformSecretsJson`, which the
Terraform steps pass as `TF_VAR_platform_secrets`. In Terraform
(`environments/<env>/secrets.tf`) external values override generated ones and
land in the Key Vault of every region. Values never appear in logs
(`issecret=true`) and are not stored in the pipeline.

### 3. Checkmarx One

Two options, chosen by `checkmarxServiceConnection` in `common.yml`:

| Option | What to do |
|--------|-----------|
| **cx CLI + DSV (default)** | Create an OAuth client in Checkmarx One (Settings → Identity → OAuth clients) with the *ast-scanner* role, store it in DSV at `control-m/shared/checkmarx`, set `checkmarxTenant` and `checkmarxBaseUri` (region URL) in `common.yml`. |
| **Marketplace task** | Install the *Checkmarx AST* extension, create a *Checkmarx One* service connection, and put its name in `checkmarxServiceConnection`. |

Scan types default to `sast,sca,iac-security` (IaC Security covers Terraform
and the shell templates). Thresholds in `checkmarxThreshold` fail the stage;
pass `failOnCheckmarxThreshold=false` on a manual run to make the stage
advisory. SARIF and HTML summaries are published as the `checkmarx-results`
artifact on every run.

### 4. Environments and approvals

Pipelines → Environments → create `ctm-dev`, `ctm-qa`, `ctm-uat`, `ctm-prod`.
Recommended checks:

| Environment | Approvals | Other checks |
|-------------|-----------|--------------|
| ctm-dev | none | exclusive lock |
| ctm-qa | 1 platform engineer | exclusive lock |
| ctm-uat | 1 platform engineer + 1 QA lead | exclusive lock, business hours |
| ctm-prod | 2 approvers (change advisory) | exclusive lock, business hours, required template `azure-pipelines.yml` |

Branch policy on `main`: require the pipeline (build validation), at least one
reviewer, and linked work items if you use them.

### 5. Backend configuration

Run `bootstrap/` once (locally or from a pipeline run with
`terraform -chdir=bootstrap apply`) and put the state storage account name in
every `environments/<env>/backend.hcl`. The pipeline identity authenticates to
the backend with Azure AD (`use_azuread_auth = true`).

## Running

* Push to a feature branch and open a PR → validate, Checkmarx, four plans.
* Merge to `main` → dev applies automatically (no approvals on `ctm-dev`),
  then qa, uat and prod wait for their approvers.
* Manual run with the **applyEnvironments** parameter limited to e.g.
  `[dev, qa]` to stop promotion early; **runCheckmarx=false** skips the scan
  (plans still run, applies still require main).

## Files

| Path | Purpose |
|------|---------|
| `azure-pipelines.yml` | entry point, parameters, stage wiring |
| `pipelines/variables/common.yml` | tool versions, service connection, DSV and Checkmarx settings |
| `pipelines/templates/stage-validate.yml` | fmt, validate, tflint, shell syntax |
| `pipelines/templates/stage-security.yml` | Checkmarx One (task or CLI) |
| `pipelines/templates/stage-plan.yml` | DSV secrets + plan + artifact |
| `pipelines/templates/stage-apply.yml` | gated deployment job applying the saved plan |
| `pipelines/templates/steps-dsv-secrets.yml` | Delinea DSV CLI install + secret read |
| `pipelines/templates/steps-terraform-run.yml` | Azure OIDC auth, init, plan/apply |
| `pipelines/templates/steps-terraform-install.yml` | pinned Terraform install with checksum |
