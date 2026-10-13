# ---------------------------------------------------------------------------
# Identity / placement
# ---------------------------------------------------------------------------
variable "environment" {
  description = "dev | qa | uat | prod"
  type        = string
  validation {
    condition     = contains(["dev", "qa", "uat", "prod"], var.environment)
    error_message = "environment must be one of dev, qa, uat, prod."
  }
}

variable "name_prefix" {
  description = "Short prefix for every resource name."
  type        = string
  default     = "ctm"
}

variable "location" {
  description = "Azure region for this platform instance."
  type        = string
}

variable "role" {
  description = "primary = full platform; dr = standby region (network, secrets, storage, LB, monitoring, optional agents; DB replica + VMs arrive via the dr module)."
  type        = string
  default     = "primary"
  validation {
    condition     = contains(["primary", "dr"], var.role)
    error_message = "role must be primary or dr."
  }
}

variable "unique_suffix" {
  description = "3-6 lowercase alphanumerics making globally-unique names (storage, key vault, postgres) unique."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,6}$", var.unique_suffix))
    error_message = "unique_suffix must be 3-6 lowercase alphanumerics."
  }
}

variable "resource_lock" {
  description = "Apply a CanNotDelete lock on the platform resource group."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ---------------------------------------------------------------------------
# Existing network (selected, not created)
# ---------------------------------------------------------------------------
variable "network" {
  description = "Existing VNet/subnets to deploy into. Optional ids of hub-managed private DNS zones."
  type = object({
    vnet_name                = string
    vnet_resource_group_name = string
    app_subnet_name          = string
    data_subnet_name         = string
    pe_subnet_name           = optional(string)
    existing_private_dns_zone_ids = optional(object({
      blob     = optional(string)
      file     = optional(string)
      vault    = optional(string)
      postgres = optional(string)
    }), {})
  })
}

variable "private_dns_zone_name" {
  description = "Platform private DNS zone (same name in primary and DR so failed-over nodes keep resolving ctm-db / ctm-web)."
  type        = string
}

variable "deployer_ip_cidrs" {
  description = "Public CIDRs of CI runners / admins allowed through the Key Vault and storage firewalls for data-plane operations."
  type        = list(string)
  default     = []
}

variable "key_vault_public_access" {
  description = "Keep the Key Vault reachable from the public endpoint (restricted by firewall) so CI can write secrets."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Secrets (generated once at the environment root and replicated to DR)
# ---------------------------------------------------------------------------
variable "secrets" {
  description = "Secrets to store in Key Vault: db-admin-password, em-db-password, ctm-db-password, em-admin-password, ssh-private-key."
  type        = map(string)
  sensitive   = true
}

variable "ssh_public_key" {
  type = string
}

# ---------------------------------------------------------------------------
# Control-M release / media
# ---------------------------------------------------------------------------
variable "ctm_version" {
  description = "Control-M release used for media paths (e.g. 9.0.21.300)."
  type        = string
  default     = "9.0.21.300"
}

variable "installer_blobs" {
  description = "Blob names (inside the installers container) uploaded by you from BMC EPD."
  type = object({
    em_installer            = string
    server_installer        = string
    em_silent_primary       = string
    em_silent_secondary     = string
    server_silent_primary   = string
    server_silent_secondary = string
  })
  default = {
    em_installer            = "em/DREM.9.0.21.300_Linux-x86_64.tar.Z"
    server_installer        = "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z"
    em_silent_primary       = "em/silent-em-primary.xml"
    em_silent_secondary     = "em/silent-em-secondary.xml"
    server_silent_primary   = "server/silent-server-primary.xml"
    server_silent_secondary = "server/silent-server-secondary.xml"
  }
  nullable = false
}

variable "image" {
  description = "Marketplace image for all nodes (RHEL 9 is on the BMC support matrix for 9.0.21)."
  type = object({
    publisher = string
    offer     = string
    sku       = string
    version   = string
  })
  default = {
    publisher = "RedHat"
    offer     = "RHEL"
    sku       = "9-lvm-gen2"
    version   = "latest"
  }
  nullable = false
}

# ---------------------------------------------------------------------------
# Tier sizing
# ---------------------------------------------------------------------------
variable "em" {
  description = "Control-M/Enterprise Manager tier."
  type = object({
    deploy           = optional(bool, true)
    vm_size          = string
    data_disk_gb     = optional(number, 256)
    disk_type        = optional(string, "Premium_LRS")
    enable_secondary = optional(bool, true)
    zones            = optional(list(string), ["1", "2"])
    os_user          = optional(string, "emuser")
    db_name          = optional(string, "emdb")
    db_user          = optional(string, "emuser")
    web_port         = optional(number, 8443)
  })
}

variable "server" {
  description = "Control-M/Server tier."
  type = object({
    deploy           = optional(bool, true)
    vm_size          = string
    data_disk_gb     = optional(number, 256)
    disk_type        = optional(string, "Premium_LRS")
    enable_secondary = optional(bool, true)
    zones            = optional(list(string), ["1", "2"])
    os_user          = optional(string, "controlm")
    db_name          = optional(string, "ctmdb")
    db_user          = optional(string, "ctmuser")
    server_name      = optional(string)
  })
}

variable "agents" {
  description = "Control-M/Agent scale set."
  type = object({
    deploy          = optional(bool, true)
    vm_size         = string
    min             = number
    max             = number
    default         = number
    zones           = optional(list(string), ["1", "2", "3"])
    use_spot        = optional(bool, false)
    hostgroup       = optional(string, "azure-agents")
    provision_image = optional(string, "Agent.Linux")
    agent_port      = optional(number, 7006)
    os_user         = optional(string, "ctmagent")
    scale_out_cpu   = optional(number, 70)
    scale_in_cpu    = optional(number, 25)
  })
}

variable "database" {
  description = "PostgreSQL Flexible Server. create=false (DR) points nodes at host_override."
  type = object({
    create                 = optional(bool, true)
    host_override          = optional(string)
    sku_name               = optional(string, "GP_Standard_D4ds_v5")
    storage_mb             = optional(number, 131072)
    postgres_version       = optional(string, "16")
    high_availability_mode = optional(string, "ZoneRedundant")
    backup_retention_days  = optional(number, 14)
    geo_redundant_backup   = optional(bool, false)
    admin_user             = optional(string, "ctmadmin")
    port                   = optional(number, 5432)
  })
}

variable "storage" {
  type = object({
    blob_replication_type  = optional(string, "RAGZRS")
    files_replication_type = optional(string, "ZRS")
    shared_share_quota_gb  = optional(number, 256)
  })
  default  = {}
  nullable = false
}

variable "load_balancer" {
  type = object({
    internal         = optional(bool, true)
    public_dns_label = optional(string)
  })
  default  = {}
  nullable = false
}

variable "backup" {
  type = object({
    enabled                = optional(bool, true)
    storage_mode_type      = optional(string, "GeoRedundant")
    cross_region_restore   = optional(bool, false)
    daily_retention_days   = optional(number, 30)
    weekly_retention_weeks = optional(number, 12)
    include_files_share    = optional(bool, true)
  })
  default  = {}
  nullable = false
}

variable "observability" {
  type = object({
    log_retention_days = optional(number, 30)
    alert_emails       = optional(list(string), [])
  })
  default  = {}
  nullable = false
}
