# ==============================================================================
# container_apps_env.tf — Serverless Azure Container Apps Managed Environment
#
# Serverless consumption environment linked to frugal Log Analytics workspace.
# Supports scale-to-zero microservices with built-in HTTPS ingress.
# ==============================================================================

resource "azurerm_container_app_environment" "aca_env" {
  name                       = "cae-${var.project_name}-${var.environment}"
  location                   = data.azurerm_resource_group.rg.location
  resource_group_name        = data.azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.aca_logs.id

  tags = local.common_tags
}
