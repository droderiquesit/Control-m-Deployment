# ---------------------------------------------------------------------------
# UAT: production-like. HA pairs, zone-redundant DB with geo backups,
# pilot-light DR (Site Recovery + DB replica, no warm agents).
# ---------------------------------------------------------------------------
subscription_id = "00000000-0000-0000-0000-000000000000"
environment     = "uat"
location        = "eastus2"
unique_suffix   = "u01"

tags = {
  costCenter = "scheduling"
  owner      = "platform-team"
}

network = {
  vnet_name                = "vnet-uat-eastus2"
  vnet_resource_group_name = "rg-network-uat"
  app_subnet_name          = "snet-ctm-app"
  data_subnet_name         = "snet-ctm-postgres"
  pe_subnet_name           = "snet-ctm-pe"
}

deployer_ip_cidrs = ["203.0.113.10/32"]

em = {
  vm_size          = "Standard_D8s_v5"
  data_disk_gb     = 512
  disk_type        = "Premium_ZRS"
  enable_secondary = true
}

server = {
  vm_size          = "Standard_D8s_v5"
  data_disk_gb     = 512
  disk_type        = "Premium_ZRS"
  enable_secondary = true
  server_name      = "CTM_UAT"
}

agents = {
  vm_size = "Standard_D4s_v5"
  min     = 2
  max     = 6
  default = 2
}

database = {
  sku_name               = "GP_Standard_D4ds_v5"
  storage_mb             = 262144
  high_availability_mode = "ZoneRedundant"
  backup_retention_days  = 14
  geo_redundant_backup   = true
}

backup = {
  enabled              = true
  storage_mode_type    = "GeoRedundant"
  cross_region_restore = true
  daily_retention_days = 30
}

observability = {
  log_retention_days = 60
  alert_emails       = ["ctm-uat-alerts@example.com"]
}

dr = {
  enabled  = true
  location = "centralus"
  network = {
    vnet_name                = "vnet-uat-centralus"
    vnet_resource_group_name = "rg-network-uat-dr"
    app_subnet_name          = "snet-ctm-app"
    data_subnet_name         = "snet-ctm-postgres"
    pe_subnet_name           = "snet-ctm-pe"
  }
  warm_agents          = 0
  enable_site_recovery = true
}
