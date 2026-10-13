# ---------------------------------------------------------------------------
# PROD platform: Enterprise Manager HA pair managing two Control-M/Servers,
# CTM_UAT and CTM_PROD. UAT runs on production infrastructure (same EM,
# same database host, same DR) so what is accepted is what goes live.
# Zone-redundant everything, 35-day geo backups, Site Recovery + DB replica
# in the paired region, resource locks and purge protection.
# ---------------------------------------------------------------------------
subscription_id = "00000000-0000-0000-0000-000000000000"
environment     = "prod"
location        = "eastus2"
unique_suffix   = "p01"
resource_lock   = true

tags = {
  costCenter  = "scheduling"
  owner       = "platform-team"
  criticality = "tier-1"
}

network = {
  vnet_name                = "vnet-prod-eastus2"
  vnet_resource_group_name = "rg-network-prod"
  app_subnet_name          = "snet-ctm-app"
  data_subnet_name         = "snet-ctm-postgres"
  pe_subnet_name           = "snet-ctm-pe"
  # Hub-managed zones (recommended in prod):
  # existing_private_dns_zone_ids = {
  #   blob  = "/subscriptions/.../privateDnsZones/privatelink.blob.core.windows.net"
  #   file  = "/subscriptions/.../privateDnsZones/privatelink.file.core.windows.net"
  #   vault = "/subscriptions/.../privateDnsZones/privatelink.vaultcore.azure.net"
  # }
}

deployer_ip_cidrs       = ["203.0.113.10/32"]
key_vault_public_access = true

em = {
  vm_size          = "Standard_D16s_v5"
  data_disk_gb     = 1024
  disk_type        = "Premium_ZRS"
  enable_secondary = true
}

servers = {
  uat = {
    name             = "CTM_UAT"
    vm_size          = "Standard_D8s_v5"
    data_disk_gb     = 512
    disk_type        = "Premium_ZRS"
    enable_secondary = true
    agents = {
      vm_size = "Standard_D4s_v5"
      min     = 2
      max     = 6
      default = 2
    }
  }
  prod = {
    name             = "CTM_PROD"
    vm_size          = "Standard_D16s_v5"
    data_disk_gb     = 1024
    disk_type        = "Premium_ZRS"
    enable_secondary = true
    agents = {
      vm_size       = "Standard_D4s_v5"
      min           = 3
      max           = 20
      default       = 3
      scale_out_cpu = 65
      scale_in_cpu  = 20
    }
  }
}

database = {
  sku_name               = "GP_Standard_D8ds_v5"
  storage_mb             = 524288
  high_availability_mode = "ZoneRedundant"
  backup_retention_days  = 35
  geo_redundant_backup   = true
}

storage = {
  blob_replication_type  = "RAGZRS"
  files_replication_type = "ZRS"
  shared_share_quota_gb  = 512
}

backup = {
  enabled                = true
  storage_mode_type      = "GeoRedundant"
  cross_region_restore   = true
  daily_retention_days   = 35
  weekly_retention_weeks = 52
}

observability = {
  log_retention_days = 90
  alert_emails       = ["ctm-prod-alerts@example.com", "noc@example.com"]
}

dr = {
  enabled  = true
  location = "centralus"
  network = {
    vnet_name                = "vnet-prod-centralus"
    vnet_resource_group_name = "rg-network-prod-dr"
    app_subnet_name          = "snet-ctm-app"
    data_subnet_name         = "snet-ctm-postgres"
    pe_subnet_name           = "snet-ctm-pe"
  }
  warm_agents          = 1 # per Server (uat + prod) in the DR region
  replica_sku_name     = "GP_Standard_D8ds_v5"
  enable_site_recovery = true
}
