# ctm-rhel9 golden image (Packer → Azure Compute Gallery)

Every Control-M VM (EM, Server) and agent VMSS instance boots from the
**ctm-rhel9** image in the **ctmgallery** Azure Compute Gallery instead of the
raw marketplace image. Packer builds that image from marketplace
`RedHat:RHEL:9-lvm-gen2:latest` and bakes in the OS prep that previously ran
in cloud-init on every first boot:

| Script                    | Bakes                                                                                     |
| ------------------------- | ----------------------------------------------------------------------------------------- |
| `scripts/00-packages.sh`  | Control-M prerequisite packages (jq, curl, nfs-utils, libnsl/libnsl2, ncurses, xfsprogs, chrony, …) |
| `scripts/10-os-tuning.sh` | firewalld disabled (NSGs are the boundary), SELinux permissive, `limits.d`/`sysctl.d` Control-M tuning |
| `scripts/20-nodejs.sh`    | Node.js 20 + npm (Automation API CLI on agents; harmless on EM/Server)                     |
| `scripts/90-deprovision.sh` | cloud-init reset + `waagent -deprovision+user` so each instance runs its own bootstrap   |

Everything environment-specific (Key Vault secrets, data-disk mount, NFS
mount, Control-M installation, systemd units, agent registration) stays in
`cloud-init/*.sh.tmpl`, rendered per node by `ctmdeploy` and delivered as
`customData`. No secrets are ever baked into the image.

## Prerequisites

- Packer **>= 1.10** and the Azure plugin: `packer init packer/ctm-rhel9.pkr.hcl`
  (installs `github.com/hashicorp/azure ~> 2`).
- The gallery must exist first. One-time setup (resource group
  `ctm-images-rg`, gallery `ctmgallery`, image definition `ctm-rhel9`):

  ```bash
  az group create -n ctm-images-rg -l eastus2
  az deployment group create -g ctm-images-rg \
    -f bicep/images.bicep -p location=eastus2
  ```

- Build permissions: contributor on `ctm-images-rg` plus the right to create
  the temporary build resource group in the subscription.

## Authentication (no credentials in any file)

Two options, both credential-free on disk:

1. **Azure CLI (default)** — `az login` (and `az account set -s <sub>` if
   needed). The template sets `use_azure_cli_auth = true` by default.
2. **Service principal / workload identity via environment variables** —
   export the standard `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`,
   `ARM_SUBSCRIPTION_ID` and pass `-var use_azure_cli_auth=false`.

## Building a version

Via the deployment CLI (wraps `packer build`; requires `packer` on PATH):

```bash
az login
python -m ctmdeploy image build --env prod --version 1.0.0
```

Or raw Packer:

```bash
packer init  packer/ctm-rhel9.pkr.hcl
packer validate -var image_version=1.0.0 packer/ctm-rhel9.pkr.hcl
packer build    -var image_version=1.0.0 packer/ctm-rhel9.pkr.hcl
```

The build boots a temporary `Standard_D4s_v5` VM in `eastus2` (a throwaway
resource group Packer creates and deletes), runs the four scripts, captures a
managed image `ctm-rhel9-<version>` in `ctm-images-rg`, and publishes gallery
version `<version>` replicated to **eastus2 + centralus** (primary and DR
regions both need local copies).

`image_version` is required and must be semver (`x.y.z`). Gallery versions
are immutable — never rebuild an existing version; bump instead.

## Version bump → rollout flow

1. Change a script under `packer/scripts/` (or just rebuild to pick up newer
   RHEL/package content).
2. `python -m ctmdeploy image build --env prod --version 1.1.0`
3. Point the environment at it in `environments/<env>/config.yaml`:

   ```yaml
   image:
     gallery_resource_group: ctm-images-rg
     gallery_name: ctmgallery
     image_definition: ctm-rhel9
     image_version: "1.1.0"   # or: latest
   ```

4. `python -m ctmdeploy whatif --env <env>` then
   `python -m ctmdeploy deploy --env <env> --phase compute` — nodes/VMSS are
   re-created on the new image; Control-M state lives on the persistent data
   disks and the database, so a rebuilt node reattaches and restarts services.

## How images reach the VMs

`ctmdeploy` reads the `image:` block of `environments/<env>/config.yaml` and
resolves it to a gallery image resource id:

```
/subscriptions/<sub>/resourceGroups/ctm-images-rg/providers/Microsoft.Compute
  /galleries/ctmgallery/images/ctm-rhel9/versions/<version|latest>
```

That id is passed as the `imageId` parameter to `bicep/compute.bicep`, whose
`node` and `agent-pool` modules use it as the VM/VMSS `imageReference`. With
`image_version: latest` new deployments pick up the newest published version;
pin an explicit version in prod for deterministic rebuilds.
