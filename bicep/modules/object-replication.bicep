// One side of a blob object replication policy. Called twice by dr.bicep:
// first on the DESTINATION account ('default' ids on the FIRST run let the
// service mint the real policy/rule ids; re-runs pass the live ids back in so
// the PUT converges instead of minting a duplicate policy), then on the
// SOURCE account echoing the destination's ids.
@description('Storage account (in this resource group) the policy is attached to.')
param accountName string

@description('\'default\' mints a new policy (first destination deploy only); otherwise the service-assigned policy id — the live one on destination re-deploys, the destination output on the source side.')
param policyId string = 'default'

@description('Full resource id of the replication source account.')
param sourceAccountId string

@description('Full resource id of the replication destination account.')
param destinationAccountId string

@description('Rule items: {sourceContainer, destinationContainer}. Service-assigned ids come from ruleIds.')
param rules array

@description('Service-assigned rule ids, positional with rules; [] (or an empty entry) lets the service mint one. Pass the live ids on destination re-deploys and the destination outputs on the source side so the PUT converges on the existing rules.')
param ruleIds array = []

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: accountName
}

resource policy 'Microsoft.Storage/storageAccounts/objectReplicationPolicies@2023-05-01' = {
  parent: account
  name: policyId
  properties: {
    sourceAccount: sourceAccountId
    destinationAccount: destinationAccountId
    rules: [for (r, i) in rules: union(
      {
        sourceContainer: r.sourceContainer
        destinationContainer: r.destinationContainer
        filters: {
          // 1601-01-01 (Windows epoch) = copy Everything, including blobs
          // created before the policy existed.
          minCreationTime: '1601-01-01T00:00:00Z'
        }
      },
      // Nested ternaries (not &&): if() is lazy, and() is not, and ruleIds[i]
      // must never be evaluated when the entry does not exist.
      i < length(ruleIds) ? (empty(ruleIds[i]) ? {} : { ruleId: ruleIds[i] }) : {}
    )]
  }
}

output policyId string = policy.properties.policyId
output ruleIds array = [for (r, i) in rules: policy.properties.rules[i].ruleId]
