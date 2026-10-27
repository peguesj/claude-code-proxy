output "endpoints" {
  description = "Per-group AIServices endpoints."
  value       = { for k, a in azurerm_cognitive_account.g : k => a.endpoint }
}
