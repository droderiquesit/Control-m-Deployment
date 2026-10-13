variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "internal" {
  description = "true = internal (private frontend in app subnet), false = public standard LB."
  type        = bool
  default     = true
}

variable "subnet_id" {
  description = "Subnet for the private frontend when internal = true."
  type        = string
  default     = null
}

variable "private_ip" {
  description = "Static private frontend IP (must be inside subnet_id). null = dynamic."
  type        = string
  default     = null
}

variable "public_dns_label" {
  description = "DNS label for the public IP when internal = false (used by Traffic Manager)."
  type        = string
  default     = null
}

variable "web_port" {
  description = "Control-M Web / Automation API HTTPS port on EM nodes."
  type        = number
  default     = 8443
}

variable "health_path" {
  description = "HTTPS path probed on EM nodes. Only the active EM answers; the standby fails the probe."
  type        = string
  default     = "/ControlM/"
}

variable "tags" {
  type    = map(string)
  default = {}
}
