variable "name" {
  description = "VM name; also becomes the OS hostname Control-M is installed against (e.g. ctm-em-a)."
  type        = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "zone" {
  description = "Availability zone (1, 2 or 3)."
  type        = string
}

variable "subnet_id" {
  type = string
}

variable "private_ip" {
  description = "Optional static private IP. null = dynamic (Azure keeps it for the NIC lifetime)."
  type        = string
  default     = null
}

variable "vm_size" {
  type = string
}

variable "image" {
  description = "Marketplace image reference."
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
  description = "User-assigned identity attached to the VM."
  type        = string
}

variable "os_disk_gb" {
  type    = number
  default = 128
}

variable "data_disk_gb" {
  description = "Persistent data disk for the Control-M home/installation directory."
  type        = number
  default     = 256
}

variable "disk_type" {
  description = "Premium_LRS or Premium_ZRS (ZRS disks survive a zone loss and can be re-attached in another zone)."
  type        = string
  default     = "Premium_LRS"
}

variable "custom_data" {
  description = "cloud-init / shell bootstrap script (plain text; module base64-encodes)."
  type        = string
}

variable "lb_backend_pool_id" {
  description = "Load balancer backend pool to join (EM nodes). null to skip."
  type        = string
  default     = null
}

variable "data_collection_rule_id" {
  description = "Azure Monitor DCR to associate. null to skip."
  type        = string
  default     = null
}

variable "patch_mode" {
  type    = string
  default = "AutomaticByPlatform"
}

variable "tags" {
  type    = map(string)
  default = {}
}
