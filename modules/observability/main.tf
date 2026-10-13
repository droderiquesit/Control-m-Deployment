resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name}-law"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

# VM Insights solution so Azure Monitor shows process/dependency maps for the nodes.
resource "azurerm_log_analytics_solution" "vminsights" {
  solution_name         = "VMInsights"
  resource_group_name   = var.resource_group_name
  location              = var.location
  workspace_resource_id = azurerm_log_analytics_workspace.this.id
  workspace_name        = azurerm_log_analytics_workspace.this.name
  tags                  = var.tags

  plan {
    publisher = "Microsoft"
    product   = "OMSGallery/VMInsights"
  }
}

# Data Collection Rule: syslog (Control-M components log to syslog via the bootstrap
# scripts) + core perf counters from every EM/Server VM and the agent scale set.
resource "azurerm_monitor_data_collection_rule" "linux" {
  name                = "${var.name}-dcr-linux"
  resource_group_name = var.resource_group_name
  location            = var.location
  kind                = "Linux"
  tags                = var.tags

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.this.id
      name                  = "law"
    }
  }

  data_flow {
    streams      = ["Microsoft-Syslog", "Microsoft-Perf"]
    destinations = ["law"]
  }

  data_sources {
    syslog {
      name           = "syslog"
      streams        = ["Microsoft-Syslog"]
      facility_names = ["auth", "authpriv", "cron", "daemon", "kern", "local0", "local1", "syslog", "user"]
      log_levels     = ["Warning", "Error", "Critical", "Alert", "Emergency", "Notice", "Info"]
    }

    performance_counter {
      name                          = "perf"
      streams                       = ["Microsoft-Perf"]
      sampling_frequency_in_seconds = 60
      counter_specifiers = [
        "Processor(*)\\% Processor Time",
        "Memory(*)\\% Used Memory",
        "Memory(*)\\Available MBytes Memory",
        "Logical Disk(*)\\% Used Space",
        "Logical Disk(*)\\Disk Transfers/sec",
        "Network(*)\\Total Bytes Transmitted",
        "Network(*)\\Total Bytes Received",
      ]
    }
  }
}

# ---------------- Alerting ----------------
resource "azurerm_monitor_action_group" "this" {
  name                = "${var.name}-alerts"
  resource_group_name = var.resource_group_name
  short_name          = substr(replace(var.name, "-", ""), 0, 12)
  tags                = var.tags

  dynamic "email_receiver" {
    for_each = toset(var.alert_emails)
    content {
      name                    = "email-${replace(each.value, "/[^a-zA-Z0-9]/", "")}"
      email_address           = each.value
      use_common_alert_schema = true
    }
  }
}

# Control-M Web unreachable through the LB (also feeds the DR decision).
resource "azurerm_monitor_metric_alert" "lb_probe" {
  count               = var.load_balancer_id != null ? 1 : 0
  name                = "${var.name}-em-web-unhealthy"
  resource_group_name = var.resource_group_name
  scopes              = [var.load_balancer_id]
  description         = "No EM node answers the Control-M Web health probe."
  severity            = 0
  frequency           = "PT1M"
  window_size         = "PT5M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.Network/loadBalancers"
    metric_name      = "DipAvailability"
    aggregation      = "Average"
    operator         = "LessThan"
    threshold        = 50
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

locals {
  pg_alerts = var.postgres_server_id == null ? {} : {
    cpu = {
      metric    = "cpu_percent"
      operator  = "GreaterThan"
      threshold = 85
      severity  = 2
      desc      = "PostgreSQL CPU above 85% for 15 minutes."
    }
    storage = {
      metric    = "storage_percent"
      operator  = "GreaterThan"
      threshold = 85
      severity  = 1
      desc      = "PostgreSQL storage above 85%."
    }
    memory = {
      metric    = "memory_percent"
      operator  = "GreaterThan"
      threshold = 90
      severity  = 2
      desc      = "PostgreSQL memory above 90%."
    }
    connections = {
      metric    = "connections_failed"
      operator  = "GreaterThan"
      threshold = 10
      severity  = 2
      desc      = "PostgreSQL failed connections."
    }
    availability = {
      metric    = "is_db_alive"
      operator  = "LessThan"
      threshold = 1
      severity  = 0
      desc      = "PostgreSQL server unavailable."
    }
  }
}

resource "azurerm_monitor_metric_alert" "postgres" {
  for_each            = local.pg_alerts
  name                = "${var.name}-pg-${each.key}"
  resource_group_name = var.resource_group_name
  scopes              = [var.postgres_server_id]
  description         = each.value.desc
  severity            = each.value.severity
  frequency           = "PT5M"
  window_size         = "PT15M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
    metric_name      = each.value.metric
    aggregation      = each.key == "availability" ? "Maximum" : "Average"
    operator         = each.value.operator
    threshold        = each.value.threshold
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

# Azure Service Health incidents for the region.
resource "azurerm_monitor_activity_log_alert" "service_health" {
  name                = "${var.name}-service-health"
  resource_group_name = var.resource_group_name
  location            = "global"
  scopes              = [format("/subscriptions/%s", data.azurerm_client_config.current.subscription_id)]
  description         = "Azure service health incidents affecting the Control-M platform region."
  tags                = var.tags

  criteria {
    category = "ServiceHealth"
    service_health {
      events    = ["Incident", "Maintenance"]
      locations = [var.location]
    }
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

data "azurerm_client_config" "current" {}
