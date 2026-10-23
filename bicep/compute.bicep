// Resource-group-scope entry point: Control-M EM/Server VMs (nodePlan), one
// agent VMSS per Control-M/Server (agentPools), Recovery Services backup and a
// per-VM availability alert. Placement is per item: every node/pool carries its
// own subnetId (EM => utility VNet, each Server + its agents => that server's
// own VNet), computed by the orchestrator together with the rendered cloud-init
// and the isNew flags: python -m ctmdeploy deploy --env <env> --phase compute
targetScope = 'resourceGroup'

@description('Platform name, e.g. ctm-prod or ctm-prod-dr.')
param name string

@description('Region of this resource group.')
param location string

@description('Compute Gallery image id (.../galleries/<g>/images/<def>/versions/<v>), resolved by the orchestrator from config.yaml image settings.')
param imageId string

@description('OpenSSH public key for the ctmops admin user (derived from the Key Vault ssh-private-key secret).')
param sshPublicKey string

@description('User-assigned identity id the nodes run as (foundation output).')
param identityId string

@description('Linux Data Collection Rule id (foundation output).')
param dataCollectionRuleId string

@description('LB backend pool id joined by EM nodes (foundation output).')
param lbBackendPoolId string

@description('Action group id notified by the availability alerts (foundation output).')
param actionGroupId string

// NOTE on secrets: customData cannot be @secure() here (Bicep cannot mark
// individual array items secure) and that is acceptable BY DESIGN — the
// rendered cloud-init carries no secrets, only resource names, ids and
// endpoints. Every credential is fetched from Key Vault at boot through the
// node's managed identity, the same posture as before this migration.
@description('VMs to create. Items: {name, component(em|server), role(primary|secondary), zone, subnetId, vmSize, dataDiskGb, diskType, customData, isNew(bool), joinLb(bool), tags(object)}. subnetId places each node (EM => utility em subnet, server => its own VNet); isNew=false on re-deploys omits the immutable customData.')
param nodePlan array

@description('Agent scale sets. Items: {stage, name, subnetId, vmSize, zones, min, max, default, scaleOutCpu, scaleInCpu, useSpot, customData, isNew(bool)}. subnetId places the pool in its server\'s VNet; isNew=false omits sku.capacity so autoscale keeps owning the count.')
param agentPools array

@description('config.yaml backup shape (enabled, storage_mode_type, cross_region_restore, daily_retention_days, weekly_retention_weeks, include_files_share).')
param backup object

@description('Files storage account id, for the file-share backup (foundation output).')
param filesAccountId string = ''

@description('NFS share name, for the file-share backup (foundation output).')
param filesShareName string = ''

@description('Create a VmAvailabilityMetric alert per node.')
param vmAvailabilityAlerts bool = true

@description('Tags applied to every resource (the orchestrator passes the merged platform tags).')
param tags object = {}

// ---------------- EM / Control-M Server nodes ----------------
module nodeVms 'modules/node.bicep' = [for n in nodePlan: {
  name: 'node-${n.name}'
  params: {
    name: n.name
    location: location
    zone: n.zone
    subnetId: n.subnetId
    vmSize: n.vmSize
    imageId: imageId
    sshPublicKey: sshPublicKey
    customData: n.customData
    isNew: n.isNew
    dataDiskGb: n.dataDiskGb
    diskType: n.diskType
    identityId: identityId
    lbBackendPoolId: n.joinLb ? lbBackendPoolId : ''
    dataCollectionRuleId: dataCollectionRuleId
    tags: union(tags, n.tags)
  }
}]

// ---------------- Agent scale set per Control-M/Server ----------------
// ctm:server mirrors the old tagging: the logical Server name CTM_<STAGE>.
module pools 'modules/agent-pool.bicep' = [for p in agentPools: {
  name: 'pool-${p.stage}'
  params: {
    name: p.name
    location: location
    subnetId: p.subnetId
    vmSize: p.vmSize
    imageId: imageId
    sshPublicKey: sshPublicKey
    customData: p.customData
    isNew: p.isNew
    zones: p.zones
    minInstances: p.min
    maxInstances: p.max
    defaultInstances: p.default
    scaleOutCpuPercent: p.scaleOutCpu
    scaleInCpuPercent: p.scaleInCpu
    useSpot: p.useSpot
    identityId: identityId
    dataCollectionRuleId: dataCollectionRuleId
    tags: union(tags, {
      'ctm:component': 'agent'
      'ctm:server': toUpper('ctm_${p.stage}')
      'ctm:stage': p.stage
    })
  }
}]

// ---------------- Backup ----------------
var includeFilesShare = backup.?include_files_share ?? true

module backupVault 'modules/backup.bicep' = if (backup.?enabled ?? true) {
  name: '${name}-backup'
  params: {
    name: name
    location: location
    storageModeType: backup.?storage_mode_type ?? 'GeoRedundant'
    crossRegionRestore: backup.?cross_region_restore ?? false
    dailyRetentionDays: backup.?daily_retention_days ?? 30
    weeklyRetentionWeeks: backup.?weekly_retention_weeks ?? 12
    // Referencing the node outputs (not a precomputed id) makes the backup
    // deployment wait for every VM.
    vmIds: [for (n, i) in nodePlan: nodeVms[i].outputs.vmId]
    filesStorageAccountId: includeFilesShare ? filesAccountId : ''
    filesShareName: includeFilesShare ? filesShareName : ''
    tags: tags
  }
}

// ---------------- Node availability alerts ----------------
resource vmAvailability 'Microsoft.Insights/metricAlerts@2018-03-01' = [for (n, i) in nodePlan: if (vmAvailabilityAlerts) {
  name: '${name}-${n.name}-unavailable'
  location: 'global'
  tags: tags
  properties: {
    description: 'Control-M node ${n.name} reports unavailable.'
    severity: 1
    enabled: true
    autoMitigate: true
    scopes: [
      nodeVms[i].outputs.vmId
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'Metric1'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.Compute/virtualMachines'
          metricName: 'VmAvailabilityMetric'
          timeAggregation: 'Average'
          operator: 'LessThan'
          threshold: 1
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroupId
      }
    ]
  }
}]

// ---------------------------------------------------------------------------
// Outputs consumed by the dr / replication phases.
// ---------------------------------------------------------------------------
output nodes array = [for (n, i) in nodePlan: {
  name: n.name
  vmId: nodeVms[i].outputs.vmId
  nicId: nodeVms[i].outputs.nicId
  osDiskId: nodeVms[i].outputs.osDiskId
  dataDiskId: nodeVms[i].outputs.dataDiskId
  zone: n.zone
  privateIp: nodeVms[i].outputs.privateIp
  component: n.component
}]

// ARM cannot loop over module outputs inside a variable or lambda, so the
// VMSS ids are computed deterministically from the pool names; they are
// identical to the agent-pool module's vmssId outputs.
output agentVmssIds object = toObject(
  agentPools,
  p => p.stage,
  p => resourceId('Microsoft.Compute/virtualMachineScaleSets', p.name)
)
