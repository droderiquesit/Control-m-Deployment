# Azure DevOps pipeline

`azure-pipelines.yml` deploys both platforms from one definition:

```
validate ──► security (Checkmarx One) ──► whatif nonprod ──► apply nonprod (CTM_DEV, CTM_QA) ──► apply prod (CTM_UAT, CTM_PROD)
                                      └─► whatif prod ──────────────────────────────────────────────┘
image (manual, runImageBuild=true + imageKind) ──► Packer publishes a gallery version:
                                       base  → ctm-rhel9 (OS only — no BMC software)
                                       agent → ctm-agent (downloads control-m-agent + silent XMLs
                                               └─► ctmdeploy image build --image agent --media-path …
                                               — the Control-M/Agent is BAKED in, unconfigured)
media (manual, publishMedia=true)  ──► Azure Artifacts feed (Universal Packages: EM + Server EPD + silent XMLs)
                                       └─► ctmdeploy media upload ──► installers blob container (primary + DR)
```

Every job runs on Microsoft-hosted `ubuntu-latest` by default; setting
`agentPoolName` in `pipelines/variables/common.yml` moves **all** stages onto
the self-hosted scale-set agent pool (`ctm-build-agents`, §6) inside the
build VNet — one pool deploys into nonprod, prod **and** DR because that VNet
peers to all three utility VNets. WIF provides the deployment identity either
way.

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
* A manual run with `runImageBuild=true`, an `imageVersion` and an
  `imageKind` adds the **image** stage: it installs a pinned,
  checksum-verified Packer, ensures the gallery (BOTH definitions) exists
  (`ctmdeploy image gallery`) and publishes the version (`ctmdeploy image
  build --image <kind>`). With `imageKind=agent` the stage first downloads
  the `control-m-agent` + `control-m-silent-xml` packages and hands them to
  the build via `--media-path` — the **agent is baked into the ctm-agent
  image here**, never uploaded to blob. Gallery versions are immutable —
  always bump. Rolling a new agent image = bump `image.agent.version` in
  config + a compute apply (VMSS model update; recycle instances to converge).
