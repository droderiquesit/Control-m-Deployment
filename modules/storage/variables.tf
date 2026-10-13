variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "blob_account_name" {
  description = "Globally unique name for the installers/backups blob account."
  type        = string
}

variable "files_account_name" {
  description = "Globally unique name for the premium Azure Files (NFS) account."
  type        = string
}

variable "blob_replication_type" {
  description = "Replication for the blob account: LRS, ZRS, GRS, RAGRS, GZRS, RAGZRS."
  type        = string
  default     = "RAGZRS"
}

variable "files_replication_type" {
  description = "Replication for premium files: LRS or ZRS (Premium ZRS needs a supported region)."
  type        = string
  default     = "ZRS"
}

variable "shared_share_quota_gb" {
  description = "Size of the NFS share holding shared Control-M files (install media cache, EM/Server backups, logs)."
  type        = number
  default     = 256
}

variable "pe_subnet_id" {
  type = string
}

variable "blob_private_dns_zone_id" {
  type = string
}

variable "file_private_dns_zone_id" {
  type = string
}

variable "allowed_ip_cidrs" {
  description = "Public CIDRs allowed through storage firewalls (upload installers, run Terraform data-plane calls)."
  type        = list(string)
  default     = []
}

variable "node_identity_principal_id" {
  description = "Principal that downloads installers (Storage Blob Data Reader)."
  type        = string
}

variable "enable_object_replication_source" {
  description = "Enable versioning + change feed so the account can be an object-replication source/destination."
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
