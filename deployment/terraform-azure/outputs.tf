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

output "container_app_environment_name" {
  description = "Name of the Azure Container Apps Environment"
  value       = azurerm_container_app_environment.aca_env.name
}

output "application_name_tag" {
  description = "Enforced ApplicationName tag value"
  value       = local.common_tags.ApplicationName
}
