output "canary_dashboard_url" {
  description = "Public HTTPS URL for the Observability Canary Dashboard"
  value       = "https://${azurerm_container_app.services["observability-canary"].ingress[0].fqdn}"
}

output "telemetry_collector_url" {
  description = "Public HTTPS URL for the Custom Telemetry Receiver"
  value       = "https://${azurerm_container_app.services["custom-telemetry-receiver"].ingress[0].fqdn}"
}

output "service_urls" {
  description = "Map of all microservices to their external HTTPS FQDNs"
  value = {
    for name, app in azurerm_container_app.services :
    name => "https://${app.ingress[0].fqdn}"
  }
}

output "postgres_fqdn" {
  description = "Fully Qualified Domain Name of the PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.postgres.fqdn
}

output "postgres_database" {
  description = "Database name on PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server_database.observability.name
}

output "postgres_admin_login" {
  description = "Administrator login for PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.postgres.administrator_login
}

output "workload_identity_id" {
  description = "Resource ID of the Workload User-Assigned Managed Identity"
  value       = azurerm_user_assigned_identity.workload.id
}

output "workload_identity_client_id" {
  description = "Client ID of the Workload User-Assigned Managed Identity"
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "container_app_environment_name" {
  description = "Name of the Azure Container Apps Environment"
  value       = azurerm_container_app_environment.aca_env.name
}

output "application_name_tag" {
  description = "Enforced ApplicationName tag value"
  value       = local.common_tags.ApplicationName
}
