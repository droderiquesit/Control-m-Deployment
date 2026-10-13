# ---------------------------------------------------------------------------
# Control-M platform composition for ONE region.
#   role = primary : one EM pair, N Control-M/Server pairs (one per stage hosted on
#                    this platform), an agent scale set per Server, PostgreSQL
#                    Flexible Server (zone-redundant HA), shared NFS, Key Vault,
#                    LB, monitoring, backup.
#   role = dr      : same foundation without DB/VMs. The dr module adds a
#                    cross-region PostgreSQL replica and Site Recovery for the VMs.
# ---------------------------------------------------------------------------
data "azurerm_client_config" "current" {}

locals {
  name    = "${var.name_prefix}-${var.environment}${var.role == "dr" ? "-dr" : ""}"
  compact = replace(local.name, "-", "")

  tags = merge(var.tags, {
    environment = var.environment
    platform    = "control-m"
    role        = var.role
    managedBy   = "terraform"
  })

  zone_name = var.private_dns_zone_name
  db_alias  = "ctm-db.${local.zone_name}"
  web_alias = "ctm-web.${local.zone_name}"

  em_primary_host   = "ctm-em-a"
  em_secondary_host = "ctm-em-b"
  data_mount        = "/ctm"
  deploy_nodes      = var.role == "primary"

  # Per-Server derived settings (hostnames, database, secret name, host group).
  servers = { for k, s in var.servers : k => merge(s, {
    name           = coalesce(s.name, upper("CTM_${k}"))
    db_name        = "ctm_${k}"
    db_user        = "ctm_${k}"
    secret_name    = "ctm-db-password-${k}"
    hostgroup      = coalesce(s.agents.hostgroup, "${k}-agents")
    primary_host   = "ctm-srv-${k}-a"
    secondary_host = s.enable_secondary ? "ctm-srv-${k}-b" : ""
  }) }

  em_nodes = local.deploy_nodes && var.em.deploy ? merge(
    { (local.em_primary_host) = { role = "primary", zone = var.em.zones[0] } },
    var.em.enable_secondary ? { (local.em_secondary_host) = { role = "secondary", zone = var.em.zones[1] } } : {}
  ) : {}

  server_nodes = local.deploy_nodes ? merge([for k, s in local.servers : merge(
    { (s.primary_host) = { server = k, role = "primary", zone = s.zones[0] } },
    s.enable_secondary ? { (s.secondary_host) = { server = k, role = "secondary", zone = s.zones[1] } } : {}
  )]...) : {}

  pg_fqdn = var.database.create ? module.database[0].fqdn : var.database.host_override
}

resource "azurerm_resource_group" "this" {
  name     = "${local.name}-rg"
  location = var.location
  tags     = local.tags
}

resource "azurerm_management_lock" "rg" {
  count      = var.resource_lock ? 1 : 0
  name       = "${local.name}-no-delete"
  scope      = azurerm_resource_group.this.id
  lock_level = "CanNotDelete"
  notes      = "Control-M ${var.environment} platform (${var.role})."
}

# ---------------- Existing network ----------------
module "network" {
  source = "../network"

  name                          = local.name
  resource_group_name           = azurerm_resource_group.this.name
  vnet_name                     = var.network.vnet_name
  vnet_resource_group_name      = var.network.vnet_resource_group_name
  app_subnet_name               = var.network.app_subnet_name
  data_subnet_name              = var.network.data_subnet_name
  pe_subnet_name                = var.network.pe_subnet_name
  existing_private_dns_zone_ids = var.network.existing_private_dns_zone_ids
  postgres_dns_zone_prefix      = "${local.compact}${var.unique_suffix}"
  tags                          = local.tags
}

# ---------------- Identity + secrets ----------------
module "security" {
  source = "../security"

  name                          = local.name
  resource_group_name           = azurerm_resource_group.this.name
  location                      = var.location
  key_vault_name                = "${local.name}-kv-${var.unique_suffix}"
  purge_protection_enabled      = var.environment == "prod"
  public_network_access_enabled = var.key_vault_public_access
  allowed_ip_cidrs              = var.deployer_ip_cidrs
  pe_subnet_id                  = module.network.pe_subnet_id
  vault_private_dns_zone_id     = module.network.privatelink_zone_ids["vault"]
  secrets                       = var.secrets
  tags                          = local.tags
}

# ---------------- Storage ----------------
module "storage" {
  source = "../storage"

  name                       = local.name
  resource_group_name        = azurerm_resource_group.this.name
  location                   = var.location
  blob_account_name          = substr("${local.compact}blob${var.unique_suffix}", 0, 24)
  files_account_name         = substr("${local.compact}files${var.unique_suffix}", 0, 24)
  blob_replication_type      = var.storage.blob_replication_type
  files_replication_type     = var.storage.files_replication_type
  shared_share_quota_gb      = var.storage.shared_share_quota_gb
  pe_subnet_id               = module.network.pe_subnet_id
  blob_private_dns_zone_id   = module.network.privatelink_zone_ids["blob"]
  file_private_dns_zone_id   = module.network.privatelink_zone_ids["file"]
  allowed_ip_cidrs           = var.deployer_ip_cidrs
  node_identity_principal_id = module.security.identity_principal_id
  tags                       = local.tags
}

