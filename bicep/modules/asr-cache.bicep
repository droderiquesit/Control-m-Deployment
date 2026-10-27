// Site Recovery replication cache storage account. Must live in the SOURCE
// region: ASR stages disk replication traffic here before shipping it to the
// DR region.
@description('Globally-unique storage account name (max 24 lowercase alphanumerics).')
param name string

@description('Primary (source) region.')
param location string

@description('Tags applied to the account.')
param tags object = {}

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

output accountId string = account.id
output accountName string = account.name
