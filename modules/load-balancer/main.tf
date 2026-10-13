resource "azurerm_public_ip" "this" {
  count               = var.internal ? 0 : 1
  name                = "${var.name}-lb-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
  domain_name_label   = var.public_dns_label
  tags                = var.tags
}

resource "azurerm_lb" "this" {
  name                = "${var.name}-lb"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"
  sku_tier            = "Regional"
  tags                = var.tags

  frontend_ip_configuration {
    name                          = "frontend"
    subnet_id                     = var.internal ? var.subnet_id : null
    private_ip_address            = var.internal ? var.private_ip : null
    private_ip_address_allocation = var.internal ? (var.private_ip != null ? "Static" : "Dynamic") : null
    public_ip_address_id          = var.internal ? null : azurerm_public_ip.this[0].id
    zones                         = var.internal ? ["1", "2", "3"] : null
  }
}

resource "azurerm_lb_backend_address_pool" "em" {
  name            = "em-web"
  loadbalancer_id = azurerm_lb.this.id
}

resource "azurerm_lb_probe" "em_web" {
  name                = "em-web-https"
  loadbalancer_id     = azurerm_lb.this.id
  protocol            = "Https"
  port                = var.web_port
  request_path        = var.health_path
  interval_in_seconds = 10
  number_of_probes    = 3
}

resource "azurerm_lb_rule" "web" {
  for_each                       = { "443" = 443, "${var.web_port}" = var.web_port }
  name                           = "em-web-${each.key}"
  loadbalancer_id                = azurerm_lb.this.id
  protocol                       = "Tcp"
  frontend_port                  = each.value
  backend_port                   = var.web_port
  frontend_ip_configuration_name = "frontend"
  backend_address_pool_ids       = [azurerm_lb_backend_address_pool.em.id]
  probe_id                       = azurerm_lb_probe.em_web.id
  idle_timeout_in_minutes        = 30
  tcp_reset_enabled              = true
  disable_outbound_snat          = true
}
