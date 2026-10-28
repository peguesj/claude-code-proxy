# Dead-man switch: Consumption Linux Function App deleting idle on_demand (idle_cost) deployments.
# Code lives in infra/deadman/; publish with infra/deploy.sh deadman.
locals {
  on_demand_groups = { for k, g in local.groups : k => g if g.policy == "on_demand" }
}

resource "random_string" "deadman" {
  count   = var.deploy_deadman ? 1 : 0
  length  = 8
  special = false
  upper   = false
}

resource "azurerm_resource_group" "deadman" {
  count    = var.deploy_deadman ? 1 : 0
  name     = "rg-${var.prefix}-deadman-eus"
  location = var.deadman_location
  tags     = local.tags
}

resource "azurerm_storage_account" "deadman" {
  count                           = var.deploy_deadman ? 1 : 0
  name                            = substr("${replace(var.prefix, "-", "")}dm${random_string.deadman[0].result}", 0, 24)
  resource_group_name             = azurerm_resource_group.deadman[0].name
  location                        = azurerm_resource_group.deadman[0].location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_container" "deadman_deploy" {
  count                 = var.deploy_deadman ? 1 : 0
  name                  = "deployments"
  storage_account_id    = azurerm_storage_account.deadman[0].id
  container_access_type = "private"
}

# Flex Consumption (FC1): classic Y1 Consumption quota is 0 in some regions/subscriptions.
resource "azurerm_service_plan" "deadman" {
  count               = var.deploy_deadman ? 1 : 0
  name                = "${var.prefix}-deadman-plan"
  resource_group_name = azurerm_resource_group.deadman[0].name
  location            = azurerm_resource_group.deadman[0].location
  os_type             = "Linux"
  sku_name            = "FC1"
  tags                = local.tags
}

resource "azurerm_function_app_flex_consumption" "deadman" {
  count                       = var.deploy_deadman ? 1 : 0
  name                        = "${var.prefix}-deadman-${random_string.deadman[0].result}"
  resource_group_name         = azurerm_resource_group.deadman[0].name
  location                    = azurerm_resource_group.deadman[0].location
  service_plan_id             = azurerm_service_plan.deadman[0].id
  storage_container_type      = "blobContainer"
  storage_container_endpoint  = "${azurerm_storage_account.deadman[0].primary_blob_endpoint}${azurerm_storage_container.deadman_deploy[0].name}"
  storage_authentication_type = "StorageAccountConnectionString"
  storage_access_key          = azurerm_storage_account.deadman[0].primary_access_key
  runtime_name                = "python"
  runtime_version             = "3.11"
  maximum_instance_count      = 1
  instance_memory_in_mb       = 512
  https_only                  = true
  tags                        = local.tags

  identity {
    type = "SystemAssigned"
  }

  site_config {}

  app_settings = {
    CCP_SUBSCRIPTION_ID = var.subscription_id
    DRY_RUN             = "true"
  }
}

# Least privilege: only the on_demand resource groups.
resource "azurerm_role_assignment" "deadman_cog" {
  for_each             = var.deploy_deadman ? local.on_demand_groups : {}
  scope                = azurerm_resource_group.g[each.key].id
  role_definition_name = "Cognitive Services Contributor"
  principal_id         = azurerm_function_app_flex_consumption.deadman[0].identity[0].principal_id
}

resource "azurerm_role_assignment" "deadman_tag" {
  for_each             = var.deploy_deadman ? local.on_demand_groups : {}
  scope                = azurerm_resource_group.g[each.key].id
  role_definition_name = "Tag Contributor"
  principal_id         = azurerm_function_app_flex_consumption.deadman[0].identity[0].principal_id
}

output "deadman_function" {
  value = var.deploy_deadman ? azurerm_function_app_flex_consumption.deadman[0].name : null
}
