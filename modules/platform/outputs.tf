output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "resource_group_id" {
  value = azurerm_resource_group.this.id
}

output "location" {
  value = var.location
}

output "vnet_id" {
  value = module.network.vnet_id
}

output "app_subnet_id" {
  value = module.network.app_subnet_id
}

output "app_subnet_name" {
  value = module.network.app_subnet_name
}

output "data_subnet_id" {
  value = module.network.data_subnet_id
}

output "postgres_private_dns_zone_id" {
  value = module.network.postgres_private_dns_zone_id
}

output "postgres_dns_link_id" {
  value = module.network.postgres_dns_link_id
}

output "key_vault_name" {
  value = module.security.key_vault_name
}

output "key_vault_id" {
  value = module.security.key_vault_id
}

output "node_identity_id" {
  value = module.security.identity_id
}

output "blob_account_id" {
  value = module.storage.blob_account_id
}

output "blob_account_name" {
  value = module.storage.blob_account_name
}

output "installers_container" {
  value = module.storage.installers_container_name
}

output "files_account_id" {
  value = module.storage.files_account_id
}

output "nfs_mount" {
  value = "${module.storage.nfs_server}:${module.storage.nfs_export_path}"
}

output "database_server_id" {
  value = var.database.create ? module.database[0].server_id : null
}

output "database_fqdn" {
  value = local.pg_fqdn
}

output "database_alias" {
  description = "Stable DNS name nodes use for the database (CNAME to primary or promoted replica)."
  value       = local.db_alias
}

output "control_m_web_url" {
  value = "https://${local.web_alias}:${var.em.web_port}/ControlM/"
}

output "automation_api_endpoint" {
  value = "https://${local.web_alias}:${var.em.web_port}/automation-api"
}

output "load_balancer_id" {
  value = module.load_balancer.lb_id
}

output "load_balancer_frontend_ip" {
  value = module.load_balancer.frontend_ip
}

output "load_balancer_public_ip_id" {
  value = module.load_balancer.public_ip_id
}

output "load_balancer_backend_pool_id" {
  value = module.load_balancer.backend_pool_id
}

output "servers" {
  description = "Control-M/Servers on this platform keyed by stage: logical name, hosts, database, host group, agent scale set."
  value = { for k, s in local.servers : k => {
    name           = s.name
    primary_host   = s.primary_host
    secondary_host = s.secondary_host
    database       = s.db_name
    hostgroup      = s.hostgroup
    agent_vmss_id  = module.agent_pool[k].vmss_id
  } }
}

output "nodes" {
  description = "EM/Server VMs (for Site Recovery): name => ids and zone."
  value = merge(
    { for k, m in module.em_node : k => { vm_id = m.vm_id, nic_id = m.nic_id, os_disk_id = m.os_disk_id, data_disk_id = m.data_disk_id, zone = m.zone, private_ip = m.private_ip, component = "em" } },
    { for k, m in module.server_node : k => { vm_id = m.vm_id, nic_id = m.nic_id, os_disk_id = m.os_disk_id, data_disk_id = m.data_disk_id, zone = m.zone, private_ip = m.private_ip, component = "server" } },
  )
}

output "agent_vmss_ids" {
  value = { for k, m in module.agent_pool : k => m.vmss_id }
}

output "log_analytics_workspace_id" {
  value = module.observability.workspace_id
}

output "action_group_id" {
  value = module.observability.action_group_id
}

output "backup_vault_name" {
  value = var.backup.enabled ? module.backup[0].vault_name : null
}

output "private_dns_zone_name" {
  value = local.zone_name
}
