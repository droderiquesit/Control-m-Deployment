variable "subscription_id" {
  description = "Azure subscription id."
  type        = string
}

variable "environment" {
  description = "Platform: nonprod (hosts dev + qa Control-M/Servers) or prod (hosts uat + prod)."
  type        = string
}

variable "location" {
  description = "Primary region."
  type        = string
}

variable "unique_suffix" {
  description = "3-6 lowercase alphanumerics for globally unique names."
  type        = string
}

variable "resource_lock" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "network" {
  description = "Existing network for the primary region."
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

variable "deployer_ip_cidrs" {
  description = "Public CIDRs of CI runners/admins allowed through Key Vault and storage firewalls."
  type        = list(string)
  default     = []
}

variable "key_vault_public_access" {
  type    = bool
  default = true
}

variable "ctm_version" {
  type    = string
  default = "9.0.21.300"
}

variable "installer_blobs" {
  description = "Blob names for installers and silent XML files (see scripts/silent-install). null = module defaults."
  type = object({
    em_installer            = string
    server_installer        = string
    em_silent_primary       = string
    em_silent_secondary     = string
    server_silent_primary   = string
    server_silent_secondary = string
  })
  default = null
}

variable "image" {
  type = object({
    publisher = string
    offer     = string
    sku       = string
    version   = string
  })
  default = null
}

variable "em" {
  type = object({
    vm_size          = string
    data_disk_gb     = optional(number, 256)
    disk_type        = optional(string, "Premium_LRS")
    enable_secondary = optional(bool, true)
    zones            = optional(list(string), ["1", "2"])
    web_port         = optional(number, 8443)
  })
}

variable "servers" {
  description = "Control-M/Servers on this platform keyed by stage (dev/qa on nonprod, uat/prod on prod). Each has its own VM pair, database and agent scale set."
  type = map(object({
    name             = optional(string)
    vm_size          = string
    data_disk_gb     = optional(number, 256)
    disk_type        = optional(string, "Premium_LRS")
    enable_secondary = optional(bool, true)
    zones            = optional(list(string), ["1", "2"])
    agents = object({
      vm_size         = string
      min             = number
      max             = number
      default         = number
      zones           = optional(list(string), ["1", "2", "3"])
      use_spot        = optional(bool, false)
      hostgroup       = optional(string)
      provision_image = optional(string, "Agent.Linux")
      scale_out_cpu   = optional(number, 70)
      scale_in_cpu    = optional(number, 25)
    })
  }))
}

variable "database" {
  type = object({
    sku_name               = string
    storage_mb             = optional(number, 131072)
    postgres_version       = optional(string, "16")
    high_availability_mode = optional(string, "ZoneRedundant")
    backup_retention_days  = optional(number, 14)
    geo_redundant_backup   = optional(bool, false)
  })
}

variable "storage" {
  type = object({
    blob_replication_type  = optional(string, "RAGZRS")
    files_replication_type = optional(string, "ZRS")
    shared_share_quota_gb  = optional(number, 256)
  })
  default = {}
}

variable "load_balancer" {
  type = object({
    internal         = optional(bool, true)
    public_dns_label = optional(string)
  })
  default = {}
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
  default = {}
}

variable "observability" {
  type = object({
    log_retention_days = optional(number, 30)
    alert_emails       = optional(list(string), [])
  })
  default = {}
}

variable "dr" {
  description = "Disaster-recovery region settings. enabled=false skips the DR region entirely."
  type = object({
    enabled  = optional(bool, false)
    location = optional(string)
    network = optional(object({
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
    }))
    warm_agents          = optional(number, 0) # per Control-M/Server
    replica_sku_name     = optional(string)
    enable_site_recovery = optional(bool, true)
    traffic_manager      = optional(bool, false)
    public_dns_label     = optional(string)
  })
  default = {}
}
