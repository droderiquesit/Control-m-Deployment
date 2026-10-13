variable "zone_name" {
  description = "Private DNS zone name for the platform (e.g. prod.ctm.internal)."
  type        = string
}

variable "resource_group_name" {
  type = string
}

variable "vnet_id" {
  description = "VNet to link with auto-registration so VM hostnames resolve automatically."
  type        = string
}

variable "records" {
  description = "Static A records to create: name => IPv4 address."
  type        = map(string)
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "cname_records" {
  description = "CNAME records to create: name => target FQDN (e.g. ctm-db => <server>.postgres.database.azure.com)."
  type        = map(string)
  default     = {}
}
