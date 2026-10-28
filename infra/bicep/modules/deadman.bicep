// Dead-man switch: Consumption Function App that deletes idle on_demand (idle_cost) deployments.
param location string
param prefix string
param subscriptionId string = subscription().subscriptionId
param tags object = {}

var suffix = uniqueString(resourceGroup().id)
var storageName = toLower(take('${replace(prefix, '-', '')}dm${suffix}', 24))

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  tags: tags
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false, supportsHttpsTrafficOnly: true }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

resource deployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blob
  name: 'deployments'
}

// Flex Consumption (FC1): classic Y1 Consumption quota is 0 in some regions/subscriptions.
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${prefix}-deadman-plan'
  location: location
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  kind: 'functionapp'
  properties: { reserved: true }
  tags: tags
}

var storageConn = 'DefaultEndpointsProtocol=https;AccountName=${sa.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${sa.listKeys().keys[0].value}'

resource fn 'Microsoft.Web/sites@2024-04-01' = {
  name: '${prefix}-deadman-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${sa.properties.primaryEndpoints.blob}${deployContainer.name}'
          authentication: { type: 'StorageAccountConnectionString', storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING' }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 1, instanceMemoryMB: 512 }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: storageConn }
        { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: storageConn }
        { name: 'CCP_SUBSCRIPTION_ID', value: subscriptionId }
        { name: 'DRY_RUN', value: 'true' }
      ]
    }
  }
}

output functionName string = fn.name
output principalId string = fn.identity.principalId
