variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "zones" {
  description = "Zones the scale set balances across."
  type        = list(string)
  default     = ["1", "2", "3"]
}

variable "vm_size" {
  type = string
}

variable "image" {
  type = object({
    publisher = string
    offer     = string
    sku       = string
    version   = string
  })
}

variable "admin_username" {
  type    = string
  default = "ctmops"
}

variable "ssh_public_key" {
  type = string
}

variable "identity_id" {
  type = string
}

variable "custom_data" {
  type = string
}

variable "min_instances" {
  type = number
}

variable "max_instances" {
  type = number
}

variable "default_instances" {
  description = "Capacity when no autoscale rule applies (also the initial size)."
  type        = number
}

variable "scale_out_cpu_percent" {
  type    = number
  default = 70
}

variable "scale_in_cpu_percent" {
  type    = number
  default = 25
}

variable "use_spot" {
  description = "Run agents as Spot VMs (dev/qa cost saver). Spot VMs may be evicted; agents deregister gracefully."
  type        = bool
  default     = false
}

variable "os_disk_gb" {
  type    = number
  default = 64
}

variable "data_collection_rule_id" {
  type    = string
  default = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
