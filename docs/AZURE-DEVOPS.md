# Azure DevOps pipeline

`azure-pipelines.yml` deploys both platforms from one definition:

```
validate ──► security (Checkmarx One) ──► plan nonprod ──► apply nonprod (CTM_DEV, CTM_QA) ──► apply prod (CTM_UAT, CTM_PROD)
                                      └─► plan prod ───────────────────────────────────────────────┘
```

* **Pull requests** run validate, Checkmarx and a plan for both platforms
  (the plan text and JSON are published as artifacts).
* **main** additionally applies nonprod, then prod. Each apply stage is a
  `deployment` job bound to an Azure DevOps **environment** (`ctm-nonprod`,
  `ctm-prod`); approvals, business-hours windows and the exclusive lock are
  configured on those environments, not in YAML.
* An apply is skipped when its plan shows no changes; prod still proceeds. A
  failed nonprod apply stops the promotion.
* The saved plan from the plan stage is what gets applied. Terraform rejects a
  stale plan if the state changed in between, so plan and apply always match.
* Stage-level changes (a new `CTM_*` Server, agent sizing) are tfvars changes
  to the owning platform; job-definition promotion dev → qa → uat → prod is an
  Automation API concern (`ctm deploy`), not this pipeline.

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
dsv secret create --path control-m/nonprod/platform --data '{
  "db-admin-password": "...", "em-db-password": "...", "em-admin-password": "...",
  "ctm-db-password-dev": "...", "ctm-db-password-qa": "..." }'
dsv secret create --path control-m/prod/platform --data '{
  "db-admin-password": "...", "em-db-password": "...", "em-admin-password": "...",
  "ctm-db-password-uat": "...", "ctm-db-password-prod": "..." }'
# ssh-private-key is optional; Terraform generates one otherwise

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
platform's secret as JSON, filters it to the keys Terraform understands and
exposes it as the secret pipeline variable `platformSecretsJson`, which the
Terraform steps pass as `TF_VAR_platform_secrets`. In Terraform
(`environments/<platform>/secrets.tf`) external values override generated ones and
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

Pipelines → Environments → create `ctm-nonprod` and `ctm-prod`.
Recommended checks:

| Environment | Approvals | Other checks |
|-------------|-----------|--------------|
| ctm-nonprod | none (or 1 platform engineer) | exclusive lock |
| ctm-prod | 2 approvers (change advisory; it hosts UAT and PROD) | exclusive lock, business hours, required template `azure-pipelines.yml` |

Branch policy on `main`: require the pipeline (build validation), at least one
reviewer, and linked work items if you use them.

### 5. Backend configuration

Run `bootstrap/` once (locally or from a pipeline run with
`terraform -chdir=bootstrap apply`) and put the state storage account name in
every `environments/<platform>/backend.hcl`. The pipeline identity authenticates to
the backend with Azure AD (`use_azuread_auth = true`).

## Running

* Push to a feature branch and open a PR → validate, Checkmarx, two plans.
* Merge to `main` → nonprod applies (no approvals on `ctm-nonprod`), then
  prod waits for its approvers.
* Manual run with the **applyEnvironments** parameter limited to `[nonprod]`
  to stop promotion early; **runCheckmarx=false** skips the scan (plans still
  run, applies still require main).

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
