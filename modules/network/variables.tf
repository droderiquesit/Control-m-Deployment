variable "name" {
  description = "Name prefix for the few network resources this module may create (private DNS zones)."
  type        = string
}

variable "resource_group_name" {
  description = "Platform resource group that receives any private DNS zones created here."
  type        = string
}

variable "vnet_name" {
  description = "Name of the EXISTING virtual network to deploy into."
  type        = string
}

variable "vnet_resource_group_name" {
  description = "Resource group of the existing virtual network."
  type        = string
}

variable "app_subnet_name" {
  description = "Existing subnet for EM/Server VMs, the agent scale set and the load balancer frontend."
  type        = string
}

variable "data_subnet_name" {
  description = "Existing subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers."
  type        = string
}

variable "pe_subnet_name" {
  description = "Existing subnet for private endpoints (Key Vault, Blob, Files). Defaults to the app subnet."
  type        = string
  default     = null
}

variable "existing_private_dns_zone_ids" {
  description = <<-EOT
    Ids of private DNS zones that already exist (typically in a hub) and are linked to the VNet.
    Keys: blob (privatelink.blob.core.windows.net), file (privatelink.file.core.windows.net),
    vault (privatelink.vaultcore.azure.net), postgres (<x>.postgres.database.azure.com).
    Any key left null is created in the platform resource group and linked to the VNet.
  EOT
  type = object({
    blob     = optional(string)
    file     = optional(string)
    vault    = optional(string)
    postgres = optional(string)
  })
  default = {}
}

variable "postgres_dns_zone_prefix" {
  description = "Prefix for a postgres private DNS zone created here (<prefix>.postgres.database.azure.com)."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
