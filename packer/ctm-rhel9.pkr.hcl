# ---------------------------------------------------------------------------
# Packer build: "ctm-rhel9" golden image for every Control-M node and agent.
# Bakes the OS prep that used to run in cloud-init on each first boot
# (packages, firewall/SELinux posture, limits/sysctl, Node.js 20) and
# publishes the result to the ctmgallery Azure Compute Gallery, replicated
# to eastus2 + centralus. See packer/README.md for the full workflow.
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
#   packer build -var image_version=1.0.0 packer/ctm-rhel9.pkr.hcl
variable "image_version" {
  type        = string
  description = "Compute Gallery image version to publish (semver x.y.z)."

  validation {
    condition     = can(regex("^\\d+\\.\\d+\\.\\d+$", var.image_version))
    error_message = "image_version must be semver, e.g. 1.0.0."
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
  description = "Gallery image definition name (Linux, Gen2)."
  default     = "ctm-rhel9"
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

source "azure-arm" "ctm_rhel9" {
  # Auth: CLI context by default; ARM_* env vars otherwise (empty -> unset).
  use_azure_cli_auth = var.use_azure_cli_auth
  client_id          = var.client_id != "" ? var.client_id : null
  client_secret      = var.client_secret != "" ? var.client_secret : null
  tenant_id          = var.tenant_id != "" ? var.tenant_id : null
  subscription_id    = var.subscription_id != "" ? var.subscription_id : null

  # Base: the same marketplace RHEL 9 LVM Gen2 image the platform ran on
  # before the golden image existed.
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
  }
}

build {
  name    = "ctm-rhel9"
  sources = ["source.azure-arm.ctm_rhel9"]

  # OS prep moved out of cloud-init into the image.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E /bin/bash '{{ .Path }}'"
    scripts = [
      "${path.root}/scripts/00-packages.sh",
      "${path.root}/scripts/10-os-tuning.sh",
      "${path.root}/scripts/20-nodejs.sh",
    ]
  }

  # Generalize last: resets cloud-init and deprovisions the Azure agent so
  # instances booted from the image run their own customData bootstrap.
  provisioner "shell" {
    execute_command   = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E /bin/bash '{{ .Path }}'"
    scripts           = ["${path.root}/scripts/90-deprovision.sh"]
    expect_disconnect = true
  }
}
