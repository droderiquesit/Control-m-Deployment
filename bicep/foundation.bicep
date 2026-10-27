// Resource-group-scope entry point: the Control-M foundation for ONE region,
// anchored on that region's UTILITY VNet (EM + LB frontend, delegated data
// subnet, private endpoints). Per-server VNets only receive DNS links here;
// their VMs/VMSS are placed by the compute phase.
//   role = primary : network refs, identity/Key Vault, storage, PostgreSQL,
//                    load balancer, platform DNS, observability.
//   role = dr      : same foundation without the database — the ctm-db alias
//                    points at databaseHostOverride (the cross-region replica).
// Deployed by: python -m ctmdeploy deploy --env <env> --phase foundation
targetScope = 'resourceGroup'

@description('Platform: nonprod (hosts dev + qa Control-M/Servers) or prod (hosts uat + prod).')
@allowed(['nonprod', 'prod'])
param environment string

@description('primary = full platform; dr = standby region foundation.')
@allowed(['primary', 'dr'])
param role string

@description('3-6 lowercase alphanumerics making globally-unique names (storage, key vault, postgres) unique.')
param uniqueSuffix string

@description('Existing network to deploy into, config.yaml v2 shape: utility {vnet_name, vnet_resource_group_name, em_subnet_name, data_subnet_name, pe_subnet_name} + existing_private_dns_zone_ids. The orchestrator passes the role-appropriate block (network: or dr.network:).')
param network object

@description('Deduped list of EVERY VNet in this platform region (utility + each server VNet + agent overrides + extra_dns_vnets), computed by the orchestrator. Items: {name, resourceGroupName}. Each private DNS zone created here is linked to all of them.')
param allVnets array

@description('Registration-enabled links for the platform private DNS zone, one per VNet in this region, computed by the orchestrator. Items: {name, vnetId}.')
param dnsVnetLinks array

@description('Platform private DNS zone (same name in primary and DR so failed-over nodes keep resolving ctm-db / ctm-web), e.g. prod.ctm.internal.')
param privateDnsZoneName string

@description('Public CIDRs of CI runners / admins allowed through the Key Vault and storage firewalls.')
param deployerIpCidrs array

@description('Keep the Key Vault reachable from the public endpoint (firewall-restricted) so CI can seed secrets.')
param keyVaultPublicAccess bool

@description('Object id granted Key Vault Secrets Officer so the orchestrator can seed secrets; empty = skip.')
param deployerObjectId string = ''

@description('true = create the PostgreSQL Flexible Server here; false (DR) = alias databaseHostOverride instead.')
param databaseCreate bool

@description('FQDN the ctm-db alias points at when databaseCreate is false (the DR replica).')
param databaseHostOverride string = ''

@description('PostgreSQL admin password. The only secret that crosses a deployment boundary; it is also seeded to Key Vault by the orchestrator.')
@secure()
param databaseAdminPassword string = ''

@description('config.yaml database shape (sku_name, storage_mb, high_availability_mode, backup_retention_days, geo_redundant_backup, postgres_version).')
param database object

@description('config.yaml storage shape (blob_replication_type, files_replication_type, shared_share_quota_gb).')
param storage object

@description('config.yaml load_balancer shape (internal, optional public_dns_label and web_port — the orchestrator injects em.web_port).')
param loadBalancer object

@description('config.yaml observability shape (log_retention_days, alert_emails).')
param observability object

@description('config.yaml servers map (stage -> server definition); used here to derive one database per Control-M/Server.')
param servers object

@description('Enterprise Manager database name.')
param emDbName string = 'emdb'

@description('Tags applied to every resource.')
param tags object = {}

// ---------------------------------------------------------------------------
// Derived names — identical to the old platform composition.
// ---------------------------------------------------------------------------
var name = 'ctm-${environment}${role == 'dr' ? '-dr' : ''}'
var compact = replace(name, '-', '')
var location = resourceGroup().location

var platformTags = union(tags, {
  environment: environment
  platform: 'control-m'
  role: role
  managedBy: 'bicep'
})

// One database per Control-M/Server (ctm_<stage>) plus the EM database.
var databaseNames = concat([emDbName], map(objectKeys(servers), stage => 'ctm_${stage}'))

// ---------------- Existing network (utility VNet + zone links) ----------------
module net 'modules/network.bicep' = {
  name: '${name}-network'
  params: {
    utility: network.utility
    allVnets: allVnets
    existingPrivateDnsZoneIds: network.?existing_private_dns_zone_ids ?? {}
    postgresDnsZonePrefix: '${compact}${uniqueSuffix}'
    tags: platformTags
  }
}

// ---------------- Identity + Key Vault ----------------
// Secrets themselves are seeded by the orchestrator (ctmdeploy --phase secrets).
module sec 'modules/security.bicep' = {
  name: '${name}-security'
  params: {
    name: name
    location: location
    keyVaultName: '${name}-kv-${uniqueSuffix}'
    purgeProtectionEnabled: environment == 'prod'
    publicNetworkAccessEnabled: keyVaultPublicAccess
    allowedIpCidrs: deployerIpCidrs
    peSubnetId: net.outputs.peSubnetId
    vaultPrivateDnsZoneId: net.outputs.privatelinkZoneIds.vault
    deployerObjectId: deployerObjectId
    tags: platformTags
  }
}

