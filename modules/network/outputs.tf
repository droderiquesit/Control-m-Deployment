output "vnet_id" {
  value = data.azurerm_virtual_network.this.id
}

output "vnet_name" {
  value = data.azurerm_virtual_network.this.name
}

output "vnet_resource_group_name" {
  value = var.vnet_resource_group_name
}

output "app_subnet_id" {
  value = data.azurerm_subnet.app.id
}

output "app_subnet_name" {
  value = data.azurerm_subnet.app.name
}

output "app_subnet_cidr" {
  value = data.azurerm_subnet.app.address_prefixes[0]
}

output "data_subnet_id" {
  value = data.azurerm_subnet.data.id
}

output "pe_subnet_id" {
  value = data.azurerm_subnet.pe.id
}

output "privatelink_zone_ids" {
  description = "Map blob/file/vault/postgres => private DNS zone id (existing or created)."
  value       = local.zone_ids
}

output "postgres_private_dns_zone_id" {
  value = local.zone_ids["postgres"]
}

output "postgres_dns_link_id" {
  description = "VNet link id when the postgres zone was created here (null when supplied)."
  value       = try(azurerm_private_dns_zone_virtual_network_link.this["postgres"].id, null)
}
