# ---------------------------------------------------------------------------
# Packer build: "ctm-agent" golden image for the Control-M agent VM scale
# sets (v4.1 two-image model). Same RHEL 9 base + OS prep as ctm-rhel9, PLUS
# the Control-M/Agent silent-INSTALLED from a build-time media directory
# (the Azure Artifacts `control-m-agent` package, downloaded by the pipeline
# image stage or staged locally) — but UNCONFIGURED: the silent install uses
# PLACEHOLDER server settings, the agent is never started and never
# registered at build time. At boot, node-scripts/ctm_node_install.py points
# the baked agent at its real Control-M/Server, starts it and registers it
# through the EM Automation API.
#
# The BASE image (packer/ctm-rhel9.pkr.hcl) NEVER contains BMC software;
# this is the only image that does, and it embeds the AGENT only.
# Build: python -m ctmdeploy image build --env prod --version 1.0.0 \
#            --image agent --media-path <dir with DRKAI* + silent-agent*.xml>
# See packer/README.md for the full workflow.
# ---------------------------------------------------------------------------

packer {
  required_version = ">= 1.10.0"
  required_plugins {
    azure = {
      source  = "github.com/hashicorp/azure"
      version = "~> 2"
    }
  }
}

# Required on every build: the gallery version to publish, e.g.
#   packer build -var image_version=1.0.0 -var media_path=/tmp/agent-media \
#     packer/ctm-agent.pkr.hcl
variable "image_version" {
  type        = string
  description = "Compute Gallery image version to publish (semver x.y.z)."

  validation {
    condition     = can(regex("^\\d+\\.\\d+\\.\\d+$", var.image_version))
    error_message = "image_version must be semver, e.g. 1.0.0."
  }
}

variable "media_path" {
  type        = string
  description = "Local directory holding the Control-M/Agent media: the DRKAI*/Agent* EPD archive plus a silent-agent*.xml answer file (from the control-m-agent and control-m-silent-xml Azure Artifacts packages). File-provisioned into the build VM; never published anywhere else."

  validation {
    condition     = length(var.media_path) > 0
    error_message = "media_path is required: the directory with the downloaded control-m-agent media."
  }
}

variable "location" {
  type        = string
  description = "Region the temporary build VM runs in."
  default     = "eastus2"
}

variable "replication_regions" {
  type        = list(string)
  description = "Regions the published image version is replicated to (primary + DR)."
  default     = ["eastus2", "centralus"]
}

variable "gallery_resource_group" {
  type        = string
  description = "Resource group holding the Compute Gallery (bicep/images.bicep)."
  default     = "ctm-images-rg"
}

variable "gallery_name" {
  type        = string
  description = "Azure Compute Gallery name."
  default     = "ctmgallery"
}

variable "image_definition" {
  type        = string
  description = "Gallery image definition name (Linux, Gen2; the agent image)."
  default     = "ctm-agent"
}

variable "vm_size" {
  type        = string
  description = "Temporary build VM size (must support Gen2 images)."
  default     = "Standard_D4s_v5"
}

# ---------------------------------------------------------------------------
# Authentication. Default: your Azure CLI login (az login). Alternative:
# workload identity / service principal through the standard ARM_* environment
# variables with -var use_azure_cli_auth=false. NO credentials in this file.
# ---------------------------------------------------------------------------
variable "use_azure_cli_auth" {
  type        = bool
  description = "Authenticate with the current az CLI login (default). Set false to use ARM_* env vars."
  default     = true
}

variable "client_id" {
  type    = string
  default = env("ARM_CLIENT_ID")
}

variable "client_secret" {
  type      = string
  default   = env("ARM_CLIENT_SECRET")
  sensitive = true
}

variable "tenant_id" {
  type    = string
  default = env("ARM_TENANT_ID")
}

variable "subscription_id" {
  type    = string
  default = env("ARM_SUBSCRIPTION_ID")
}

source "azure-arm" "ctm_agent" {
  # Auth: CLI context by default; ARM_* env vars otherwise (empty -> unset).
  use_azure_cli_auth = var.use_azure_cli_auth
  client_id          = var.client_id != "" ? var.client_id : null
  client_secret      = var.client_secret != "" ? var.client_secret : null
  tenant_id          = var.tenant_id != "" ? var.tenant_id : null
  subscription_id    = var.subscription_id != "" ? var.subscription_id : null

  # Same marketplace RHEL 9 LVM Gen2 base as the ctm-rhel9 image.
  os_type         = "Linux"
  image_publisher = "RedHat"
  image_offer     = "RHEL"
  image_sku       = "9-lvm-gen2"
  image_version   = "latest"

  # Packer creates (and deletes) a temporary resource group in this location.
  location = var.location
  vm_size  = var.vm_size

  communicator = "ssh"
  ssh_username = "packer"

  # Intermediate managed image: the capture artifact the gallery version is
  # published from (same RG as the gallery; replace on version rebuilds).
  managed_image_name                = "${var.image_definition}-${var.image_version}"
  managed_image_resource_group_name = var.gallery_resource_group

  shared_image_gallery_destination {
    resource_group       = var.gallery_resource_group
    gallery_name         = var.gallery_name
    image_name           = var.image_definition
    image_version        = var.image_version
    replication_regions  = var.replication_regions
    storage_account_type = "Standard_LRS"
  }

  azure_tags = {
    platform  = "control-m"
    builtBy   = "packer"
    baseImage = "RedHat:RHEL:9-lvm-gen2"
    contains  = "control-m-agent-unconfigured"
  }
}

build {
  name    = "ctm-agent"
  sources = ["source.azure-arm.ctm_agent"]

  # Same OS prep as the base image.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E /bin/bash '{{ .Path }}'"
    scripts = [
      "${path.root}/scripts/00-packages.sh",
      "${path.root}/scripts/10-os-tuning.sh",
      "${path.root}/scripts/20-nodejs.sh",
    ]
  }

  # Stage the agent media (DRKAI* archive + silent-agent*.xml) into the build
  # VM. The file provisioner runs as the ssh user, so it lands in /tmp; the
  # install script consumes and DELETES it.
  provisioner "shell" {
    inline = ["mkdir -p /tmp/ctm-agent-media"]
  }

  provisioner "file" {
    source      = "${var.media_path}/"
    destination = "/tmp/ctm-agent-media"
  }

  # Silent-install the agent (ctmagent user, placeholder server settings,
  # NOT started, NOT registered), then remove the media.
  provisioner "shell" {
    execute_command  = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E /bin/bash '{{ .Path }}'"
    environment_vars = ["CTM_AGENT_MEDIA_DIR=/tmp/ctm-agent-media"]
    scripts          = ["${path.root}/scripts/30-agent-install.sh"]
  }

  # Generalize last: resets cloud-init and deprovisions the Azure agent so
  # instances booted from the image run their own customData bootstrap.
  provisioner "shell" {
    execute_command   = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E /bin/bash '{{ .Path }}'"
    scripts           = ["${path.root}/scripts/90-deprovision.sh"]
    expect_disconnect = true
  }
}
