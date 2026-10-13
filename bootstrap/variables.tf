variable "subscription_id" {
  description = "Azure subscription that hosts the Terraform state."
  type        = string
}

variable "location" {
  description = "Azure region for the state resource group and storage account."
  type        = string
  default     = "eastus2"
}

variable "name_prefix" {
  description = "Prefix for the state resource group."
  type        = string
  default     = "ctm"
}

variable "state_storage_account_name" {
  description = "Globally unique storage account name for state (3-24 lowercase alphanumerics)."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.state_storage_account_name))
    error_message = "Storage account names must be 3-24 lowercase letters and digits."
  }
}

variable "tags" {
  description = "Tags applied to bootstrap resources."
  type        = map(string)
  default = {
    project   = "control-m"
    managedBy = "terraform"
  }
}