// ---------------- Storage ----------------
module store 'modules/storage.bicep' = {
  name: '${name}-storage'
  params: {
    name: name
    location: location
    blobAccountName: take('${compact}blob${uniqueSuffix}', 24)
    filesAccountName: take('${compact}files${uniqueSuffix}', 24)
    blobReplicationType: storage.?blob_replication_type ?? 'RAGZRS'
    filesReplicationType: storage.?files_replication_type ?? 'ZRS'
    sharedShareQuotaGb: storage.?shared_share_quota_gb ?? 256
    peSubnetId: net.outputs.peSubnetId
    blobPrivateDnsZoneId: net.outputs.privatelinkZoneIds.blob
    filePrivateDnsZoneId: net.outputs.privatelinkZoneIds.file
    allowedIpCidrs: deployerIpCidrs
    nodeIdentityPrincipalId: sec.outputs.identityPrincipalId
    tags: platformTags
  }
}

// ---------------- Database (primary role only) ----------------
module db 'modules/database.bicep' = if (databaseCreate) {
  name: '${name}-database'
  params: {
    name: '${name}-pg-${uniqueSuffix}'
    location: location
    delegatedSubnetId: net.outputs.dataSubnetId
    privateDnsZoneId: net.outputs.postgresPrivateDnsZoneId
    postgresVersion: database.?postgres_version ?? '16'
    skuName: database.?sku_name ?? ''
    storageMb: database.?storage_mb ?? 131072
    highAvailabilityMode: database.?high_availability_mode ?? 'ZoneRedundant'
    backupRetentionDays: database.?backup_retention_days ?? 14
    geoRedundantBackup: database.?geo_redundant_backup ?? false
    // The orchestrator injects the LIVE zones when the server already exists,
    // so a re-deploy does not fight an HA zone swap back (idempotency).
    availabilityZone: database.?availability_zone ?? '1'
    standbyZone: database.?standby_zone ?? '2'
    administratorLogin: database.?admin_user ?? 'ctmadmin'
    administratorPassword: databaseAdminPassword
    databases: databaseNames
    tags: platformTags
  }
}

// ---------------- Load balancer (Control-M Web / Automation API) ----------------
module lb 'modules/load-balancer.bicep' = {
  name: '${name}-load-balancer'
  params: {
    name: name
    location: location
    internal: loadBalancer.?internal ?? true
    subnetId: net.outputs.emSubnetId
    publicDnsLabel: loadBalancer.?public_dns_label ?? ''
    webPort: loadBalancer.?web_port ?? 8443
    tags: platformTags
  }
}

// ---------------- Platform DNS ----------------
// ctm-db CNAME exists only when there is a database here or an override (DR).
var postgresFqdn = databaseCreate ? db!.outputs.fqdn : databaseHostOverride

module dnsZone 'modules/dns.bicep' = {
  name: '${name}-dns'
  params: {
    zoneName: privateDnsZoneName
    vnetLinks: dnsVnetLinks
    aRecords: { 'ctm-web': lb.outputs.frontendIp }
    cnameRecords: postgresFqdn != '' ? { 'ctm-db': postgresFqdn } : {}
    tags: platformTags
  }
}

// ---------------- Observability ----------------
module obs 'modules/observability.bicep' = {
  name: '${name}-observability'
  params: {
    name: name
    location: location
    logRetentionDays: observability.?log_retention_days ?? 30
    alertEmails: observability.?alert_emails ?? []
    loadBalancerId: lb.outputs.lbId
    postgresServerId: databaseCreate ? db!.outputs.serverId : ''
    tags: platformTags
  }
}

// ---------------------------------------------------------------------------
// Outputs consumed by the compute / dr phases.
// ---------------------------------------------------------------------------
output emSubnetId string = net.outputs.emSubnetId
output dataSubnetId string = net.outputs.dataSubnetId
output peSubnetId string = net.outputs.peSubnetId
output vnetId string = net.outputs.utilityVnetId
output postgresPrivateDnsZoneId string = net.outputs.postgresPrivateDnsZoneId
output identityId string = sec.outputs.identityId
output identityClientId string = sec.outputs.identityClientId
output identityPrincipalId string = sec.outputs.identityPrincipalId
output keyVaultName string = sec.outputs.keyVaultName
output keyVaultUri string = sec.outputs.keyVaultUri
output blobAccountId string = store.outputs.blobAccountId
output blobAccountName string = store.outputs.blobAccountName
output filesAccountId string = store.outputs.filesAccountId
output filesAccountName string = store.outputs.filesAccountName
output nfsServer string = store.outputs.nfsServer
output nfsExportPath string = store.outputs.nfsExportPath
output shareName string = store.outputs.shareName
output lbId string = lb.outputs.lbId
output lbFrontendIp string = lb.outputs.frontendIp
output lbBackendPoolId string = lb.outputs.backendPoolId
output lbPublicIpId string = lb.outputs.publicIpId
output databaseServerId string = databaseCreate ? db!.outputs.serverId : ''
output databaseFqdn string = postgresFqdn
output workspaceId string = obs.outputs.workspaceId
output dataCollectionRuleId string = obs.outputs.dataCollectionRuleId
output actionGroupId string = obs.outputs.actionGroupId
output dnsZoneName string = dnsZone.outputs.zoneName
