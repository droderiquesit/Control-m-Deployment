variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alert_emails" {
  description = "Email recipients for platform alerts."
  type        = list(string)
  default     = []
}

variable "load_balancer_id" {
  description = "LB whose health-probe availability is alerted on (null to skip)."
  type        = string
  default     = null
}

variable "postgres_server_id" {
  description = "PostgreSQL Flexible Server to alert on (null to skip)."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
