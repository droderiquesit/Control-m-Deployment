output "replica_server_id" {
  value = azurerm_postgresql_flexible_server.replica.id
}

output "replica_fqdn" {
  value = azurerm_postgresql_flexible_server.replica.fqdn
}

output "site_recovery_vault_name" {
  value = var.enable_site_recovery ? azurerm_recovery_services_vault.asr[0].name : null
}

output "replicated_vm_names" {
  value = [for k, v in azurerm_site_recovery_replicated_vm.nodes : v.name]
}

output "traffic_manager_fqdn" {
  value = local.tm_enabled ? azurerm_traffic_manager_profile.this[0].fqdn : null
}