* The **EM/Server installer media** (EPD archives + silent-install XMLs) is
  stored as **Universal Packages in an Azure Artifacts feed**, never in git.
  A manual run on `main` with `publishMedia=true` adds the **media** stage,
  which downloads the three pinned EM/Server packages (em, server,
  silent_xml) and runs `ctmdeploy media upload` to push them into each
  platform's `installers` blob container (primary and, for prod, DR),
  together with the node installer script. The VMs are unchanged — EM/Server
  nodes pull the media from blob via managed identity at first boot (agent
  scale sets don't: their software is in the image). Setup and package
  layout: section 5 below.

## One-time setup

### 1. Azure: Workload Identity Federation service connection

1. Create an app registration / managed identity for the pipeline and grant
   it the **Control-M Platform Deployer** custom role
   (`az deployment sub create -l <region> -f identity/deployer-role.bicep`,
   then assign — `identity/README.md`) plus the two data-plane roles:
   **Key Vault Secrets Officer** and **Storage Blob Data Contributor** on the
   platform resource groups. The custom role covers every phase except
   network writes; where custom roles are not allowed, the fallback is
   `Contributor` + `User Access Administrator` on the subscription. Full
   matrix: `docs/IDENTITY-PERMISSIONS.md`.
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

### 5. Azure Artifacts media feed (installer media)

The BMC installer media lives as **Universal Packages** in the Azure
Artifacts feed named by `artifactsFeed` in `common.yml` (`ctm-media`). Four
packages are pinned there (names/versions in `common.yml`, mirrored by the
`media.azure_artifacts` block of `environments/<env>/config.yaml` — keep both
in sync):

| Package | Version | Contents (flat, at the package root) | Mapped blob prefix |
|---------|---------|--------------------------------------|--------------------|
| `control-m-em` | `9.0.21.300` | `DREM.9.0.21.300_Linux-x86_64.tar.Z` (EM EPD archive) | `em/` |
| `control-m-server` | `9.0.21.300` | `DRSRV.9.0.21.300_Linux-x86_64.tar.Z` (Server EPD archive) | `server/` |
| `control-m-agent` | `9.0.21.300` | `DRKAI.9.0.21.300_Linux-x86_64.tar.Z` (Agent EPD archive) | **none — consumed by the IMAGE stage** (`imageKind=agent`): it feeds the Packer ctm-agent build and never reaches blob |
| `control-m-silent-xml` | `1.0.0` | `silent-em-primary.xml`, `silent-em-secondary.xml`, `silent-server-primary.xml`, `silent-server-secondary.xml`, `silent-agent.xml` | by filename (below); `silent-agent.xml` maps to nothing — the Packer agent build consumes it |

**Layout / mapping rule** (implemented by `blob_name_for` in
`deploy/ctmdeploy/media.py`; packages stay **flat**, `ctmdeploy media upload`
maps each file to its blob name in the `installers` container):

* a path already prefixed `em/...` or `server/...` is uploaded **verbatim**
  (the pipeline downloads the em and server packages into directories of
  exactly those names);
* otherwise the **filename** decides: `DREM*` and `silent-em-*` → `em/`,
  `DRSRV*` and `silent-server-*` → `server/`;
* anything else — **all agent media (`DRKAI*`, `silent-agent*`) included** —
  is skipped with a warning and never uploaded (v4.1: the agent is baked
  into the ctm-agent image by the image stage).

The resulting blob names must match the `installer_blobs` of the config
(defaults: `em/DREM.9.0.21.300_Linux-x86_64.tar.Z`,
`server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z`, `em/silent-em-primary.xml`,
`em/silent-em-secondary.xml`, `server/silent-server-primary.xml`,
`server/silent-server-secondary.xml`). The upload
also pushes `node-scripts/ctm_node_install.py` to
`scripts/ctm_node_install.py`, keeping the node installer and the media in
lock-step. After every run `ctmdeploy media upload` prints the upload
manifest and **warns about any `installer_blobs` name still missing** from a
container, so an incomplete media set is visible before the first VM boots
against it.

**Create the feed**: Artifacts → **Create Feed** → name `ctm-media`, scope
*Project* (the az CLI has no feed-create command; the REST equivalent is
`POST https://feeds.dev.azure.com/<org>/<project>/_apis/packaging/feeds?api-version=7.1`).
Upstream sources are unnecessary — the feed only ever holds these packages.

**Publish the packages** (one directory per package, holding exactly the
files listed above; needs the azure-devops CLI extension and *Feed Publisher*
on the feed):

```bash
az extension add --name azure-devops
az login     # or export AZURE_DEVOPS_EXT_PAT=<PAT with Packaging read+write>
ORG=https://dev.azure.com/REPLACE_WITH_ORG
PROJ=REPLACE_WITH_PROJECT

# 1. EM EPD archive — ./epd/em contains only DREM.9.0.21.300_Linux-x86_64.tar.Z
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-em --version 9.0.21.300 \
  --description "BMC Control-M/EM 9.0.21.300 EPD media" --path ./epd/em

# 2. Server EPD archive — ./epd/server contains only DRSRV.9.0.21.300_Linux-x86_64.tar.Z
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-server --version 9.0.21.300 \
  --description "BMC Control-M/Server 9.0.21.300 EPD media" --path ./epd/server

# 3. Agent EPD archive — ./epd/agent contains only DRKAI.9.0.21.300_Linux-x86_64.tar.Z
#    (consumed by the IMAGE stage's Packer ctm-agent build, never uploaded to blob)
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-agent --version 9.0.21.300 \
  --description "BMC Control-M/Agent 9.0.21.300 EPD media (ctm-agent image build)" --path ./epd/agent

# 4. Silent-install XMLs — ./epd/silent-xml contains the five silent-*.xml files
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-silent-xml --version 1.0.0 \
  --description "Control-M silent-install XMLs" --path ./epd/silent-xml
```

Universal Package versions must be valid SemVer; if your feed rejects the
four-part BMC version `9.0.21.300`, publish it as `9.0.21-300` and set that
same string in `common.yml`
(`mediaEmVersion`/`mediaServerVersion`/`mediaAgentVersion`) and in
`media.azure_artifacts.packages.*.version` of both config files — the package
version is only a feed coordinate, the blob names and `installer_blobs` are
untouched. Published versions are **immutable**: fixing a media set means
publishing a new version and bumping the pins (common.yml + both config
files) by PR.

**Feed permissions for the pipeline identity**: the `UniversalPackages@0`
download task authenticates with the job access token, so on the feed
(Artifacts → ctm-media → gear icon → Permissions) grant the **Feed Reader**
role to the pipeline's build identity — `<Project> Build Service (<org>)`
when *Limit job authorization scope to current project* is on (the default),
otherwise `Project Collection Build Service (<org>)`. The upload side is Azure
RBAC, not feed RBAC: blob writes are **data-plane**, which subscription
Contributor does *not* include, so grant the WIF service connection's identity
(section 1) **Storage Blob Data Contributor** on the platform storage accounts
(or their resource groups) — for prod that is the primary *and* the DR blob
account. The feed must be in the organization running the pipeline; a feed
elsewhere requires `feedsToUse: external` plus a service connection in
`stage-media.yml`.

**How publishMedia runs are triggered**: a **manual run on `main` with
`publishMedia=true`**. The media stage depends only on validate and is
independent of what-if/apply (publishing media is a data-plane blob upload,
not an ARM deployment — nothing else in the run changes). It runs one job
per environment: three `UniversalPackages@0` downloads into `em/`, `server/`
and `silent_xml/` directories (the agent package belongs to the IMAGE
stage), then `python -m ctmdeploy media upload --env <env> --path …` inside
`AzureCLI@2` (federated login, overwrite
enabled — re-publishing the same set converges; prod uploads to the primary
**and** DR containers because `dr.enabled: true`). On a PR or any non-main
branch the stage never runs, even with the parameter set.

**Manual alternative (local use)** — the same mover runs from a workstation
with `az login` and **Storage Blob Data Contributor** on the storage
accounts (a management-plane role such as Contributor is not enough for
data-plane blob writes); useful before the pipeline exists or for urgent
fixes:

```bash
az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-em --version 9.0.21.300 --path ./media/em
az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-server --version 9.0.21.300 --path ./media/server
az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-silent-xml --version 1.0.0 --path ./media/silent_xml

python -m ctmdeploy media upload --env nonprod --path ./media
python -m ctmdeploy media upload --env prod --path ./media   # primary + DR
```

(Files staged by hand work too — the mapping rule above applies to any
directory.) `--role primary|dr|both` restricts the target platforms; the
default is `both` when DR is enabled, else `primary`.

### 6. Self-hosted scale-set agents (optional)

The prod `access` phase deploys the **`ctm-build-agents`** uniform VMSS
(Ubuntu 22.04, Manual upgrade, overprovision off, **no autoscale** — Azure
DevOps owns capacity) into `snet-ctm-build` of `vnet-ctm-build`, which peers
to the nonprod, prod **and** DR utility VNets: one agent pool deploys into
every platform, and its traffic to the Key Vault / blob private endpoints
rides those peerings. Bootstrap installs tooling only (python 3.11, unzip,
curl, the pinned checksum-verified bicep CLI); the elastic pool installs the
pipeline agent itself, and **WIF remains the deployment identity** (the scale
set's own `ctm-build-id` identity carries no role grants).

> **Re-run caveat:** the access phase sends the configured
> `build_agents.instances` as the VMSS `sku.capacity` on every deploy, so
> re-running `--phase access` after the elastic pool has taken ownership
> resets the **live instance count** to that baseline. The pool scales it
> back on its own; schedule access-phase re-runs outside build-heavy windows.

Register it as an Azure DevOps **elastic pool**:

1. Grant Azure DevOps' service principal (the identity the elastic-pool
   configuration runs as) **Virtual Machine Contributor** on `ctm-build-rg` —
   documented here on purpose, never granted by any template
   (`docs/IDENTITY-PERMISSIONS.md`).
2. Organization settings → Agent pools → **Add pool** → *Azure virtual
   machine scale set* → select the subscription and `ctm-build-agents` → set
   the pool name (e.g. `ctm-build`), max agents, and "tear down after each
   job" to taste. Azure DevOps now creates/deletes instances; never resize
   the scale set by hand.
3. Point the pipeline at the pool: `agentPoolName: "ctm-build"` in
   `pipelines/variables/common.yml`. Every stage template takes the value
   through its `agentPool` parameter and switches its jobs from
   Microsoft-hosted `ubuntu-latest` to `pool: name: ctm-build` at compile
   time; `""` switches back. (Self-hosted agents resolve the platform private
   DNS zones only if `vnet-ctm-build` is linked — see the DNS note in
   `docs/NETWORK-REQUIREMENTS.md`.)

### 7. No state backend

There is nothing else to set up. Bicep deployments are stateless ARM calls —
no state storage account, no state locks, no backend configuration, no
bootstrap step. The what-if stage is the review surface; the deployment
history itself lives in Azure (`az deployment sub/group list`, deployment
names `ctm-<env>-<phase>[-dr]`).

## Running

* Push to a feature branch and open a PR → validate, Checkmarx, two what-ifs.
* Merge to `main` → nonprod applies (no approvals on `ctm-nonprod`), then
  prod waits for its approvers. `--phase all` includes the **access** phase
  (Bastion, the private Application Gateway + `ctm-global`, the kvsync
  Function infrastructure, the build-agent scale set). The kvsync function
  **code** is a separate, idempotent data-plane push — run
  `python -m ctmdeploy kvsync deploy --env <env>` after an apply when
  `functions/kvsync/` changed (see `docs/SETUP.md` §6).
* Manual run with the **applyEnvironments** parameter limited to `[nonprod]`
  to stop promotion early; **runCheckmarx=false** skips the scan (what-ifs
  still run, applies still require main).
* Manual run with **runImageBuild=true** + **imageVersion** + **imageKind**
  to publish a new golden image (then bump `image.base.version` /
  `image.agent.version` in config via PR). `imageKind=base` builds the
  OS-only ctm-rhel9 (BMC software never baked in); `imageKind=agent` builds
  ctm-agent with the Control-M/Agent silent-installed (unconfigured) from
  the `control-m-agent` package the stage downloads.
* Manual run on `main` with **publishMedia=true** to publish the EM/Server
  installer media: the three Azure Artifacts universal packages pinned in
  `common.yml` (em, server, silent_xml) are downloaded and `ctmdeploy media
  upload` pushes them (plus the node installer script) to every platform's
  installers blob container (section 5). Runs independently of
  what-if/apply; does nothing on PRs or other branches.
* Set **agentPoolName** in `common.yml` to run everything on the self-hosted
  scale-set pool (section 6); leave it `""` for Microsoft-hosted agents.

## Files

| Path | Purpose |
|------|---------|
| `azure-pipelines.yml` | entry point, parameters, conditional default pool (agentPoolName), stage wiring |
| `pipelines/variables/common.yml` | tool pins (bicep sha256, packer, python), **agentPoolName** ('' = Microsoft-hosted), service connection, Azure Artifacts media feed/package pins (EM/Server/XML → media stage; **Agent → image stage**), DSV and Checkmarx settings |
| `pipelines/templates/stage-validate.yml` | bicep build+lint (bicep/, network/, identity/) + bicepparam build, compileall (deploy, node-scripts, functions), pytest, config load, shell syntax |
| `pipelines/templates/stage-security.yml` | Checkmarx One (task or CLI) |
| `pipelines/templates/stage-whatif.yml` | DSV secrets + `ctmdeploy whatif` + artifact + hasChanges gate |
| `pipelines/templates/stage-apply.yml` | gated deployment job running `ctmdeploy deploy --phase all` |
| `pipelines/templates/stage-image.yml` | pinned Packer + `ctmdeploy image gallery/build --image <imageKind>`; imageKind=agent also downloads the control-m-agent + silent-xml packages for `--media-path` |
| `pipelines/templates/stage-media.yml` | three Azure Artifacts universal-package downloads (em, server, silent_xml) + `ctmdeploy media upload` (publishMedia=true, main only) |
| `pipelines/templates/steps-dsv-secrets.yml` | Delinea DSV CLI install + secret read |
| `pipelines/templates/steps-tools-install.yml` | pinned Bicep (sha256 verified) + `pip install -e deploy/` |

Every stage template also takes an `agentPool` parameter (wired from
`agentPoolName`): `''` keeps the job on Microsoft-hosted `ubuntu-latest`,
anything else becomes `pool: name: <agentPool>` at compile time.
