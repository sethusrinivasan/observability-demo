# ==============================================================================
# event_grid.tf — Frugal Serverless Event Dispatching via Azure Event Grid
#
# Azure equivalent to AWS EventBridge.
# First 100,000 operations per month are free, then $0.60 per million.
# Tracks telemetry lake events and dispatches notifications.
# ==============================================================================

resource "azurerm_eventgrid_topic" "telemetry_events" {
  name                = "evgt-${var.project_name}-${var.environment}"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name

  input_schema = "EventGridSchema"
  tags         = local.common_tags
}
