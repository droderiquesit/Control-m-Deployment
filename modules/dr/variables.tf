variable "name" {
  description = "Name prefix, e.g. ctm-prod."
  type        = string
}

variable "primary" {
  description = "Outputs of the primary platform."
  type = object({
    resource_group_name = string
    location            = string
    vnet_id             = string
    database_server_id  = string
    blob_account_id     = string
    lb_public_ip_id     = optional(string)
    nodes = map(object({
      vm_id        = string
      nic_id       = string
      os_disk_id   = string
      data_disk_id = string
      zone         = string
      component    = string
    }))
  })
}

variable "dr" {
  description = "Outputs of the DR platform."
  type = object({
    resource_group_name          = string
    resource_group_id            = string
    location                     = string
    vnet_id                      = string
    app_subnet_name              = string
    data_subnet_id               = string
    postgres_private_dns_zone_id = string
    postgres_dns_link_id         = optional(string)
    blob_account_id              = string
    lb_public_ip_id              = optional(string)
    lb_backend_pool_id           = string
  })
}

variable "replica_name" {
  description = "Name of the cross-region PostgreSQL read replica (must match the host the DR platform was given)."
  type        = string
}

variable "replica_sku_name" {
  description = "Replica compute SKU; keep >= primary so it can take production load after promotion."
  type        = string
}

variable "replica_zone" {
  type    = string
  default = "1"
}

variable "enable_site_recovery" {
  description = "Replicate EM/Server VM disks to the DR region with Azure Site Recovery."
  type        = bool
  default     = true
}

variable "recovery_point_retention_hours" {
  type    = number
  default = 24
}

variable "app_consistent_snapshot_minutes" {
  type    = number
  default = 60
}

variable "replicate_installers" {
  description = "Object-replicate the installers container to the DR blob account."
  type        = bool
  default     = true
}

variable "traffic_manager" {
  description = "Global DNS failover for a PUBLIC Control-M Web endpoint (both LBs must be public)."
  type = object({
    enabled       = optional(bool, false)
    relative_name = optional(string)
  })
  default = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
