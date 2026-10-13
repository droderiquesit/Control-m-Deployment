resource "azurerm_linux_virtual_machine_scale_set" "this" {
  name                            = var.name
  computer_name_prefix            = "ctm-agent-"
  resource_group_name             = var.resource_group_name
  location                        = var.location
  sku                             = var.vm_size
  instances                       = var.default_instances
  admin_username                  = var.admin_username
  disable_password_authentication = true
  custom_data                     = base64encode(var.custom_data)
  zones                           = var.zones
  zone_balance                    = length(var.zones) > 1
  upgrade_mode                    = "Manual"
  overprovision                   = false
  single_placement_group          = false
  priority                        = var.use_spot ? "Spot" : "Regular"
  eviction_policy                 = var.use_spot ? "Delete" : null
  max_bid_price                   = var.use_spot ? -1 : null
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
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = var.os_disk_gb
  }

  source_image_reference {
    publisher = var.image.publisher
    offer     = var.image.offer
    sku       = var.image.sku
    version   = var.image.version
  }

  network_interface {
    name                          = "primary"
    primary                       = true
    enable_accelerated_networking = true

    ip_configuration {
      name      = "primary"
      primary   = true
      subnet_id = var.subnet_id
    }
  }

  # Scheduled Events "Terminate" is delivered to the VM so the agent can
  # deregister from Control-M/Server before the instance is removed.
  termination_notification {
    enabled = true
    timeout = "PT10M"
  }

  scale_in {
    rule                   = "OldestVM"
    force_deletion_enabled = false
  }

  extension {
    name                       = "AzureMonitorLinuxAgent"
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
  }

  boot_diagnostics {}

  lifecycle {
    # Autoscale owns the instance count after creation.
    ignore_changes = [instances, source_image_reference[0].version]
  }
}

resource "azurerm_monitor_data_collection_rule_association" "this" {
  count                   = var.data_collection_rule_id != null ? 1 : 0
  name                    = "${var.name}-dcra"
  target_resource_id      = azurerm_linux_virtual_machine_scale_set.this.id
  data_collection_rule_id = var.data_collection_rule_id
}

resource "azurerm_monitor_autoscale_setting" "this" {
  name                = "${var.name}-autoscale"
  resource_group_name = var.resource_group_name
  location            = var.location
  target_resource_id  = azurerm_linux_virtual_machine_scale_set.this.id
  enabled             = true
  tags                = var.tags

  profile {
    name = "cpu"

    capacity {
      default = var.default_instances
      minimum = var.min_instances
      maximum = var.max_instances
    }

    rule {
      metric_trigger {
        metric_name        = "Percentage CPU"
        metric_resource_id = azurerm_linux_virtual_machine_scale_set.this.id
        metric_namespace   = "microsoft.compute/virtualmachinescalesets"
        time_grain         = "PT1M"
        statistic          = "Average"
        time_window        = "PT5M"
        time_aggregation   = "Average"
        operator           = "GreaterThan"
        threshold          = var.scale_out_cpu_percent
      }
      scale_action {
        direction = "Increase"
        type      = "ChangeCount"
        value     = "1"
        cooldown  = "PT5M"
      }
    }

    rule {
      metric_trigger {
        metric_name        = "Percentage CPU"
        metric_resource_id = azurerm_linux_virtual_machine_scale_set.this.id
        metric_namespace   = "microsoft.compute/virtualmachinescalesets"
        time_grain         = "PT1M"
        statistic          = "Average"
        time_window        = "PT15M"
        time_aggregation   = "Average"
        operator           = "LessThan"
        threshold          = var.scale_in_cpu_percent
      }
      scale_action {
        direction = "Decrease"
        type      = "ChangeCount"
        value     = "1"
        cooldown  = "PT15M"
      }
    }
  }

}
