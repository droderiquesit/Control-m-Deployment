# Control-M golden images (Packer → Azure Compute Gallery)

Two image definitions live in the **ctmgallery** Azure Compute Gallery
(v4.1 two-image model):

| Image | Template | Contains | Boots |
| --- | --- | --- | --- |
| **ctm-rhel9** (base) | `ctm-rhel9.pkr.hcl` | RHEL 9 + OS prep **only — NEVER any BMC software** | EM and Control-M/Server VMs |
| **ctm-agent** | `ctm-agent.pkr.hcl` | the same OS prep **plus the Control-M/Agent silent-installed but UNCONFIGURED** (placeholder server settings, not started, not registered, media removed) | the agent VM scale sets |

**The base image is the operating system only.** EM and Control-M/Server are
installed at provision time by the python node installer
(`node-scripts/ctm_node_install.py`), using the BMC EPD media +
silent-install XMLs downloaded from the `installers` blob container (managed
identity). Rebuilding or bumping the base image therefore never changes the
Control-M version, and an EM/Server upgrade never needs a new base image.

**The agent image embeds the BMC agent software — and nothing else.** The
`control-m-agent` Universal Package feeds the **Packer build** (never the
platform blob containers): `scripts/30-agent-install.sh` creates the
`ctmagent` user and silent-installs the agent from a build-time media
directory using the `silent-agent.xml` placeholder contract with
**placeholder server config** (`START_AGENT` forced to `no`). At first boot
the node installer writes the real server name/ports/hostname into the baked
agent's `CONFIG.dat`, starts it and registers it through the EM Automation
API. Bumping the Control-M/Agent version = a new ctm-agent image version.

Both images build from marketplace `RedHat:RHEL:9-lvm-gen2:latest` and bake
in the OS prep that previously ran in cloud-init on every first boot:

| Script                      | Bakes                                                                                     | Used by |
| --------------------------- | ----------------------------------------------------------------------------------------- | --- |
| `scripts/00-packages.sh`    | Control-M prerequisite packages (jq, curl, nfs-utils, libnsl/libnsl2, ncurses, xfsprogs, chrony, …) | both |
| `scripts/10-os-tuning.sh`   | firewalld disabled (NSGs are the boundary), SELinux permissive, `limits.d`/`sysctl.d` Control-M tuning | both |
| `scripts/20-nodejs.sh`      | Node.js 20 + npm (Automation API CLI convenience; runtime-optional)                        | both |
| `scripts/30-agent-install.sh` | **ctm-agent only**: `ctmagent` user + Control-M/Agent silent install from the file-provisioned media (placeholder server config, never started/registered), then media cleanup | agent |
| `scripts/90-deprovision.sh` | cloud-init reset + `waagent -deprovision+user` so each instance runs its own bootstrap     | both |

Everything environment-specific (Key Vault secrets, data-disk mount, NFS
mount, EM/Server installation, agent configuration/registration, systemd
units) stays in `cloud-init/*.sh.tmpl` + `node-scripts/ctm_node_install.py`,
rendered per node by `ctmdeploy` and delivered as `customData`. **No secrets
are ever baked into either image** (the agent silent XML carries none).

## Prerequisites

- Packer **>= 1.10** and the Azure plugin: `packer init packer/` (installs
  `github.com/hashicorp/azure ~> 2`).
- The gallery must exist first. One-time setup (resource group
  `ctm-images-rg`, gallery `ctmgallery`, image definitions `ctm-rhel9` +
  `ctm-agent`):

  ```bash
  az group create -n ctm-images-rg -l eastus2
  az deployment group create -g ctm-images-rg \
    -f bicep/images.bicep -p location=eastus2
  ```

  (or `python -m ctmdeploy image gallery --env prod`, which does both.)
- Build permissions: contributor on `ctm-images-rg` plus the right to create
  the temporary build resource group in the subscription.
- **Agent image only**: a local media directory holding the Control-M/Agent
  EPD archive (`DRKAI*`) and a `silent-agent*.xml` answer file — the
  `control-m-agent` and `control-m-silent-xml` Azure Artifacts packages
  (the pipeline image stage downloads both; locally, stage them yourself).

