# Setup — from empty subscription to a running Control-M platform

End-to-end order of operations. Each step names the doc that owns the
details. Commands run from the repository root; `<env>` is `nonprod` or
`prod` (bring nonprod up first).

## 0. Prerequisites

| What | Why |
|------|-----|
| **Azure CLI** (`az login`) + **Bicep CLI** on PATH | `ctmdeploy` compiles Bicep and submits ARM deployments through your az session |
| **Python ≥ 3.11**, then `pip install -e "deploy/[dev]"` | installs the `ctmdeploy` orchestrator (and pytest/ruff for `make test`/`lint`). The **nodes** need nothing installed — the node installer is stdlib-only python and runs on RHEL 9's system python3 |
| **Packer ≥ 1.10** on PATH (or `CTM_PACKER_BIN`) | golden image builds (step 5) |
| Subscription RBAC for the deploying identity: the **Control-M Platform Deployer** custom role (step 2) — or the built-in fallback **Contributor + User Access Administrator** | the custom role covers every phase except network writes; the fallback works everywhere but is far broader (`docs/IDENTITY-PERMISSIONS.md`) |
| **Key Vault Secrets Officer** + **Storage Blob Data Contributor** (data plane) for the deploying identity | secrets seeding and media/script uploads are data-plane calls that no management role includes (details + commands: `docs/IDENTITY-PERMISSIONS.md`) |
| An identity for the **cloud team**: **Network Contributor** on the `rg-network-*` groups (+ resource-group create) | the `network/` folder is theirs; the platform deployer deliberately cannot write VNets/subnets/NSGs/peerings |
| An **Azure DevOps organization + project** (pipeline path only) | hosts the pipeline, the Delinea DSV wiring, the `ctm-media` Azure Artifacts feed and (optionally) the scale-set agent pool |
| **BMC EPD media**: the EM, Server **and Agent** EPD `tar.Z` archives + the five silent-install XMLs (`scripts/silent-install/README.md`) | the software installed on the nodes — agent scale sets included — at provision time |

Sanity check: `make test` (the pure-python test suite; needs no Azure SDKs
or network) and `bicep build bicep/foundation.bicep --stdout > /dev/null`.

## 1. Fill in the config placeholders

Edit `environments/nonprod/config.yaml` and `environments/prod/config.yaml`
(full key reference: `docs/CONFIGURATION.md`):

* `subscription_id` — the real GUID (the `00000000-…` placeholder is rejected
  at deploy time).
* `deployer_ip_cidrs` — public CIDRs of your CI runners / admin stations
  (Key Vault + blob firewall).
* `observability.alert_emails` — real recipients.
* `unique_suffix` — keep `n01`/`p01` unless a globally unique name collides.
* The v4 blocks ship enabled — review them: `access.bastion`/`access.appgw`,
  `kv_sync` (EP1 runs a warm, billed instance; nonprod may set
  `enabled: false`) and, on prod, `build_agents`.
* If you will use the pipeline + Azure Artifacts path:
  `media.azure_artifacts.organization`/`project` (and the same values in
  `pipelines/variables/common.yml`).

## 2. Deployer identity (custom role)

Deploy the **Control-M Platform Deployer** role and assign it (plus the two
data-plane roles) to whatever identity runs `ctmdeploy` — the pipeline's WIF
service connection and/or your workstation principal:

```bash
az deployment sub create -l eastus2 -f identity/deployer-role.bicep
SP_OBJECT_ID=$(az ad sp show --id <appId> --query id -o tsv)
az role assignment create --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Control-M Platform Deployer" \
  --scope "/subscriptions/$(az account show --query id -o tsv)"
```

The role can deploy **everything except the network** — no
VNet/subnet/NSG/peering writes. Data-plane grants, the cloud-team identity
and the built-in fallback: `docs/IDENTITY-PERMISSIONS.md` and
`identity/README.md`. Deploying the role definition itself is a one-time
step that needs User Access Administrator (or Owner).

## 3. Networking — cloud team, Option A, or bring-your-own

Details, the full requirements list and the address plan:
`docs/NETWORK-REQUIREMENTS.md`.

* **Cloud team (recommended)**: the standalone `network/` folder deploys the
  complete plan — VNets, subnets (bastion/appgw/func included), one NSG per
  subnet, all peerings (spoke ⇄ utility, prod ⇄ DR global, build ⇄ all three
  utilities) — with nothing but the az CLI (`network/README.md`):

  ```bash
  # nonprod FIRST (the prod plan peers the build VNet into vnet-dev-utility):
  az deployment sub create -l eastus2 -f network/main.bicep -p network/params/nonprod.bicepparam
  az deployment sub create -l eastus2 -f network/main.bicep -p network/params/prod.bicepparam
  ```

