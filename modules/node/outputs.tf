output "vm_id" {
  value = azurerm_linux_virtual_machine.this.id
}

output "vm_name" {
  value = azurerm_linux_virtual_machine.this.name
}

output "nic_id" {
  value = azurerm_network_interface.this.id
}

output "private_ip" {
  value = azurerm_network_interface.this.private_ip_address
}

output "os_disk_id" {
  value = data.azurerm_managed_disk.os.id
}

output "data_disk_id" {
  value = azurerm_managed_disk.data.id
}

output "zone" {
  value = var.zone
}
