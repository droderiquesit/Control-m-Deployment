output "lb_id" {
  value = azurerm_lb.this.id
}

output "backend_pool_id" {
  value = azurerm_lb_backend_address_pool.em.id
}

output "frontend_ip" {
  description = "Private frontend IP (internal) or public IP address."
  value       = var.internal ? azurerm_lb.this.frontend_ip_configuration[0].private_ip_address : azurerm_public_ip.this[0].ip_address
}

output "public_ip_id" {
  value = var.internal ? null : azurerm_public_ip.this[0].id
}

output "public_fqdn" {
  value = var.internal ? null : azurerm_public_ip.this[0].fqdn
}
