resource "azurerm_private_dns_zone" "this" {
  name                = var.zone_name
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# registration_enabled => every VM in the VNet gets an A record named after its hostname.
resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  name                  = "platform-vnet"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.this.name
  virtual_network_id    = var.vnet_id
  registration_enabled  = true
  tags                  = var.tags
}

resource "azurerm_private_dns_a_record" "static" {
  for_each            = var.records
  name                = each.key
  zone_name           = azurerm_private_dns_zone.this.name
  resource_group_name = var.resource_group_name
  ttl                 = 60
  records             = [each.value]
  tags                = var.tags
}

resource "azurerm_private_dns_cname_record" "static" {
  for_each            = var.cname_records
  name                = each.key
  zone_name           = azurerm_private_dns_zone.this.name
  resource_group_name = var.resource_group_name
  ttl                 = 60
  record              = each.value
  tags                = var.tags
}
