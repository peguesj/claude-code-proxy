// Resource-group-scope module: one AIServices account + its model deployments.
targetScope = 'resourceGroup'

param location string
param accountName string
param models array
@description('When false, only the account is provisioned (on_demand groups).')
param deployModels bool = true
param tags object = {}

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

// Deployments on one account must be created serially.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for m in (deployModels ? models : []): {
  parent: account
  name: m.deployment
  sku: {
    name: m.sku
    capacity: m.capacity
  }
  properties: {
    model: {
      format: m.format
      name: m.name
      version: m.version
    }
  }
}]

output endpoint string = account.properties.endpoint
output accountId string = account.id
