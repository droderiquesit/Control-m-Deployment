# ---------------------------------------------------------------------------
# Bring-your-own network: everything here is a lookup of pre-existing
# networking. The only resources created are private DNS zones that the
# platform needs and that were not supplied (hub-managed zones are preferred).
#
# Requirements for the existing network (see docs/NETWORK-REQUIREMENTS.md):
#   app subnet  - outbound internet (NAT GW / firewall), NSG allowing intra-subnet
#                 traffic, 8443/443 from users, AzureLoadBalancer probes.
#   data subnet - delegated to Microsoft.DBforPostgreSQL/flexibleServers.
#   pe subnet   - private endpoint policies disabled (default).
# ---------------------------------------------------------------------------
data "azurerm_virtual_network" "this" {
  name                = var.vnet_name
  resource_group_name = var.vnet_resource_group_name
}

data "azurerm_subnet" "app" {
  name                 = var.app_subnet_name
  virtual_network_name = data.azurerm_virtual_network.this.name
  resource_group_name  = var.vnet_resource_group_name
}

data "azurerm_subnet" "data" {
  name                 = var.data_subnet_name
  virtual_network_name = data.azurerm_virtual_network.this.name
  resource_group_name  = var.vnet_resource_group_name
}

data "azurerm_subnet" "pe" {
  name                 = coalesce(var.pe_subnet_name, var.app_subnet_name)
  virtual_network_name = data.azurerm_virtual_network.this.name
  resource_group_name  = var.vnet_resource_group_name
}

locals {
  zone_names = {
    blob     = "privatelink.blob.core.windows.net"
    file     = "privatelink.file.core.windows.net"
    vault    = "privatelink.vaultcore.azure.net"
    postgres = "${var.postgres_dns_zone_prefix}.postgres.database.azure.com"
  }

  zones_to_create = { for k, v in local.zone_names : k => v if lookup(var.existing_private_dns_zone_ids, k, null) == null }

  zone_ids = { for k, v in local.zone_names : k =>
    lookup(var.existing_private_dns_zone_ids, k, null) != null ? var.existing_private_dns_zone_ids[k] : azurerm_private_dns_zone.this[k].id
  }
}

resource "azurerm_private_dns_zone" "this" {
  for_each            = local.zones_to_create
  name                = each.value
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each              = local.zones_to_create
  name                  = "${var.name}-${each.key}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.this[each.key].name
  virtual_network_id    = data.azurerm_virtual_network.this.id
  tags                  = var.tags
}
