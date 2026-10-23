// Monitoring for the Control-M platform: Log Analytics workspace + VM Insights,
// the Linux Data Collection Rule (syslog + perf counters), the alert action group
// and the metric/activity-log alerts (LB health probe, PostgreSQL, Service Health).
// Ported property-for-property from the old modules/observability.

@description('Platform name prefix (ctm-<env>[-dr]); resources are named <name>-law, <name>-dcr-linux, <name>-alerts, ...')
param name string

@description('Azure region for the workspace and data collection rule.')
param location string

@description('Log Analytics data retention in days.')
param logRetentionDays int

@description('Email recipients for platform alerts.')
param alertEmails array = []

@description('LB whose health-probe availability is alerted on (empty string to skip).')
param loadBalancerId string = ''

@description('PostgreSQL Flexible Server to alert on (empty string to skip).')
param postgresServerId string = ''

@description('Tags applied to every resource created by this module.')
param tags object = {}

var lawName = '${name}-law'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

// VM Insights solution so Azure Monitor shows process/dependency maps for the nodes.
resource vmInsights 'Microsoft.OperationsManagement/solutions@2015-11-01-preview' = {
  name: 'VMInsights(${lawName})'
  location: location
  tags: tags
  plan: {
    name: 'VMInsights(${lawName})'
    publisher: 'Microsoft'
    product: 'OMSGallery/VMInsights'
    promotionCode: ''
  }
  properties: {
    workspaceResourceId: workspace.id
  }
}

// Data Collection Rule: syslog (Control-M components log to syslog via the bootstrap
// scripts) + core perf counters from every EM/Server VM and the agent scale set.
resource dataCollectionRule 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${name}-dcr-linux'
  location: location
  kind: 'Linux'
  tags: tags
  properties: {
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: workspace.id
          name: 'law'
        }
      ]
    }
    dataFlows: [
      {
        streams: [
          'Microsoft-Syslog'
          'Microsoft-Perf'
        ]
        destinations: [
          'law'
        ]
      }
    ]
    dataSources: {
      syslog: [
        {
          name: 'syslog'
          streams: [
            'Microsoft-Syslog'
          ]
          facilityNames: [
            'auth'
            'authpriv'
            'cron'
            'daemon'
            'kern'
            'local0'
            'local1'
            'syslog'
            'user'
          ]
          logLevels: [
            'Warning'
            'Error'
            'Critical'
            'Alert'
            'Emergency'
            'Notice'
            'Info'
          ]
        }
      ]
      performanceCounters: [
        {
          name: 'perf'
          streams: [
            'Microsoft-Perf'
          ]
          samplingFrequencyInSeconds: 60
          counterSpecifiers: [
            'Processor(*)\\% Processor Time'
            'Memory(*)\\% Used Memory'
            'Memory(*)\\Available MBytes Memory'
            'Logical Disk(*)\\% Used Space'
            'Logical Disk(*)\\Disk Transfers/sec'
            'Network(*)\\Total Bytes Transmitted'
            'Network(*)\\Total Bytes Received'
          ]
        }
      ]
    }
  }
}

// ---------------- Alerting ----------------
// Bicep has no regex replace: strip the characters RFC-common in org email
// addresses so receiver names stay alphanumeric (mirrors the original regex strip).
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${name}-alerts'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take(replace(name, '-', ''), 12)
    enabled: true
    emailReceivers: [
      for email in alertEmails: {
        name: 'email-${replace(replace(replace(replace(replace(email, '@', ''), '.', ''), '_', ''), '-', ''), '+', '')}'
        emailAddress: email
        useCommonAlertSchema: true
      }
    ]
  }
}

// Control-M Web unreachable through the LB (also feeds the DR decision).
resource lbProbeAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (!empty(loadBalancerId)) {
  name: '${name}-em-web-unhealthy'
  location: 'global'
  tags: tags
  properties: {
    description: 'No EM node answers the Control-M Web health probe.'
    severity: 0
    enabled: true
    autoMitigate: true
    scopes: [
      loadBalancerId
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'DipAvailability'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.Network/loadBalancers'
          metricName: 'DipAvailability'
          timeAggregation: 'Average'
          operator: 'LessThan'
          threshold: 50
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

var pgAlerts = [
  {
    key: 'cpu'
    metric: 'cpu_percent'
    aggregation: 'Average'
    operator: 'GreaterThan'
    threshold: 85
    severity: 2
    description: 'PostgreSQL CPU above 85% for 15 minutes.'
  }
  {
    key: 'storage'
    metric: 'storage_percent'
    aggregation: 'Average'
    operator: 'GreaterThan'
    threshold: 85
    severity: 1
    description: 'PostgreSQL storage above 85%.'
  }
  {
    key: 'memory'
    metric: 'memory_percent'
    aggregation: 'Average'
    operator: 'GreaterThan'
    threshold: 90
    severity: 2
    description: 'PostgreSQL memory above 90%.'
  }
  {
    key: 'connections'
    metric: 'connections_failed'
    aggregation: 'Average'
    operator: 'GreaterThan'
    threshold: 10
    severity: 2
    description: 'PostgreSQL failed connections.'
  }
  {
    key: 'availability'
    metric: 'is_db_alive'
    aggregation: 'Maximum'
    operator: 'LessThan'
    threshold: 1
    severity: 0
    description: 'PostgreSQL server unavailable.'
  }
]

resource postgresAlerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [
  for alert in pgAlerts: if (!empty(postgresServerId)) {
    name: '${name}-pg-${alert.key}'
    location: 'global'
    tags: tags
    properties: {
      description: alert.description
      severity: alert.severity
      enabled: true
      autoMitigate: true
      scopes: [
        postgresServerId
      ]
      evaluationFrequency: 'PT5M'
      windowSize: 'PT15M'
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allOf: [
          {
            name: alert.metric
            criterionType: 'StaticThresholdCriterion'
            metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
            metricName: alert.metric
            timeAggregation: alert.aggregation
            operator: alert.operator
            threshold: alert.threshold
          }
        ]
      }
      actions: [
        {
          actionGroupId: actionGroup.id
        }
      ]
    }
  }
]

// Azure Service Health incidents for the region. Activity-log alerts watch the
// whole subscription even though this module deploys at resource-group scope.
resource serviceHealthAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: '${name}-service-health'
  location: 'global'
  tags: tags
  properties: {
    description: 'Azure service health incidents affecting the Control-M platform region.'
    enabled: true
    scopes: [
      '/subscriptions/${subscription().subscriptionId}'
    ]
    condition: {
      allOf: [
        {
          field: 'category'
          equals: 'ServiceHealth'
        }
        {
          field: 'properties.incidentType'
          containsAny: [
            'Incident'
            'Maintenance'
          ]
        }
        {
          field: 'properties.impactedServices[*].ImpactedRegions[*].RegionName'
          containsAny: [
            location
          ]
        }
      ]
    }
    actions: {
      actionGroups: [
        {
          actionGroupId: actionGroup.id
        }
      ]
    }
  }
}

output workspaceId string = workspace.id
output dataCollectionRuleId string = dataCollectionRule.id
output actionGroupId string = actionGroup.id
