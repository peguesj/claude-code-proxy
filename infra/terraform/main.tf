terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  registry = jsondecode(file(var.groups_file))

  groups = {
    for g in local.registry.groups : g.id => g
    if var.include_legacy || g.id != "legacy-werc"
  }

  # Flatten group -> model; deployed for always_on groups, or on_demand when opted in.
  deployments = {
    for pair in flatten([
      for gid, g in local.groups : [
        for m in g.models : {
          key   = "${gid}/${m.deployment}"
          group = gid
          model = m
        }
        if g.policy == "always_on" || var.deploy_on_demand_models
      ]
    ]) : pair.key => pair
  }

  tags = merge(var.tags, { project = "claude-code-proxy", managedBy = "ccp-infra" })
}

resource "azurerm_resource_group" "g" {
  for_each = local.groups
  name     = each.value.resource_group
  location = lookup(var.region_overrides, each.key, each.value.region)
  tags     = merge(local.tags, { ccpGroup = each.key, ccpPolicy = each.value.policy, ccpBilling = each.value.billing })
}

resource "azurerm_cognitive_account" "g" {
  for_each              = local.groups
  name                  = each.value.account
  location              = azurerm_resource_group.g[each.key].location
  resource_group_name   = azurerm_resource_group.g[each.key].name
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = each.value.account
  tags                  = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_deployment" "m" {
  for_each             = local.deployments
  name                 = each.value.model.deployment
  cognitive_account_id = azurerm_cognitive_account.g[each.value.group].id

  model {
    format  = each.value.model.format
    name    = each.value.model.name
    version = each.value.model.version
  }

  sku {
    name     = each.value.model.sku
    capacity = each.value.model.capacity
  }
}
