resource "azurerm_network_interface" "this" {
  name                           = "${var.name}-nic"
  resource_group_name            = var.resource_group_name
  location                       = var.location
  accelerated_networking_enabled = true
  tags                           = var.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = var.private_ip != null ? "Static" : "Dynamic"
    private_ip_address            = var.private_ip
  }
}

resource "azurerm_network_interface_backend_address_pool_association" "lb" {
  count                   = var.lb_backend_pool_id != null ? 1 : 0
  network_interface_id    = azurerm_network_interface.this.id
  ip_configuration_name   = "primary"
  backend_address_pool_id = var.lb_backend_pool_id
}

resource "azurerm_linux_virtual_machine" "this" {
  name                            = var.name
  computer_name                   = var.name
  resource_group_name             = var.resource_group_name
  location                        = var.location
  zone                            = var.zone
  size                            = var.vm_size
  admin_username                  = var.admin_username
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.this.id]
  custom_data                     = base64encode(var.custom_data)
  patch_mode                      = var.patch_mode
  patch_assessment_mode           = "AutomaticByPlatform"
  provision_vm_agent              = true
  tags                            = var.tags

  admin_ssh_key {
    username   = var.admin_username
    public_key = var.ssh_public_key
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  os_disk {
    name                 = "${var.name}-osdisk"
    caching              = "ReadWrite"
    storage_account_type = var.disk_type
    disk_size_gb         = var.os_disk_gb
  }

  source_image_reference {
    publisher = var.image.publisher
    offer     = var.image.offer
    sku       = var.image.sku
    version   = var.image.version
  }

  boot_diagnostics {}

  lifecycle {
    # Re-running bootstrap on an installed Control-M node is destructive; change via re-provision.
    ignore_changes = [custom_data, source_image_reference[0].version]
  }
}

resource "azurerm_managed_disk" "data" {
  name                 = "${var.name}-data"
  resource_group_name  = var.resource_group_name
  location             = var.location
  zone                 = var.disk_type == "Premium_ZRS" ? null : var.zone
  storage_account_type = var.disk_type
  create_option        = "Empty"
  disk_size_gb         = var.data_disk_gb
  tags                 = merge(var.tags, { "ctm:role" = "data" })
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  managed_disk_id    = azurerm_managed_disk.data.id
  virtual_machine_id = azurerm_linux_virtual_machine.this.id
  lun                = 0
  caching            = "None"
}

# Resolve the OS disk id (needed by Site Recovery replication).
data "azurerm_managed_disk" "os" {
  name                = azurerm_linux_virtual_machine.this.os_disk[0].name
  resource_group_name = var.resource_group_name
}

# Azure Monitor Agent (syslog + perf -> Log Analytics through the DCR)
resource "azurerm_virtual_machine_extension" "ama" {
  name                       = "AzureMonitorLinuxAgent"
  virtual_machine_id         = azurerm_linux_virtual_machine.this.id
  publisher                  = "Microsoft.Azure.Monitor"
  type                       = "AzureMonitorLinuxAgent"
  type_handler_version       = "1.0"
  auto_upgrade_minor_version = true
  automatic_upgrade_enabled  = true
  settings = jsonencode({
    authentication = {
      managedIdentity = {
        "identifier-name"  = "mi_res_id"
        "identifier-value" = var.identity_id
      }
    }
  })
  tags = var.tags
}

resource "azurerm_monitor_data_collection_rule_association" "this" {
  count                   = var.data_collection_rule_id != null ? 1 : 0
  name                    = "${var.name}-dcra"
  target_resource_id      = azurerm_linux_virtual_machine.this.id
  data_collection_rule_id = var.data_collection_rule_id
}
