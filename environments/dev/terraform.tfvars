# ---------------------------------------------------------------------------
# DEV: single EM + single Server, burstable DB, spot agents, no DR.
# Cheapest footprint that still exercises the real install path.
# ---------------------------------------------------------------------------
subscription_id = "00000000-0000-0000-0000-000000000000"
environment     = "dev"
location        = "eastus2"
unique_suffix   = "d01"

tags = {
  costCenter = "scheduling"
  owner      = "platform-team"
}

# Existing network (select, not create)
network = {
  vnet_name                = "vnet-dev-eastus2"
  vnet_resource_group_name = "rg-network-dev"
  app_subnet_name          = "snet-ctm-app"
  data_subnet_name         = "snet-ctm-postgres"
  pe_subnet_name           = "snet-ctm-pe"
  # existing_private_dns_zone_ids = { blob = "...", file = "...", vault = "...", postgres = "..." }
}

deployer_ip_cidrs = ["203.0.113.10/32"]

em = {
  vm_size          = "Standard_D4s_v5"
  data_disk_gb     = 128
  enable_secondary = false
  zones            = ["1", "2"]
}

server = {
  vm_size          = "Standard_D4s_v5"
  data_disk_gb     = 128
  enable_secondary = false
  server_name      = "CTM_DEV"
}

agents = {
  vm_size  = "Standard_D2s_v5"
  min      = 1
  max      = 2
  default  = 1
  use_spot = true
}

database = {
  sku_name               = "B_Standard_B2ms"
  storage_mb             = 65536
  high_availability_mode = "Disabled"
  backup_retention_days  = 7
  geo_redundant_backup   = false
}

storage = {
  blob_replication_type  = "LRS"
  files_replication_type = "LRS"
  shared_share_quota_gb  = 128
}

backup = {
  enabled              = true
  storage_mode_type    = "LocallyRedundant"
  daily_retention_days = 7
  include_files_share  = false
}

observability = {
  log_retention_days = 14
  alert_emails       = ["ctm-dev-alerts@example.com"]
}

dr = { enabled = false }