## Authentication (no credentials in any file)

Two options, both credential-free on disk:

1. **Azure CLI (default)** — `az login` (and `az account set -s <sub>` if
   needed). The templates set `use_azure_cli_auth = true` by default.
2. **Service principal / workload identity via environment variables** —
   export the standard `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`,
   `ARM_SUBSCRIPTION_ID` and pass `-var use_azure_cli_auth=false`.

## Building a version

Via the deployment CLI (wraps `packer build`; requires `packer` on PATH):

```bash
az login
# base image (ctm-rhel9, OS only) — --image base is the default:
python -m ctmdeploy image build --env prod --version 1.0.0

# agent image (ctm-agent) — requires the agent media directory:
python -m ctmdeploy image build --env prod --version 1.0.0 \
  --image agent --media-path ./agent-media
```

`--media-path` is **required** for `--image agent` and **forbidden** for
`--image base` (the base image never touches BMC media). The CLI fails fast
when the directory is missing the `DRKAI*` archive or `silent-agent*.xml`.

Or raw Packer:

```bash
packer init     packer/
packer validate -var image_version=1.0.0 packer/ctm-rhel9.pkr.hcl
packer build    -var image_version=1.0.0 packer/ctm-rhel9.pkr.hcl

packer build -var image_version=1.0.0 -var media_path=./agent-media \
  packer/ctm-agent.pkr.hcl
```

Each build boots a temporary `Standard_D4s_v5` VM in `eastus2` (a throwaway
resource group Packer creates and deletes), runs its scripts, captures a
managed image `<definition>-<version>` in `ctm-images-rg`, and publishes
gallery version `<version>` replicated to **eastus2 + centralus** (primary
and DR regions both need local copies).

`image_version` is required and must be semver (`x.y.z`). Gallery versions
are immutable — never rebuild an existing version; bump instead. The two
definitions version independently.

## Version bump → rollout flow

1. Change a script under `packer/scripts/` (base and/or agent), or bump the
   agent media version (`control-m-agent` package) for a new agent build.
2. Build: `python -m ctmdeploy image build --env prod --version 1.1.0`
   (add `--image agent --media-path …` for the agent image). Via the
   pipeline: `runImageBuild=true` + `imageKind` + `imageVersion`.
3. Point the environment at it in `environments/<env>/config.yaml`:

   ```yaml
   image:
     gallery_resource_group: ctm-images-rg
     gallery_name: ctmgallery
     base:  {definition: ctm-rhel9, version: "1.1.0"}  # or: latest
     agent: {definition: ctm-agent, version: "1.1.0"}  # or: latest
   ```

4. `python -m ctmdeploy whatif --env <env>` then
   `python -m ctmdeploy deploy --env <env> --phase compute`:
   * **EM/Server VMs** are re-created on the new base image; Control-M state
     lives on the persistent data disks and the database, so a rebuilt node
     reattaches and restarts services.
   * **Agent scale sets** take the new agent image as a **VMSS model
     update**: new/scaled-out/reimaged instances boot the new version, while
     existing instances keep the old one until recycled (scale-in/out or a
     manual reimage — see `docs/OPERATIONS.md`). Agents deregister
     gracefully on scale-in, so recycling is safe.

## How images reach the VMs

`ctmdeploy` reads the nested `image:` block of
`environments/<env>/config.yaml` and resolves TWO gallery image resource ids:

```
/subscriptions/<sub>/resourceGroups/ctm-images-rg/providers/Microsoft.Compute
  /galleries/ctmgallery/images/ctm-rhel9/versions/<base.version|latest>
  /galleries/ctmgallery/images/ctm-agent/versions/<agent.version|latest>
```

They are passed as the `imageId` (nodes) and `agentImageId` (agent pools)
parameters of `bicep/compute.bicep`, whose `node` and `agent-pool` modules
use them as the VM/VMSS `imageReference`. With `version: latest` new
deployments pick up the newest published version; pin explicit versions in
prod for deterministic rebuilds.