* **Option A — the platform pipeline creates it**: flip
  `network_infrastructure.create: true` and run
  `python -m ctmdeploy deploy --env <env> --phase network` — the same
  template (`network/main.bicep`), fed from the config's identical plan. Set
  `user_access_cidrs` (keep the appgw subnet prefix on prod) and
  `bastion_cidrs` (keep the `AzureBastionSubnet` prefixes). Requires an
  identity that may write networks — the cloud team's, not the deployer role.
* **Option B — bring your own (`create: false`, the default)**: the network
  team provides everything per the contract in
  `docs/NETWORK-REQUIREMENTS.md` and the placement blocks select it by name.
  Even then, keep the `network_infrastructure.vnets` plan accurate: the
  access phase resolves its bastion/appgw/func subnet ids **by role** from
  that plan.

Either way, what stays yours: DNS forwarding from any custom DNS servers to
Azure DNS, on-prem connectivity, and outbound internet egress where default
outbound access is disabled.

## 4. Installer media feed (Azure Artifacts)

The BMC media is stored as **Universal Packages** in the `ctm-media` Azure
Artifacts feed — never in git, never in the image. Create the feed and
publish the **four** packages (full commands, layout and permissions:
`docs/AZURE-DEVOPS.md` §5). The em/server/silent_xml packages feed the blob
media stage; the **agent package feeds the ctm-agent image build (step 5)**:

```bash
az extension add --name azure-devops
ORG=https://dev.azure.com/<org>; PROJ=<project>

az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-em         --version 9.0.21.300 --path ./epd/em
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-server     --version 9.0.21.300 --path ./epd/server
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-agent      --version 9.0.21.300 --path ./epd/agent
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-silent-xml --version 1.0.0      --path ./epd/silent-xml
```

*No Azure DevOps?* Skip this step and upload the files straight to blob in
step 7 — `ctmdeploy media upload` accepts any staged directory.

## 5. Golden images (base, then agent)

Two images (v4.1): build the **base** first, then the **agent** image with
the Control-M/Agent media.

```bash
az login
python -m ctmdeploy image gallery --env prod   # one-time: ctm-images-rg / ctmgallery / ctm-rhel9 + ctm-agent

# 5a. BASE image (ctm-rhel9) — the OS only; EM/Server VMs boot this one.
python -m ctmdeploy image build --env prod --version 1.0.0

# 5b. AGENT image (ctm-agent) — bakes the Control-M/Agent in (unconfigured).
#     Stage the control-m-agent package + silent-agent.xml first:
az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project   --feed ctm-media --name control-m-agent      --version 9.0.21.300 --path ./agent-media
cp scripts/silent-install/silent-agent.xml ./agent-media/   # or download control-m-silent-xml
python -m ctmdeploy image build --env prod --version 1.0.0   --image agent --media-path ./agent-media
```

The base image is the **operating system only** (RHEL 9 + packages, tuning,
Node.js 20) — **no BMC software is baked into it**; EM and Server install at
provision time from the blob media via the idempotent python node installer.
The **agent image additionally embeds the Control-M/Agent** (silent-installed
with placeholder server settings, never started or registered at build time);
agent instances only configure/start/register at boot. Versions replicate to
both regions. Set `image.base.version` / `image.agent.version` in the config
files (pin prod; `latest` is fine for nonprod). Via the pipeline: manual run
with `runImageBuild=true` + `imageKind` (base|agent) + `imageVersion`.
Details: `packer/README.md`.

## 6. Deploy the platforms

```bash
python -m ctmdeploy whatif --env nonprod            # review
python -m ctmdeploy deploy --env nonprod --phase all
python -m ctmdeploy kvsync deploy --env nonprod     # kv_sync.enabled: push the function code
# then, once nonprod looks right:
python -m ctmdeploy whatif --env prod
python -m ctmdeploy deploy --env prod --phase all --wait
python -m ctmdeploy kvsync deploy --env prod
```

Phases, in order: `network` (only when `network_infrastructure.create`) →
`rg` → `foundation` → `secrets` → `compute` → **`access`** → `dr` →
`replication` (`dr`/`replication` run only where `dr.enabled`; any phase can
be re-run alone, and re-running is always safe — see the Idempotency section
of the README). The compute phase uploads `node-scripts/ctm_node_install.py`
to the installers container first, so nodes always boot the repo's installer
version. The access phase deploys Bastion, the kvsync Function
infrastructure, and on prod the private Application Gateway (`ctm-global`) —
plus its DR twin `ctm-prod-dr-appgw` in the DR utility VNet when
`access.appgw.dr_instance` (shipped on) — plus the `ctm-build-agents` scale
set. Pass `--deployer-object-id <objectId>`
(or export `CTM_DEPLOYER_OBJECT_ID`) so the foundation grants your principal
Key Vault Secrets Officer for the secrets phase. Everything that got created:
`docs/RESOURCES.md`; the vault contents and the kvsync tagging convention:
`docs/KEYVAULT.md`.

