# ---------------------------------------------------------------------------
# Cross-region disaster recovery for the Control-M platform.
#   1. PostgreSQL Flexible Server cross-region read replica (promote on failover)
#   2. Azure Site Recovery replication of EM/Server VMs (OS + data disks)
#   3. Blob object replication of installer media to the DR account
#   4. Optional Traffic Manager priority failover for a public web endpoint
# ---------------------------------------------------------------------------

# ---------------- 1. Database replica ----------------
resource "terraform_data" "dr_dns_link" {
  input = var.dr.postgres_dns_link_id
}

resource "azurerm_postgresql_flexible_server" "replica" {
  name                          = var.replica_name
  resource_group_name           = var.dr.resource_group_name
  location                      = var.dr.location
  create_mode                   = "Replica"
  source_server_id              = var.primary.database_server_id
  delegated_subnet_id           = var.dr.data_subnet_id
  private_dns_zone_id           = var.dr.postgres_private_dns_zone_id
  public_network_access_enabled = false
  sku_name                      = var.replica_sku_name
  zone                          = var.replica_zone
  tags                          = merge(var.tags, { "ctm:role" = "dr-replica" })

  lifecycle {
    # After promotion the server is standalone; keep Terraform from trying to re-create it.
    ignore_changes = [create_mode, source_server_id, zone, high_availability, storage_mb, backup_retention_days, geo_redundant_backup_enabled]
  }

  depends_on = [terraform_data.dr_dns_link]
}

# ---------------- 2. Site Recovery ----------------
resource "azurerm_recovery_services_vault" "asr" {
  count               = var.enable_site_recovery ? 1 : 0
  name                = "${var.name}-asr-rsv"
  resource_group_name = var.dr.resource_group_name
  location            = var.dr.location
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_site_recovery_fabric" "primary" {
  count               = var.enable_site_recovery ? 1 : 0
  name                = "${var.name}-fabric-primary"
  resource_group_name = var.dr.resource_group_name
  recovery_vault_name = azurerm_recovery_services_vault.asr[0].name
  location            = var.primary.location
}

resource "azurerm_site_recovery_fabric" "dr" {
  count               = var.enable_site_recovery ? 1 : 0
  name                = "${var.name}-fabric-dr"
  resource_group_name = var.dr.resource_group_name
  recovery_vault_name = azurerm_recovery_services_vault.asr[0].name
  location            = var.dr.location
}

resource "azurerm_site_recovery_protection_container" "primary" {
  count                = var.enable_site_recovery ? 1 : 0
  name                 = "${var.name}-container-primary"
  resource_group_name  = var.dr.resource_group_name
  recovery_vault_name  = azurerm_recovery_services_vault.asr[0].name
  recovery_fabric_name = azurerm_site_recovery_fabric.primary[0].name
}

resource "azurerm_site_recovery_protection_container" "dr" {
  count                = var.enable_site_recovery ? 1 : 0
  name                 = "${var.name}-container-dr"
  resource_group_name  = var.dr.resource_group_name
  recovery_vault_name  = azurerm_recovery_services_vault.asr[0].name
  recovery_fabric_name = azurerm_site_recovery_fabric.dr[0].name
}

resource "azurerm_site_recovery_replication_policy" "this" {
  count                                                = var.enable_site_recovery ? 1 : 0
  name                                                 = "${var.name}-asr-policy"
  resource_group_name                                  = var.dr.resource_group_name
  recovery_vault_name                                  = azurerm_recovery_services_vault.asr[0].name
  recovery_point_retention_in_minutes                  = var.recovery_point_retention_hours * 60
  application_consistent_snapshot_frequency_in_minutes = var.app_consistent_snapshot_minutes
}

resource "azurerm_site_recovery_protection_container_mapping" "this" {
  count                                     = var.enable_site_recovery ? 1 : 0
  name                                      = "${var.name}-container-mapping"
  resource_group_name                       = var.dr.resource_group_name
  recovery_vault_name                       = azurerm_recovery_services_vault.asr[0].name
  recovery_fabric_name                      = azurerm_site_recovery_fabric.primary[0].name
  recovery_source_protection_container_name = azurerm_site_recovery_protection_container.primary[0].name
  recovery_target_protection_container_id   = azurerm_site_recovery_protection_container.dr[0].id
  recovery_replication_policy_id            = azurerm_site_recovery_replication_policy.this[0].id
}

resource "azurerm_site_recovery_network_mapping" "this" {
  count                       = var.enable_site_recovery ? 1 : 0
  name                        = "${var.name}-network-mapping"
  resource_group_name         = var.dr.resource_group_name
  recovery_vault_name         = azurerm_recovery_services_vault.asr[0].name
  source_recovery_fabric_name = azurerm_site_recovery_fabric.primary[0].name
  target_recovery_fabric_name = azurerm_site_recovery_fabric.dr[0].name
  source_network_id           = var.primary.vnet_id
  target_network_id           = var.dr.vnet_id
}

