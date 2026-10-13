# ---------------------------------------------------------------------------
# QA: full HA pairs (EM + Server primary/secondary), zone-redundant DB, no DR.
# ---------------------------------------------------------------------------
subscription_id = "00000000-0000-0000-0000-000000000000"
environment     = "qa"
location        = "eastus2"
unique_suffix   = "q01"

tags = {
  costCenter = "scheduling"
  owner      = "platform-team"
}

network = {
  vnet_name                = "vnet-qa-eastus2"
  vnet_resource_group_name = "rg-network-qa"
  app_subnet_name          = "snet-ctm-app"
  data_subnet_name         = "snet-ctm-postgres"
  pe_subnet_name           = "snet-ctm-pe"
}

deployer_ip_cidrs = ["203.0.113.10/32"]

em = {
  vm_size          = "Standard_D4s_v5"
  data_disk_gb     = 256
  enable_secondary = true
}

server = {
  vm_size          = "Standard_D4s_v5"
  data_disk_gb     = 256
  enable_secondary = true
  server_name      = "CTM_QA"
}

agents = {
  vm_size  = "Standard_D2s_v5"
  min      = 1
  max      = 4
  default  = 2
  use_spot = true
}

database = {
  sku_name               = "GP_Standard_D2ds_v5"
  storage_mb             = 131072
  high_availability_mode = "ZoneRedundant"
  backup_retention_days  = 7
}

storage = {
  blob_replication_type  = "ZRS"
  files_replication_type = "ZRS"
}

backup = {
  enabled              = true
  storage_mode_type    = "ZoneRedundant"
  daily_retention_days = 14
}

observability = {
  log_retention_days = 30
  alert_emails       = ["ctm-qa-alerts@example.com"]
}

dr = { enabled = false }