## 7. Publish the media to blob

The VMs boot and wait for the media, so do this right after (or before) the
compute phase. Either path ends in the same place — the `installers` blob
container of each platform (prod: primary **and** DR):

* **Pipeline**: manual run on `main` with **`publishMedia=true`** — downloads
  the three EM/Server packages from the feed and runs `ctmdeploy media
  upload` per environment. (The agent package is consumed by the IMAGE
  stage, step 5 — agent media never goes to blob.)
* **Workstation**:

  ```bash
  az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
    --feed ctm-media --name control-m-em         --version 9.0.21.300 --path ./media/em
  az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
    --feed ctm-media --name control-m-server     --version 9.0.21.300 --path ./media/server
  az artifacts universal download --organization "$ORG" --project "$PROJ" --scope project \
    --feed ctm-media --name control-m-silent-xml --version 1.0.0      --path ./media/silent_xml

  python -m ctmdeploy media upload --env nonprod --path ./media
  python -m ctmdeploy media upload --env prod    --path ./media    # primary + DR
  ```

The upload prints a manifest and **warns about any `installer_blobs` name
still missing** — resolve every warning before expecting nodes to finish
bootstrapping. (It also re-uploads the node installer script, keeping script
and media in lock-step.)

## 8. Verify boot and finish inside Control-M

* Watch a node: `tail -f /var/log/ctm-bootstrap.log`. SSH arrives through
  **Azure Bastion** (`access.bastion.enabled`) as `ctmops`, key = the
  `ssh-private-key` vault secret:

  ```bash
  az network bastion ssh --name ctm-<env>-bastion -g ctm-<env>-rg \
    --target-ip-address <node private IP> --auth-type ssh-key \
    --username ctmops --ssh-key ./ctmops.pem
  ```

  The thin cloud-init shim fetches and runs the idempotent installer
  (`node-scripts/ctm_node_install.py`); its stage machine records progress in
  `/var/lib/ctm/install-state.json` and every stage re-checks real state, so
  a stuck node is re-run safely with `systemctl start ctm-install.service`.
  Nodes retry the media download, so an upload after boot is fine. Order is
  self-enforcing: secondary EM waits for the primary's web server, secondary
  Servers wait for the primary's Configuration Agent, agents wait for the
  Automation API behind the LB, then configure + start the agent BAKED into
  their ctm-agent image and register themselves via the Automation API REST.
* **Register each Control-M/Server in its EM** (Configuration Manager → Add
  Control-M/Server: nonprod `CTM_DEV`, `CTM_QA`; prod `CTM_UAT`, `CTM_PROD`)
  and **enable automatic failover** for the HA pairs.
* On prod, browse `https://ctm-global.prod.ctm.internal` (the private
  Application Gateway) — it follows whichever region's EM is healthy;
  `ctm-web.<env>.ctm.internal` stays the region-local entry point.
* **Replace the self-signed EM certificate**, then set the kvsync app setting
  `CTM_VERIFY_TLS=true` and remove the flagged unverified-TLS registration
  calls in `node-scripts/ctm_node_install.py`.

## 9. Pipeline setup

Wire the Azure DevOps pipeline per `docs/AZURE-DEVOPS.md`: the Workload
Identity Federation service connection with the deployer role from step 2
(§1), Delinea DSV secrets + `ctm-dsv-auth` variable group (§2), Checkmarx One
(§3), the `ctm-nonprod` / `ctm-prod` environments with approvals (§4), the
media feed permissions (§5), and — once the prod access phase has deployed
`ctm-build-agents` — **create the Azure DevOps scale-set agent pool** (§6):

1. Grant Azure DevOps' service principal **Virtual Machine Contributor** on
   `ctm-build-rg`.
2. Organization settings → Agent pools → Add pool → **Azure virtual machine
   scale set** → pick `ctm-build-agents` → name the pool (e.g. `ctm-build`).
3. Set `agentPoolName: "ctm-build"` in `pipelines/variables/common.yml` —
   every stage then runs on the self-hosted agents inside the build VNet
   (peered to nonprod, prod **and** DR, so the one pool deploys everywhere);
   leave it `""` to stay on Microsoft-hosted `ubuntu-latest`.

From then on, platform changes are PRs against
`environments/<env>/config.yaml`: validate → scan → what-if → gated applies.

## 10. DR drill

```bash
python -m ctmdeploy failover --env prod --test   # Site Recovery test failover only
```

Run at least quarterly; the full failover/failback procedure is
`docs/DR-RUNBOOK.md`. The `ctm-global` alias needs no runbook step — the
gateway's health probe moves traffic to the DR load balancer as soon as the
failed-over EM answers, and the DR gateway twin (`access.appgw.dr_instance`)
keeps the entry itself answering even if the primary region is lost
(`ctmdeploy failover` re-asserts its record automatically).
