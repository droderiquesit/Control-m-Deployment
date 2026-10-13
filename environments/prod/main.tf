locals {
  name                  = "ctm-${var.environment}"
  private_dns_zone_name = "${var.environment}.ctm.internal"
  replica_name          = "${local.name}-dr-pg-${var.unique_suffix}"
}

# ---------------- Primary region ----------------
module "platform" {
  source = "../../modules/platform"

  environment   = var.environment
  location      = var.location
  role          = "primary"
  unique_suffix = var.unique_suffix
  resource_lock = var.resource_lock
  tags          = var.tags

  network                 = var.network
  private_dns_zone_name   = local.private_dns_zone_name
  deployer_ip_cidrs       = var.deployer_ip_cidrs
  key_vault_public_access = var.key_vault_public_access

  secrets        = local.secrets
  ssh_public_key = tls_private_key.ssh.public_key_openssh

  ctm_version     = var.ctm_version
  installer_blobs = var.installer_blobs
  image           = var.image

  em            = var.em
  servers       = var.servers
  database      = var.database
  storage       = var.storage
  load_balancer = var.load_balancer
  backup        = var.backup
  observability = var.observability
}

# ---------------- DR region foundation ----------------
module "platform_dr" {
  count  = var.dr.enabled ? 1 : 0
  source = "../../modules/platform"

  environment   = var.environment
  location      = var.dr.location
  role          = "dr"
  unique_suffix = var.unique_suffix
  resource_lock = var.resource_lock
  tags          = var.tags

  network                 = var.dr.network
  private_dns_zone_name   = local.private_dns_zone_name
  deployer_ip_cidrs       = var.deployer_ip_cidrs
  key_vault_public_access = var.key_vault_public_access

  secrets        = local.secrets
  ssh_public_key = tls_private_key.ssh.public_key_openssh

  ctm_version     = var.ctm_version
  installer_blobs = var.installer_blobs
  image           = var.image

  em = merge(var.em, { deploy = false })
  # Same Servers, but only warm agents in DR (VMs arrive through Site Recovery).
  servers = { for k, s in var.servers : k => merge(s, {
    agents = merge(s.agents, { min = 0, default = var.dr.warm_agents })
  }) }

  database = {
    create        = false
    host_override = "${local.replica_name}.postgres.database.azure.com"
  }

  storage = var.storage
  load_balancer = {
    internal         = var.load_balancer.internal
    public_dns_label = var.dr.public_dns_label
  }
  backup        = { enabled = false }
  observability = var.observability
}

# ---------------- Cross-region replication / failover ----------------
module "dr" {
  count  = var.dr.enabled ? 1 : 0
  source = "../../modules/dr"

  name = local.name
  tags = var.tags

  primary = {
    resource_group_name = module.platform.resource_group_name
    location            = module.platform.location
    vnet_id             = module.platform.vnet_id
    database_server_id  = module.platform.database_server_id
    blob_account_id     = module.platform.blob_account_id
    lb_public_ip_id     = module.platform.load_balancer_public_ip_id
    nodes               = module.platform.nodes
  }

  dr = {
    resource_group_name          = module.platform_dr[0].resource_group_name
    resource_group_id            = module.platform_dr[0].resource_group_id
    location                     = module.platform_dr[0].location
    vnet_id                      = module.platform_dr[0].vnet_id
    app_subnet_name              = module.platform_dr[0].app_subnet_name
    data_subnet_id               = module.platform_dr[0].data_subnet_id
    postgres_private_dns_zone_id = module.platform_dr[0].postgres_private_dns_zone_id
    postgres_dns_link_id         = module.platform_dr[0].postgres_dns_link_id
    blob_account_id              = module.platform_dr[0].blob_account_id
    lb_public_ip_id              = module.platform_dr[0].load_balancer_public_ip_id
    lb_backend_pool_id           = module.platform_dr[0].load_balancer_backend_pool_id
  }

  replica_name         = local.replica_name
  replica_sku_name     = coalesce(var.dr.replica_sku_name, var.database.sku_name)
  enable_site_recovery = var.dr.enable_site_recovery

  traffic_manager = {
    enabled       = var.dr.traffic_manager
    relative_name = "${local.name}-web"
  }
}
