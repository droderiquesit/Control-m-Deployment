output "control_m_web_url" {
  value = module.platform.control_m_web_url
}

output "automation_api_endpoint" {
  value = module.platform.automation_api_endpoint
}

output "key_vault_name" {
  description = "Vault holding db/em passwords and the SSH private key (secret: ssh-private-key)."
  value       = module.platform.key_vault_name
}

output "installers_upload_target" {
  description = "Upload BMC media + silent XML here before the VMs boot (az storage blob upload --auth-mode login)."
  value       = "https://${module.platform.blob_account_name}.blob.core.windows.net/${module.platform.installers_container}"
}

output "nodes" {
  value = module.platform.nodes
}

output "database_alias" {
  value = module.platform.database_alias
}

output "database_fqdn" {
  value = module.platform.database_fqdn
}

output "nfs_mount" {
  value = module.platform.nfs_mount
}

output "ctm_server_name" {
  value = module.platform.ctm_server_name
}

output "dr" {
  value = var.dr.enabled ? {
    resource_group       = module.platform_dr[0].resource_group_name
    replica_fqdn         = module.dr[0].replica_fqdn
    site_recovery_vault  = module.dr[0].site_recovery_vault_name
    replicated_vms       = module.dr[0].replicated_vm_names
    traffic_manager_fqdn = module.dr[0].traffic_manager_fqdn
    key_vault_name       = module.platform_dr[0].key_vault_name
  } : null
}
