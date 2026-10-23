# Azure DevOps pipeline

`azure-pipelines.yml` deploys both platforms from one definition:

```
validate ──► security (Checkmarx One) ──► whatif nonprod ──► apply nonprod (CTM_DEV, CTM_QA) ──► apply prod (CTM_UAT, CTM_PROD)
                                      └─► whatif prod ──────────────────────────────────────────────┘
image (manual, runImageBuild=true) ──► Packer publishes a new ctm-rhel9 gallery version
```

* **Pull requests** run validate (bicep build/lint, pytest, shell syntax),
  Checkmarx and an ARM **what-if** for both platforms (the change summary text
  and JSON are published as the `whatif-<env>` artifacts).
* **main** additionally applies nonprod, then prod
  (`python -m ctmdeploy deploy --env <env> --phase all --yes --wait`). Each
  apply stage is a `deployment` job bound to an Azure DevOps **environment**
  (`ctm-nonprod`, `ctm-prod`); approvals, business-hours windows and the
  exclusive lock are configured on those environments, not in YAML.
* An apply is skipped when its what-if shows no changes; prod still proceeds.
  A failed nonprod apply stops the promotion.
* **ARM has no "apply this exact saved plan"**: deployments are stateless, so
  the apply stage re-runs the same deployments the what-if stage analyzed.
  Approvers review the `whatif-<env>` artifact before approving; a drift
  between what-if and apply can only come from changes made in Azure in
  between (the apply is still incremental and idempotent).
* Stage-level changes (a new `CTM_*` Server, agent sizing) are
  `environments/<platform>/config.yaml` changes to the owning platform;
  job-definition promotion dev → qa → uat → prod is an Automation API concern
  (`ctm deploy`), not this pipeline.
* A manual run with `runImageBuild=true` and an `imageVersion` adds the
  **image** stage: it installs a pinned, checksum-verified Packer, ensures the
  gallery exists (`ctmdeploy image gallery`) and publishes the version
  (`ctmdeploy image build`). Gallery versions are immutable — always bump.

## One-time setup

### 1. Azure: Workload Identity Federation service connection

1. Create an app registration / managed identity for the pipeline and grant it
   `Contributor` and `User Access Administrator` on the subscription (role
   assignments are created by the foundation deployment).
2. Project settings → Service connections → New → Azure Resource Manager →
   **Workload Identity federation (automatic)**. Name it `sc-ctm-platform-wif`.
3. Copy the connection's resource id (GUID in the URL) into
   `pipelines/variables/common.yml` → `azureServiceConnectionId`.

How the token flow works: every `ctmdeploy` command runs **inside an
`AzureCLI@2` task** with `addSpnToEnvironment: true`. The task performs the
federated az login, and `DefaultAzureCredential` in the python code resolves
through that az session — tokens are minted on demand, no SPN secret exists
anywhere. The task exports `ARM_CLIENT_ID`/`ARM_TENANT_ID`/
`ARM_SUBSCRIPTION_ID` (and `AZURE_*` twins) for anything else that needs them,
and resolves the service principal's **object id** into
`CTM_DEPLOYER_OBJECT_ID`, which the foundation deployment uses to grant the
pipeline **Key Vault Secrets Officer** so the secrets phase can seed the
vaults. If the connection's identity cannot read its own SPN in Graph, grant
that role assignment out-of-band instead and set the variable yourself.

### 2. Delinea DevOps Secrets Vault

Create the secrets (one per environment plus the shared Checkmarx client):