# ---------------- Database ----------------
module "database" {
  count  = var.database.create ? 1 : 0
  source = "../database"

  name                         = "${local.name}-pg-${var.unique_suffix}"
  resource_group_name          = azurerm_resource_group.this.name
  location                     = var.location
  delegated_subnet_id          = module.network.data_subnet_id
  private_dns_zone_id          = module.network.postgres_private_dns_zone_id
  dns_link_dependency          = module.network.postgres_dns_link_id
  postgres_version             = var.database.postgres_version
  sku_name                     = var.database.sku_name
  storage_mb                   = var.database.storage_mb
  high_availability_mode       = var.database.high_availability_mode
  backup_retention_days        = var.database.backup_retention_days
  geo_redundant_backup_enabled = var.database.geo_redundant_backup
  administrator_login          = var.database.admin_user
  administrator_password       = var.secrets["db-admin-password"]
  databases                    = concat([var.em.db_name], [for k, srv in local.servers : srv.db_name])
  tags                         = local.tags
}

# ---------------- Load balancer (Control-M Web / Automation API) ----------------
module "load_balancer" {
  source = "../load-balancer"

  name                = local.name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  internal            = var.load_balancer.internal
  subnet_id           = module.network.app_subnet_id
  public_dns_label    = var.load_balancer.public_dns_label
  web_port            = var.em.web_port
  tags                = local.tags
}

# ---------------- Platform DNS ----------------
module "dns" {
  source = "../dns"

  zone_name           = local.zone_name
  resource_group_name = azurerm_resource_group.this.name
  vnet_id             = module.network.vnet_id
  records             = { "ctm-web" = module.load_balancer.frontend_ip }
  cname_records       = local.pg_fqdn != null ? { "ctm-db" = local.pg_fqdn } : {}
  tags                = local.tags
}

# ---------------- Observability ----------------
module "observability" {
  source = "../observability"

  name                = local.name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  log_retention_days  = var.observability.log_retention_days
  alert_emails        = var.observability.alert_emails
  load_balancer_id    = module.load_balancer.lb_id
  postgres_server_id  = var.database.create ? module.database[0].server_id : null
  tags                = local.tags
}

# ---------------- Bootstrap scripts ----------------
locals {
  common_tpl = {
    environment          = var.environment
    key_vault_name       = module.security.key_vault_name
    identity_client_id   = module.security.identity_client_id
    blob_account         = module.storage.blob_account_name
    installers_container = module.storage.installers_container_name
    backups_container    = module.storage.backups_container_name
    nfs_server           = module.storage.nfs_server
    nfs_export           = module.storage.nfs_export_path
    data_mount           = local.data_mount
    ctm_version          = var.ctm_version
    dns_zone             = local.zone_name
  }

  em_userdata = {
    for name, n in local.em_nodes : name => join("\n", [
      templatefile("${path.module}/templates/common.sh.tftpl", merge(local.common_tpl, { role = "em-${n.role}", attach_data_disk = "true" })),
      templatefile("${path.module}/templates/em.sh.tftpl", {
        em_role           = n.role
        em_user           = var.em.os_user
        em_installer_blob = var.installer_blobs.em_installer
        em_silent_blob    = n.role == "primary" ? var.installer_blobs.em_silent_primary : var.installer_blobs.em_silent_secondary
        db_host           = local.db_alias
        db_port           = var.database.port
        em_db_name        = var.em.db_name
        em_db_user        = var.em.db_user
        db_admin_user     = var.database.admin_user
        em_primary_host   = local.em_primary_host
        em_secondary_host = var.em.enable_secondary ? local.em_secondary_host : ""
        web_port          = var.em.web_port
      })
    ])
  }

  server_userdata = {
    for name, n in local.server_nodes : name => join("\n", [
      templatefile("${path.module}/templates/common.sh.tftpl", merge(local.common_tpl, { role = "server-${n.role}", attach_data_disk = "true" })),
      templatefile("${path.module}/templates/server.sh.tftpl", {
        server_role           = n.role
        ctm_user              = local.servers[n.server].os_user
        server_installer_blob = var.installer_blobs.server_installer
        server_silent_blob    = n.role == "primary" ? var.installer_blobs.server_silent_primary : var.installer_blobs.server_silent_secondary
        db_host               = local.db_alias
        db_port               = var.database.port
        ctm_db_name           = local.servers[n.server].db_name
        ctm_db_user           = local.servers[n.server].db_user
        ctm_db_secret_name    = local.servers[n.server].secret_name
        db_admin_user         = var.database.admin_user
        ctm_server_name       = local.servers[n.server].name
        server_primary_host   = local.servers[n.server].primary_host
        server_secondary_host = local.servers[n.server].secondary_host
        em_host               = local.em_primary_host
      })
    ])
  }

  agent_userdata = {
    for k, srv in local.servers : k => join("\n", [
      templatefile("${path.module}/templates/common.sh.tftpl", merge(local.common_tpl, { role = "agent-${k}", attach_data_disk = "false" })),
      templatefile("${path.module}/templates/agent.sh.tftpl", {
        agent_user      = srv.agents.os_user
        em_api_endpoint = "https://${local.web_alias}:${var.em.web_port}/automation-api"
        em_api_user     = "emuser"
        ctm_server_name = srv.name
        hostgroup       = srv.hostgroup
        agent_port      = srv.agents.agent_port
        provision_image = srv.agents.provision_image
      })
    ])
  }
}

