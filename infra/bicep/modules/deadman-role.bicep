// Grants the dead-man identity rights on ONE on_demand resource group only.
param principalId string
var roles = {
  cognitiveServicesContributor: '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68'
  tagContributor: '4a9ae827-6dc8-4573-8ac7-8239d42aa03f'
}
resource ra 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in items(roles): {
  name: guid(resourceGroup().id, principalId, r.value)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.value)
  }
}]