# Replication cache account must live in the source region.
resource "azurerm_storage_account" "asr_cache" {
  count                           = var.enable_site_recovery ? 1 : 0
  name                            = substr("${replace(var.name, "-", "")}asrcache${substr(md5(var.primary.resource_group_name), 0, 6)}", 0, 24)
  resource_group_name             = var.primary.resource_group_name
  location                        = var.primary.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  tags                            = merge(var.tags, { "ctm:role" = "asr-cache" })
}

resource "azurerm_site_recovery_replicated_vm" "nodes" {
  for_each = var.enable_site_recovery ? var.primary.nodes : {}

  name                                      = "${each.key}-replication"
  resource_group_name                       = var.dr.resource_group_name
  recovery_vault_name                       = azurerm_recovery_services_vault.asr[0].name
  source_recovery_fabric_name               = azurerm_site_recovery_fabric.primary[0].name
  source_vm_id                              = each.value.vm_id
  recovery_replication_policy_id            = azurerm_site_recovery_replication_policy.this[0].id
  source_recovery_protection_container_name = azurerm_site_recovery_protection_container.primary[0].name
  target_resource_group_id                  = var.dr.resource_group_id
  target_recovery_fabric_id                 = azurerm_site_recovery_fabric.dr[0].id
  target_recovery_protection_container_id   = azurerm_site_recovery_protection_container.dr[0].id
  target_zone                               = each.value.zone
  target_network_id                         = var.dr.vnet_id

  managed_disk {
    disk_id                    = each.value.os_disk_id
    staging_storage_account_id = azurerm_storage_account.asr_cache[0].id
    target_resource_group_id   = var.dr.resource_group_id
    target_disk_type           = "Premium_LRS"
    target_replica_disk_type   = "Premium_LRS"
  }

  managed_disk {
    disk_id                    = each.value.data_disk_id
    staging_storage_account_id = azurerm_storage_account.asr_cache[0].id
    target_resource_group_id   = var.dr.resource_group_id
    target_disk_type           = "Premium_LRS"
    target_replica_disk_type   = "Premium_LRS"
  }

  network_interface {
    source_network_interface_id                     = each.value.nic_id
    target_subnet_name                              = var.dr.app_subnet_name
    recovery_load_balancer_backend_address_pool_ids = each.value.component == "em" ? [var.dr.lb_backend_pool_id] : []
  }

  depends_on = [
    azurerm_site_recovery_protection_container_mapping.this,
    azurerm_site_recovery_network_mapping.this,
  ]
}

# ---------------- 3. Installer media replication ----------------
resource "azurerm_storage_object_replication" "installers" {
  count                          = var.replicate_installers ? 1 : 0
  source_storage_account_id      = var.primary.blob_account_id
  destination_storage_account_id = var.dr.blob_account_id

  rules {
    source_container_name      = "installers"
    destination_container_name = "installers"
    copy_blobs_created_after   = "Everything"
  }

  rules {
    source_container_name      = "backups"
    destination_container_name = "backups"
    copy_blobs_created_after   = "Everything"
  }
}

# ---------------- 4. Traffic Manager (public endpoints only) ----------------
locals {
  tm_enabled = var.traffic_manager.enabled && var.primary.lb_public_ip_id != null && var.dr.lb_public_ip_id != null
}

resource "azurerm_traffic_manager_profile" "this" {
  count                  = local.tm_enabled ? 1 : 0
  name                   = "${var.name}-tm"
  resource_group_name    = var.primary.resource_group_name
  traffic_routing_method = "Priority"
  tags                   = var.tags

  dns_config {
    relative_name = coalesce(var.traffic_manager.relative_name, "${var.name}-web")
    ttl           = 30
  }

  monitor_config {
    protocol                     = "HTTPS"
    port                         = 443
    path                         = "/ControlM/"
    interval_in_seconds          = 30
    timeout_in_seconds           = 10
    tolerated_number_of_failures = 3
  }
}

resource "azurerm_traffic_manager_azure_endpoint" "primary" {
  count              = local.tm_enabled ? 1 : 0
  name               = "primary"
  profile_id         = azurerm_traffic_manager_profile.this[0].id
  target_resource_id = var.primary.lb_public_ip_id
  priority           = 1
  weight             = 1
}

resource "azurerm_traffic_manager_azure_endpoint" "dr" {
  count              = local.tm_enabled ? 1 : 0
  name               = "dr"
  profile_id         = azurerm_traffic_manager_profile.this[0].id
  target_resource_id = var.dr.lb_public_ip_id
  priority           = 2
  weight             = 1
}
