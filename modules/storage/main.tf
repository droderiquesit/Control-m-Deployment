locals {
  ip_rules = [for c in var.allowed_ip_cidrs : replace(c, "/32", "")]
}

# ---------------- Blob account: installers + Control-M backups ----------------
resource "azurerm_storage_account" "blob" {
  name                            = var.blob_account_name
  resource_group_name             = var.resource_group_name
  location                        = var.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = var.blob_replication_type
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  tags                            = var.tags

  blob_properties {
    versioning_enabled  = var.enable_object_replication_source
    change_feed_enabled = var.enable_object_replication_source
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
    ip_rules       = local.ip_rules
  }
}

resource "azurerm_storage_container" "installers" {
  name                  = "installers"
  storage_account_id    = azurerm_storage_account.blob.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "backups" {
  name                  = "backups"
  storage_account_id    = azurerm_storage_account.blob.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "blob" {
  storage_account_id = azurerm_storage_account.blob.id

  rule {
    name    = "tier-and-expire-backups"
    enabled = true
    filters {
      prefix_match = ["backups/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 30
        tier_to_archive_after_days_since_modification_greater_than = 90
        delete_after_days_since_modification_greater_than          = 365
      }
      version {
        delete_after_days_since_creation = 60
      }
    }
  }
}

resource "azurerm_role_assignment" "nodes_blob_reader" {
  scope                = azurerm_storage_account.blob.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = var.node_identity_principal_id
}

# Nodes write EM/Server backups (util -U ... ) to the backups container.
resource "azurerm_role_assignment" "nodes_backups_writer" {
  scope                = azurerm_storage_container.backups.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.node_identity_principal_id
}

resource "azurerm_private_endpoint" "blob" {
  name                = "${var.name}-blob-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.pe_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.name}-blob"
    private_connection_resource_id = azurerm_storage_account.blob.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.blob_private_dns_zone_id]
  }
}

# ---------------- Premium Files (NFS 4.1): shared Control-M filesystem ----------------
resource "azurerm_storage_account" "files" {
  name                            = var.files_account_name
  resource_group_name             = var.resource_group_name
  location                        = var.location
  account_kind                    = "FileStorage"
  account_tier                    = "Premium"
  account_replication_type        = var.files_replication_type
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = false # required for NFS
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = false
  tags                            = var.tags

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }
}

resource "azurerm_storage_share" "shared" {
  name               = "ctm-shared"
  storage_account_id = azurerm_storage_account.files.id
  quota              = var.shared_share_quota_gb
  enabled_protocol   = "NFS"
}

resource "azurerm_private_endpoint" "files" {
  name                = "${var.name}-files-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.pe_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.name}-files"
    private_connection_resource_id = azurerm_storage_account.files.id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.file_private_dns_zone_id]
  }
}
