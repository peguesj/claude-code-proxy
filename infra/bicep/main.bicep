// CCP model groups: per group -> resource group + AIServices account + deployments.
// Group shape mirrors ccp_groups.json. Generate the param file with:
//   infra/deploy.sh params
targetScope = 'subscription'

@description('Groups from ccp_groups.json ("groups" array).')
param groups array

@description('Prefix for the deployment names and tags.')
param prefix string = 'ccp'

@description('on_demand groups provision the account only unless true.')
param deployOnDemandModels bool = false

@description('The legacy group (existing legacy resource group) is preserved untouched unless explicitly included.')
param includeLegacy bool = false

@description('Deploy the Azure dead-man switch that deletes idle on_demand deployments.')
param deployDeadman bool = true

@description('Resource group and region for the dead-man Function App.')
param deadmanResourceGroup string = 'rg-${prefix}-deadman-eus'
param deadmanLocation string = 'eastus'

var tags = {
  project: 'claude-code-proxy'
  managedBy: 'ccp-infra'
}

var activeGroups = filter(groups, g => includeLegacy || g.id != 'legacy-werc')

module rgs 'modules/rg.bicep' = [for g in activeGroups: {
  name: '${prefix}-rg-${g.id}'
  params: {
    name: g.resource_group
    location: g.region
    tags: union(tags, { ccpGroup: g.id, ccpPolicy: g.policy, ccpBilling: g.billing })
  }
}]

module accounts 'modules/group.bicep' = [for (g, i) in activeGroups: {
  name: '${prefix}-grp-${g.id}'
  scope: resourceGroup(g.resource_group)
  dependsOn: [rgs[i]]
  params: {
    location: g.region
    accountName: g.account
    models: g.models
    deployModels: g.policy == 'always_on' || deployOnDemandModels
    tags: union(tags, { ccpGroup: g.id, ccpPolicy: g.policy },
      g.policy == 'on_demand' ? { 'ccp-policy': 'on_demand', 'ccp-idle-minutes': string(g.idle_teardown_minutes) } : {})
  }
}]

var onDemandGroups = filter(activeGroups, g => g.policy == 'on_demand')

resource deadmanRg 'Microsoft.Resources/resourceGroups@2024-03-01' = if (deployDeadman) {
  name: deadmanResourceGroup
  location: deadmanLocation
  tags: tags
}

module deadman 'modules/deadman.bicep' = if (deployDeadman) {
  name: '${prefix}-deadman'
  scope: resourceGroup(deadmanResourceGroup)
  dependsOn: [deadmanRg]
  params: { location: deadmanLocation, prefix: prefix, tags: tags }
}

module deadmanRoles 'modules/deadman-role.bicep' = [for g in onDemandGroups: if (deployDeadman) {
  name: '${prefix}-deadman-role-${g.id}'
  scope: resourceGroup(g.resource_group)
  dependsOn: [rgs]
  params: { principalId: deadman!.outputs.principalId }
}]

output endpoints array = [for (g, i) in activeGroups: {
  id: g.id
  endpoint: accounts[i].outputs.endpoint
}]
