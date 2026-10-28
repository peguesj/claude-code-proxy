variable "subscription_id" {
  type        = string
  description = "Azure subscription id."
}

variable "prefix" {
  type        = string
  default     = "ccp"
  description = "Name prefix for tags and derived names."
}

variable "groups_file" {
  type        = string
  default     = "../ccp_groups.seed.json"
  description = "Path to ccp_groups.json (or the seed)."
}

variable "region_overrides" {
  type        = map(string)
  default     = {}
  description = "Optional per-group-id region override (e.g. { \"chat-eus\" = \"westus3\" })."
}

variable "deploy_on_demand_models" {
  type        = bool
  default     = false
  description = "on_demand groups provision the account only unless true (PTU deployments bill while idle)."
}

variable "include_legacy" {
  type        = bool
  default     = false
  description = "Manage the legacy-werc group (existing legacy resource group). Off by default: preserved untouched."
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "deploy_deadman" {
  description = "Deploy the Azure Function dead-man switch that deletes idle on_demand deployments."
  type        = bool
  default     = true
}

variable "deadman_location" {
  type    = string
  default = "eastus"
}
