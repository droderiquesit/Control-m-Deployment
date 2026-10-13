variable "name" {
  description = "Name prefix (e.g. ctm-prod)."
  type        = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "key_vault_name" {
  description = "Globally unique Key Vault name (3-24 alphanumerics and hyphens)."
  type        = string
}

variable "purge_protection_enabled" {
  description = "Enable Key Vault purge protection (recommended for prod; cannot be disabled once on)."
  type        = bool
  default     = false
}

variable "public_network_access_enabled" {
  description = "Allow the Key Vault data plane from public networks (restricted by network_acls). Set false when deploying from inside the VNet only."
  type        = bool
  default     = true
}

variable "allowed_ip_cidrs" {
  description = "CIDRs allowed through Key Vault network ACLs (deployment runners, admins)."
  type        = list(string)
  default     = []
}

variable "pe_subnet_id" {
  description = "Subnet for the Key Vault private endpoint (null to skip)."
  type        = string
  default     = null
}

variable "vault_private_dns_zone_id" {
  description = "privatelink.vaultcore.azure.net zone id for the private endpoint."
  type        = string
  default     = null
}

variable "secrets" {
  description = "Secrets to store: name => value. Values are sensitive."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
