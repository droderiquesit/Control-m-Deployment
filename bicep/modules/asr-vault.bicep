// Recovery Services vault for Azure Site Recovery in the DR region. Vault
// only: fabrics, protection containers, mappings and replicated items are
// created by the orchestrator (ctmdeploy --phase replication) via the SDK.
@description('Vault name, e.g. ctm-prod-asr-rsv.')
param name string

@description('DR region.')
param location string

@description('Tags applied to the vault.')
param tags object = {}

resource vault 'Microsoft.RecoveryServices/vaults@2024-04-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {}
}

output vaultId string = vault.id
output vaultName string = vault.name
