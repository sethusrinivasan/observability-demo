output "resource_group_name" {
  description = "Name of the foundational resource group"
  value       = azurerm_resource_group.foundation.name
}

output "storage_account_name" {
  description = "Name of the foundational storage account"
  value       = azurerm_storage_account.lake.name
}

output "storage_container_tfstate" {
  description = "Blob container name for Terraform remote state"
  value       = azurerm_storage_container.tfstate.name
}

output "storage_container_telemetry_lake" {
  description = "Blob container name for the telemetry lake"
  value       = azurerm_storage_container.telemetry_lake.name
}

output "acr_name" {
  description = "Name of the Azure Container Registry"
  value       = azurerm_container_registry.acr.name
}

output "acr_login_server" {
  description = "Login server endpoint for Azure Container Registry"
  value       = azurerm_container_registry.acr.login_server
}

output "github_actions_client_id" {
  description = "Client ID of the Managed Identity for GitHub Actions OIDC"
  value       = azurerm_user_assigned_identity.github_actions.client_id
}

output "application_name_tag" {
  description = "Enforced ApplicationName tag value"
  value       = local.common_tags.ApplicationName
}
