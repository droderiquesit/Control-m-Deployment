# ---------------------------------------------------------------------------
# NONPROD platform: one Enterprise Manager (single node) managing two
# Control-M/Servers, CTM_DEV and CTM_QA, each with its own agent pool.
# Zone-redundant database so QA can rehearse HA; no DR.
# ---------------------------------------------------------------------------
subscription_id = "00000000-0000-0000-0000-000000000000"
environment     = "nonprod"
location        = "eastus2"
unique_suffix   = "n01"

tags = {
  costCenter = "scheduling"
  owner      = "platform-team"
}

# Existing network (select, not create)
network = {
  vnet_name                = "vnet-nonprod-eastus2"
  vnet_resource_group_name = "rg-network-nonprod"
  app_subnet_name          = "snet-ctm-app"
  data_subnet_name         = "snet-ctm-postgres"
  pe_subnet_name           = "snet-ctm-pe"
  # existing_private_dns_zone_ids = { blob = "...", file = "...", vault = "...", postgres = "..." }
}

deployer_ip_cidrs = ["203.0.113.10/32"]

em = {
  vm_size          = "Standard_D4s_v5"
  data_disk_gb     = 256
  enable_secondary = false # single EM is enough for nonprod; flip to true to rehearse EM failover
}

servers = {
  dev = {
    name             = "CTM_DEV"
    vm_size          = "Standard_D4s_v5"
    data_disk_gb     = 128
    enable_secondary = false
    agents = {
      vm_size  = "Standard_D2s_v5"
      min      = 1
      max      = 2
      default  = 1
      use_spot = true
    }
  }
  qa = {
    name             = "CTM_QA"
    vm_size          = "Standard_D4s_v5"
    data_disk_gb     = 256
    enable_secondary = true # Server HA pair, so failover is tested before prod
    agents = {
      vm_size  = "Standard_D2s_v5"
      min      = 1
      max      = 4
      default  = 2
      use_spot = true
    }
  }
}

database = {
  sku_name               = "GP_Standard_D2ds_v5"
  storage_mb             = 131072
  high_availability_mode = "ZoneRedundant"
  backup_retention_days  = 7
  geo_redundant_backup   = false
}

storage = {
  blob_replication_type  = "ZRS"
  files_replication_type = "ZRS"
  shared_share_quota_gb  = 256
}

backup = {
  enabled              = true
  storage_mode_type    = "ZoneRedundant"
  daily_retention_days = 14
  include_files_share  = true
}

observability = {
  log_retention_days = 30
  alert_emails       = ["ctm-nonprod-alerts@example.com"]
}

dr = { enabled = false }
