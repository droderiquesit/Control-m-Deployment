variable "name" {
  description = "PostgreSQL Flexible Server name (globally unique, lowercase)."
  type        = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "delegated_subnet_id" {
  type = string
}

variable "private_dns_zone_id" {
  type = string
}

variable "dns_link_dependency" {
  description = "Id of the VNet link for the postgres private DNS zone (ordering only; null when the zone pre-exists)."
  type        = string
  default     = null
}

variable "postgres_version" {
  description = "PostgreSQL major version supported by your Control-M release (BMC: check the Control-M compatibility matrix)."
  type        = string
  default     = "16"
}

variable "sku_name" {
  description = "Flexible Server SKU, e.g. B_Standard_B2ms (dev), GP_Standard_D4ds_v5, MO_Standard_E8ds_v5."
  type        = string
}

variable "storage_mb" {
  type    = number
  default = 131072
}

variable "storage_tier" {
  description = "Optional storage performance tier (P10..P80). null = default for size."
  type        = string
  default     = null
}

variable "auto_grow_enabled" {
  type    = bool
  default = true
}

variable "zone" {
  description = "Primary availability zone."
  type        = string
  default     = "1"
}

variable "high_availability_mode" {
  description = "Disabled, SameZone or ZoneRedundant."
  type        = string
  default     = "ZoneRedundant"
  validation {
    condition     = contains(["Disabled", "SameZone", "ZoneRedundant"], var.high_availability_mode)
    error_message = "high_availability_mode must be Disabled, SameZone or ZoneRedundant."
  }
}

variable "standby_zone" {
  type    = string
  default = "2"
}

variable "backup_retention_days" {
  type    = number
  default = 14
}

variable "geo_redundant_backup_enabled" {
  description = "Replicate automated backups to the paired region (DR)."
  type        = bool
  default     = false
}

variable "administrator_login" {
  type    = string
  default = "ctmadmin"
}

variable "administrator_password" {
  type      = string
  sensitive = true
}

variable "databases" {
  description = "Databases to pre-create (Control-M installers can also create their own)."
  type        = list(string)
  default     = []
}

variable "server_parameters" {
  description = "Server parameters to set (name => value)."
  type        = map(string)
  default = {
    max_connections                     = "500"
    log_min_duration_statement          = "5000"
    "azure.extensions"                  = "PG_STAT_STATEMENTS"
    idle_in_transaction_session_timeout = "600000"
  }
}

variable "maintenance_window" {
  description = "Custom maintenance window; day_of_week 0=Sunday."
  type = object({
    day_of_week  = number
    start_hour   = number
    start_minute = number
  })
  default = {
    day_of_week  = 0
    start_hour   = 3
    start_minute = 0
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}