# ---------------- EM nodes ----------------
module "em_node" {
  for_each = local.em_nodes
  source   = "../node"

  name                    = each.key
  resource_group_name     = azurerm_resource_group.this.name
  location                = var.location
  zone                    = each.value.zone
  subnet_id               = module.network.app_subnet_id
  vm_size                 = var.em.vm_size
  image                   = var.image
  ssh_public_key          = var.ssh_public_key
  identity_id             = module.security.identity_id
  data_disk_gb            = var.em.data_disk_gb
  disk_type               = var.em.disk_type
  custom_data             = local.em_userdata[each.key]
  lb_backend_pool_id      = module.load_balancer.backend_pool_id
  data_collection_rule_id = module.observability.data_collection_rule_id
  tags                    = merge(local.tags, { "ctm:component" = "em", "ctm:role" = each.value.role })

  depends_on = [module.database, module.storage, module.security]
}

# ---------------- Control-M/Server nodes ----------------
module "server_node" {
  for_each = local.server_nodes
  source   = "../node"

  name                    = each.key
  resource_group_name     = azurerm_resource_group.this.name
  location                = var.location
  zone                    = each.value.zone
  subnet_id               = module.network.app_subnet_id
  vm_size                 = local.servers[each.value.server].vm_size
  image                   = var.image
  ssh_public_key          = var.ssh_public_key
  identity_id             = module.security.identity_id
  data_disk_gb            = local.servers[each.value.server].data_disk_gb
  disk_type               = local.servers[each.value.server].disk_type
  custom_data             = local.server_userdata[each.key]
  data_collection_rule_id = module.observability.data_collection_rule_id
  tags                    = merge(local.tags, { "ctm:component" = "server", "ctm:role" = each.value.role, "ctm:server" = local.servers[each.value.server].name })

  depends_on = [module.database, module.storage, module.security]
}

# ---------------- Agent scale set per Control-M/Server ----------------
module "agent_pool" {
  for_each = local.servers
  source   = "../agent-pool"

  name                    = "${local.name}-agents-${each.key}"
  resource_group_name     = azurerm_resource_group.this.name
  location                = var.location
  subnet_id               = module.network.app_subnet_id
  zones                   = each.value.agents.zones
  vm_size                 = each.value.agents.vm_size
  image                   = var.image
  ssh_public_key          = var.ssh_public_key
  identity_id             = module.security.identity_id
  custom_data             = local.agent_userdata[each.key]
  min_instances           = each.value.agents.min
  max_instances           = each.value.agents.max
  default_instances       = each.value.agents.default
  scale_out_cpu_percent   = each.value.agents.scale_out_cpu
  scale_in_cpu_percent    = each.value.agents.scale_in_cpu
  use_spot                = each.value.agents.use_spot
  data_collection_rule_id = module.observability.data_collection_rule_id
  tags                    = merge(local.tags, { "ctm:component" = "agent", "ctm:server" = each.value.name, "ctm:stage" = each.key })

  depends_on = [module.storage, module.security]
}

# ---------------- Node availability alerts ----------------
locals {
  node_vm_ids = merge(
    { for k, m in module.em_node : k => m.vm_id },
    { for k, m in module.server_node : k => m.vm_id },
  )
}

resource "azurerm_monitor_metric_alert" "vm_availability" {
  for_each            = local.node_vm_ids
  name                = "${local.name}-${each.key}-unavailable"
  resource_group_name = azurerm_resource_group.this.name
  scopes              = [each.value]
  description         = "Control-M node ${each.key} reports unavailable."
  severity            = 1
  frequency           = "PT1M"
  window_size         = "PT5M"
  tags                = local.tags

  criteria {
    metric_namespace = "Microsoft.Compute/virtualMachines"
    metric_name      = "VmAvailabilityMetric"
    aggregation      = "Average"
    operator         = "LessThan"
    threshold        = 1
  }

  action {
    action_group_id = module.observability.action_group_id
  }
}

# ---------------- Backup ----------------
module "backup" {
  count  = var.backup.enabled ? 1 : 0
  source = "../backup"

  name                         = local.name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = var.location
  storage_mode_type            = var.backup.storage_mode_type
  cross_region_restore_enabled = var.backup.cross_region_restore
  daily_retention_days         = var.backup.daily_retention_days
  weekly_retention_weeks       = var.backup.weekly_retention_weeks
  vm_ids                       = local.node_vm_ids
  files_storage_account_id     = var.backup.include_files_share ? module.storage.files_account_id : null
  files_share_name             = var.backup.include_files_share ? module.storage.share_name : null
  tags                         = local.tags
}
