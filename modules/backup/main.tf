resource "azurerm_recovery_services_vault" "this" {
  name                         = "${var.name}-rsv"
  resource_group_name          = var.resource_group_name
  location                     = var.location
  sku                          = "Standard"
  storage_mode_type            = var.storage_mode_type
  cross_region_restore_enabled = var.storage_mode_type == "GeoRedundant" ? var.cross_region_restore_enabled : false
  tags                         = var.tags
}

resource "azurerm_backup_policy_vm" "daily" {
  name                           = "${var.name}-vm-daily"
  resource_group_name            = var.resource_group_name
  recovery_vault_name            = azurerm_recovery_services_vault.this.name
  policy_type                    = "V2"
  timezone                       = "UTC"
  instant_restore_retention_days = 5

  backup {
    frequency = "Daily"
    time      = var.backup_time_utc
  }

  retention_daily {
    count = var.daily_retention_days
  }

  retention_weekly {
    count    = var.weekly_retention_weeks
    weekdays = ["Sunday"]
  }
}

resource "azurerm_backup_protected_vm" "this" {
  for_each            = var.vm_ids
  resource_group_name = var.resource_group_name
  recovery_vault_name = azurerm_recovery_services_vault.this.name
  source_vm_id        = each.value
  backup_policy_id    = azurerm_backup_policy_vm.daily.id
}

# ---------------- Azure Files share backup ----------------
resource "azurerm_backup_container_storage_account" "files" {
  count               = var.files_storage_account_id != null ? 1 : 0
  resource_group_name = var.resource_group_name
  recovery_vault_name = azurerm_recovery_services_vault.this.name
  storage_account_id  = var.files_storage_account_id
}

resource "azurerm_backup_policy_file_share" "daily" {
  count               = var.files_storage_account_id != null ? 1 : 0
  name                = "${var.name}-files-daily"
  resource_group_name = var.resource_group_name
  recovery_vault_name = azurerm_recovery_services_vault.this.name
  timezone            = "UTC"

  backup {
    frequency = "Daily"
    time      = var.backup_time_utc
  }

  retention_daily {
    count = var.daily_retention_days
  }
}

resource "azurerm_backup_protected_file_share" "shared" {
  count                     = var.files_storage_account_id != null ? 1 : 0
  resource_group_name       = var.resource_group_name
  recovery_vault_name       = azurerm_recovery_services_vault.this.name
  source_storage_account_id = azurerm_backup_container_storage_account.files[0].storage_account_id
  source_file_share_name    = var.files_share_name
  backup_policy_id          = azurerm_backup_policy_file_share.daily[0].id
}
