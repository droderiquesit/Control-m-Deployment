data "azurerm_client_config" "current" {}

# ---------------- Managed identity used by every Control-M VM ----------------
resource "azurerm_user_assigned_identity" "nodes" {
  name                = "${var.name}-nodes-id"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# ---------------- Key Vault ----------------
resource "azurerm_key_vault" "this" {
  name                          = var.key_vault_name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = var.purge_protection_enabled
  soft_delete_retention_days    = 30
  public_network_access_enabled = var.public_network_access_enabled
  tags                          = var.tags

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.allowed_ip_cidrs
  }
}

# Deployer needs data-plane rights to write secrets.
resource "azurerm_role_assignment" "deployer_secrets_officer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# Nodes read secrets at boot (DB credentials, EM admin password, SSH key).
resource "azurerm_role_assignment" "nodes_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.nodes.principal_id
}

resource "azurerm_key_vault_secret" "this" {
  for_each     = nonsensitive(toset(keys(var.secrets)))
  name         = each.key
  value        = var.secrets[each.key]
  key_vault_id = azurerm_key_vault.this.id
  content_type = "text/plain"
  tags         = var.tags

  depends_on = [azurerm_role_assignment.deployer_secrets_officer]
}

# ---------------- Private endpoint (optional) ----------------
resource "azurerm_private_endpoint" "vault" {
  count               = var.pe_subnet_id != null ? 1 : 0
  name                = "${var.name}-kv-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.pe_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.name}-kv"
    private_connection_resource_id = azurerm_key_vault.this.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.vault_private_dns_zone_id]
  }
}
