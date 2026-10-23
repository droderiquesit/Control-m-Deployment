// One side of a blob object replication policy. Called twice by dr.bicep:
// first on the DESTINATION account (policyId 'default' lets the service mint
// the real policy/rule ids), then on the SOURCE account echoing those ids.
@description('Storage account (in this resource group) the policy is attached to.')
param accountName string

@description('Use \'default\' on the destination side; the service-assigned policy id on the source side.')
param policyId string = 'default'

@description('Full resource id of the replication source account.')
param sourceAccountId string

@description('Full resource id of the replication destination account.')
param destinationAccountId string

@description('Rule items: {sourceContainer, destinationContainer, ruleId?}. ruleId is set only on the source side, echoing the destination output.')
param rules array

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: accountName
}

resource policy 'Microsoft.Storage/storageAccounts/objectReplicationPolicies@2023-05-01' = {
  parent: account
  name: policyId
  properties: {
    sourceAccount: sourceAccountId
    destinationAccount: destinationAccountId
    rules: [for r in rules: union(
      {
        sourceContainer: r.sourceContainer
        destinationContainer: r.destinationContainer
        filters: {
          // 1601-01-01 (Windows epoch) = copy Everything, including blobs
          // created before the policy existed.
          minCreationTime: '1601-01-01T00:00:00Z'
        }
      },
      contains(r, 'ruleId') ? { ruleId: r.ruleId } : {}
    )]
  }
}

output policyId string = policy.properties.policyId
output ruleIds array = [for (r, i) in rules: policy.properties.rules[i].ruleId]
