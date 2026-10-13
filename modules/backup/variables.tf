variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "storage_mode_type" {
  description = "Vault redundancy: LocallyRedundant, ZoneRedundant or GeoRedundant (required for cross-region restore)."
  type        = string
  default     = "GeoRedundant"
}

variable "cross_region_restore_enabled" {
  description = "Allow restoring VM backups into the paired region (GeoRedundant only)."
  type        = bool
  default     = false
}

variable "daily_retention_days" {
  type    = number
  default = 30
}

variable "weekly_retention_weeks" {
  type    = number
  default = 12
}

variable "backup_time_utc" {
  type    = string
  default = "23:00"
}

variable "vm_ids" {
  description = "VMs to protect: name => id."
  type        = map(string)
  default     = {}
}

variable "files_storage_account_id" {
  description = "Premium Files account whose share is backed up (null to skip)."
  type        = string
  default     = null
}

variable "files_share_name" {
  type    = string
  default = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
