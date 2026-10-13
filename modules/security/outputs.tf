output "identity_id" {
  value = azurerm_user_assigned_identity.nodes.id
}

output "identity_principal_id" {
  value = azurerm_user_assigned_identity.nodes.principal_id
}

output "identity_client_id" {
  value = azurerm_user_assigned_identity.nodes.client_id
}

output "key_vault_id" {
  value = azurerm_key_vault.this.id
}

output "key_vault_name" {
  value = azurerm_key_vault.this.name
}

output "key_vault_uri" {
  value = azurerm_key_vault.this.vault_uri
}