```bash
dsv secret create --path control-m/nonprod/platform --data '{
  "db-admin-password": "...", "em-db-password": "...", "em-admin-password": "...",
  "ctm-db-password-dev": "...", "ctm-db-password-qa": "..." }'
dsv secret create --path control-m/prod/platform --data '{
  "db-admin-password": "...", "em-db-password": "...", "em-admin-password": "...",
  "ctm-db-password-uat": "...", "ctm-db-password-prod": "..." }'
# ssh-private-key is optional; ctmdeploy generates one otherwise

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
platform's secret as JSON, filters it to the allow-listed keys and exposes it
as the secret pipeline variable `platformSecretsJson`, which the what-if and
apply steps pass to `ctmdeploy` as the **`CTM_PLATFORM_SECRETS`** environment
variable (JSON map). During the secrets phase, external values override
generated ones and land in the Key Vault of every region; secrets already in
the vault are never rotated unless explicitly overridden. Values never appear
in logs (`issecret=true`) and are not stored in the pipeline.

### 3. Checkmarx One

Two options, chosen by `checkmarxServiceConnection` in `common.yml`:

| Option | What to do |
|--------|-----------|
| **cx CLI + DSV (default)** | Create an OAuth client in Checkmarx One (Settings → Identity → OAuth clients) with the *ast-scanner* role, store it in DSV at `control-m/shared/checkmarx`, set `checkmarxTenant` and `checkmarxBaseUri` (region URL) in `common.yml`. |
| **Marketplace task** | Install the *Checkmarx AST* extension, create a *Checkmarx One* service connection, and put its name in `checkmarxServiceConnection`. |

Scan types default to `sast,sca,iac-security` (IaC Security covers the Bicep
templates, the pipelines and the shell/cloud-init scripts; SAST/SCA cover the
python orchestrator). Thresholds in `checkmarxThreshold` fail the stage; pass
`failOnCheckmarxThreshold=false` on a manual run to make the stage advisory.
SARIF and HTML summaries are published as the `checkmarx-results` artifact on
every run.

### 4. Environments and approvals

Pipelines → Environments → create `ctm-nonprod` and `ctm-prod`.
Recommended checks:

| Environment | Approvals | Other checks |
|-------------|-----------|--------------|
| ctm-nonprod | none (or 1 platform engineer) | exclusive lock |
| ctm-prod | 2 approvers (change advisory; it hosts UAT and PROD) | exclusive lock, business hours, required template `azure-pipelines.yml` |

Branch policy on `main`: require the pipeline (build validation), at least one
reviewer, and linked work items if you use them.

### 5. No state backend

There is nothing else to set up. Bicep deployments are stateless ARM calls —
no state storage account, no state locks, no backend configuration, no
bootstrap step. The what-if stage is the review surface; the deployment
history itself lives in Azure (`az deployment sub/group list`, deployment
names `ctm-<env>-<phase>[-dr]`).

## Running

* Push to a feature branch and open a PR → validate, Checkmarx, two what-ifs.
* Merge to `main` → nonprod applies (no approvals on `ctm-nonprod`), then
  prod waits for its approvers.
* Manual run with the **applyEnvironments** parameter limited to `[nonprod]`
  to stop promotion early; **runCheckmarx=false** skips the scan (what-ifs
  still run, applies still require main).
* Manual run with **runImageBuild=true** + **imageVersion** to publish a new
  golden image (then bump `image.image_version` in config via PR).

## Files

| Path | Purpose |
|------|---------|
| `azure-pipelines.yml` | entry point, parameters, stage wiring |
| `pipelines/variables/common.yml` | tool pins (bicep sha256, packer, python), service connection, DSV and Checkmarx settings |
| `pipelines/templates/stage-validate.yml` | bicep build+lint, compileall, pytest, config load, shell syntax |
| `pipelines/templates/stage-security.yml` | Checkmarx One (task or CLI) |
| `pipelines/templates/stage-whatif.yml` | DSV secrets + `ctmdeploy whatif` + artifact + hasChanges gate |
| `pipelines/templates/stage-apply.yml` | gated deployment job running `ctmdeploy deploy --phase all` |
| `pipelines/templates/stage-image.yml` | pinned Packer + `ctmdeploy image gallery/build` |
| `pipelines/templates/steps-dsv-secrets.yml` | Delinea DSV CLI install + secret read |
| `pipelines/templates/steps-tools-install.yml` | pinned Bicep (sha256 verified) + `pip install -e deploy/` |
