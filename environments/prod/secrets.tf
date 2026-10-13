# Platform secrets are written to the Key Vault of every region (primary + DR)
# so a failed-over node finds identical values. By default they are generated
# here; a CI pipeline can override any of them from an external vault (Delinea
# DevOps Secrets Vault) through TF_VAR_platform_secrets, see docs/AZURE-DEVOPS.md.
# Special characters in generated values are limited to ones safe in XML and sed.
variable "platform_secrets" {
  description = "Optional overrides from an external vault: db-admin-password, em-db-password, ctm-db-password, em-admin-password, ssh-private-key."
  type        = map(string)
  sensitive   = true
  default     = {}
  nullable    = false
}

resource "random_password" "db_admin" {
  length           = 32
  special          = true
  override_special = "!#%^*-_=+"
}

resource "random_password" "em_db" {
  length           = 32
  special          = true
  override_special = "!#%^*-_=+"
}

resource "random_password" "ctm_db" {
  length           = 32
  special          = true
  override_special = "!#%^*-_=+"
}

resource "random_password" "em_admin" {
  length           = 24
  special          = true
  override_special = "!#%^*-_=+"
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

locals {
  generated_secrets = {
    "db-admin-password" = random_password.db_admin.result
    "em-db-password"    = random_password.em_db.result
    "ctm-db-password"   = random_password.ctm_db.result
    "em-admin-password" = random_password.em_admin.result
    "ssh-private-key"   = tls_private_key.ssh.private_key_pem
  }

  # External values win; anything not supplied falls back to the generated value.
  secrets = merge(local.generated_secrets, { for k, v in var.platform_secrets : k => v if v != "" })
}
