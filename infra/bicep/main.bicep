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
    tags: union(tags, { ccpGroup: g.id, ccpPolicy: g.policy })
  }
}]

output endpoints array = [for (g, i) in activeGroups: {
  id: g.id
  endpoint: accounts[i].outputs.endpoint
}]
