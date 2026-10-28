# ==============================================================================
# log_analytics.tf — Frugal Log Analytics Workspace for Container Apps
#
# Enforces:
#  - SKU: PerGB2018
#  - Retention: 30 days
#  - Daily Ingestion Cap: 0.05 GB (50MB) to strictly prevent runaway logging charges
#  - ApplicationName = "Observability-Demo" tag
# ==============================================================================

resource "azurerm_log_analytics_workspace" "aca_logs" {
  name                = "law-${var.project_name}-${var.environment}"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.05

  tags = local.common_tags
}
