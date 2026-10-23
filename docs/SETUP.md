# Setup — from empty subscription to a running Control-M platform

End-to-end order of operations. Each step names the doc that owns the
details. Commands run from the repository root; `<env>` is `nonprod` or
`prod` (bring nonprod up first).

## 0. Prerequisites

| What | Why |
|------|-----|
| **Azure CLI** (`az login`) + **Bicep CLI** on PATH | `ctmdeploy` compiles Bicep and submits ARM deployments through your az session |
| **Python ≥ 3.11**, then `pip install -e "deploy/[dev]"` | installs the `ctmdeploy` orchestrator (and pytest/ruff for `make test`/`lint`) |
| **Packer ≥ 1.10** on PATH (or `CTM_PACKER_BIN`) | golden image builds (step 4) |
| Subscription RBAC for the deploying identity: **Contributor + User Access Administrator** | the foundation creates role assignments (Key Vault, storage); UAA is required for those |
| Blob **data-plane** role for whoever uploads media: **Storage Blob Data Contributor** on the platform storage accounts | media upload (step 6) is a data-plane write; Contributor alone is not enough |
| An **Azure DevOps organization + project** (pipeline path only) | hosts the pipeline, the Delinea DSV wiring and the `ctm-media` Azure Artifacts feed |
| **BMC EPD media**: the EM and Server EPD `tar.Z` archives + the four silent-install XMLs (`scripts/silent-install/README.md`) | the software installed on the nodes at provision time |

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
* If you will use the pipeline + Azure Artifacts path:
  `media.azure_artifacts.organization`/`project` (and the same values in
  `pipelines/variables/common.yml`).

## 2. Networking — Option A or Option B

Details and the address plan: `docs/NETWORK-REQUIREMENTS.md`.

* **Option A — let this repo create the network.** The shipped config files
  already carry the complete PoC address plan under
  `network_infrastructure.vnets`. Flip **one flag** and deploy:

  ```yaml
  network_infrastructure:
    create: true
    user_access_cidrs: ["<your operator CIDR>"]   # Control-M Web/API 443,8443
    bastion_cidrs: ["<your bastion CIDR>"]        # SSH 22
  ```

  ```bash
  python -m ctmdeploy deploy --env <env> --phase network
  ```

  This provisions the network resource groups, VNets, subnets (with
  delegation and PE policies), one NSG per subnet and the spoke ⇄ utility
  peerings — DR VNets included on prod. Validation now also cross-checks the
  placement blocks against the vnets list, so typos fail at load time.

* **Option B — bring your own (the default, `create: false`).** Have the
  network team provide the VNets/subnets/peering/NSGs per the contract in
  `docs/NETWORK-REQUIREMENTS.md`, and point the placement blocks
  (`network.utility`, `servers.<stage>.network`, `dr.*`) at them. The
  `network` phase then skips itself.

Either way, what stays yours: DNS forwarding from any custom DNS servers to
Azure DNS, on-prem connectivity, and the bastion/jump host itself.

## 3. Installer media feed (Azure Artifacts)

The BMC media is stored as **Universal Packages** in the `ctm-media` Azure
Artifacts feed — never in git, never in the image. Create the feed and
publish the three packages (full commands, layout and permissions:
`docs/AZURE-DEVOPS.md` §5):

```bash
az extension add --name azure-devops
ORG=https://dev.azure.com/<org>; PROJ=<project>

az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-em         --version 9.0.21.300 --path ./epd/em
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-server     --version 9.0.21.300 --path ./epd/server
az artifacts universal publish --organization "$ORG" --project "$PROJ" --scope project \
  --feed ctm-media --name control-m-silent-xml --version 1.0.0      --path ./epd/silent-xml
```

*No Azure DevOps?* Skip this step and upload the files straight to blob in
step 6 — `ctmdeploy media upload` accepts any staged directory.

## 4. Golden image

```bash
az login
python -m ctmdeploy image gallery --env prod               # one-time: ctm-images-rg / ctmgallery / ctm-rhel9
python -m ctmdeploy image build  --env prod --version 1.0.0
```

The image is the **operating system only** (RHEL 9 + packages, tuning,
Node.js 20) — **no BMC software is baked in**; EM/Server are installed at
provision time from the blob media and agents are provisioned through the EM
Automation API. Versions replicate to both regions. Set
`image.image_version` in the config files (pin prod; `latest` is fine for
nonprod). Details: `packer/README.md`.

## 5. Deploy the platforms

```bash
python -m ctmdeploy whatif --env nonprod            # review
python -m ctmdeploy deploy --env nonprod --phase all
# then, once nonprod looks right:
python -m ctmdeploy whatif --env prod
python -m ctmdeploy deploy --env prod --phase all --wait
```

Phases, in order: `network` (only when `network_infrastructure.create`) →
`rg` → `foundation` → `secrets` → `compute` → `dr` → `replication` (`dr`/
`replication` run only where `dr.enabled`; any phase can be re-run alone, and
re-running is always safe — see the Idempotency section of the README). Pass
`--deployer-object-id <objectId>` (or export `CTM_DEPLOYER_OBJECT_ID`) so the
foundation grants your principal Key Vault Secrets Officer for the secrets
phase. Everything that got created: `docs/RESOURCES.md`; the vault contents:
`docs/KEYVAULT.md`.

## 6. Publish the media to blob

The VMs boot and wait for the media, so do this right after (or before) the
compute phase. Either path ends in the same place — the `installers` blob
container of each platform (prod: primary **and** DR):

* **Pipeline**: manual run on `main` with **`publishMedia=true`** — downloads
  the three packages from the feed and runs `ctmdeploy media upload` per
  environment.
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
bootstrapping.

## 7. Verify boot and finish inside Control-M

* Watch a node: `tail -f /var/log/ctm-bootstrap.log` (serial console or SSH
  via your bastion as `ctmops`, key = the `ssh-private-key` vault secret).
  Nodes retry the media download, so an upload after boot is fine. Order is
  self-enforcing: secondary EM waits for the primary's web server, secondary
  Servers wait for the primary's Configuration Agent, agents wait for the
  Automation API behind the LB.
* **Register each Control-M/Server in its EM** (Configuration Manager → Add
  Control-M/Server: nonprod `CTM_DEV`, `CTM_QA`; prod `CTM_UAT`, `CTM_PROD`)
  and **enable automatic failover** for the HA pairs. Agents register
  themselves once their Server exists.
* **Replace the self-signed EM certificate** and remove the `-k` /
  `NODE_TLS_REJECT_UNAUTHORIZED=0` shortcuts in `cloud-init/agent.sh.tmpl`.

## 8. Pipeline setup

Wire the Azure DevOps pipeline per `docs/AZURE-DEVOPS.md`: the Workload
Identity Federation service connection (§1), Delinea DSV secrets +
`ctm-dsv-auth` variable group (§2), Checkmarx One (§3), the `ctm-nonprod` /
`ctm-prod` environments with approvals (§4), and the media feed permissions
(§5). From then on, platform changes are PRs against
`environments/<env>/config.yaml`: validate → scan → what-if → gated applies.

## 9. DR drill

```bash
python -m ctmdeploy failover --env prod --test   # Site Recovery test failover only
```

Run at least quarterly; the full failover/failback procedure is
`docs/DR-RUNBOOK.md`.
