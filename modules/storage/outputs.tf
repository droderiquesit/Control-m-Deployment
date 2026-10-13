output "blob_account_id" {
  value = azurerm_storage_account.blob.id
}

output "blob_account_name" {
  value = azurerm_storage_account.blob.name
}

output "installers_container_name" {
  value = azurerm_storage_container.installers.name
}

output "backups_container_name" {
  value = azurerm_storage_container.backups.name
}

output "files_account_id" {
  value = azurerm_storage_account.files.id
}

output "files_account_name" {
  value = azurerm_storage_account.files.name
}

output "nfs_server" {
  value = "${azurerm_storage_account.files.name}.file.core.windows.net"
}

output "nfs_export_path" {
  value = "/${azurerm_storage_account.files.name}/${azurerm_storage_share.shared.name}"
}

output "share_name" {
  value = azurerm_storage_share.shared.name
}
