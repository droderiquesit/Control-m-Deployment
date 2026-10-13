# Platform secrets are generated once here and written to the Key Vault of
# every region (primary + DR) so a failed-over node finds identical values.
# Special characters are limited to ones that are safe in XML and sed.
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
  secrets = {
    "db-admin-password" = random_password.db_admin.result
    "em-db-password"    = random_password.em_db.result
    "ctm-db-password"   = random_password.ctm_db.result
    "em-admin-password" = random_password.em_admin.result
    "ssh-private-key"   = tls_private_key.ssh.private_key_pem
  }
}
