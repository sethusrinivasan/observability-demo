# ==============================================================================
# identity.tf — Workload User-Assigned Managed Identity (Passwordless Access)
#
# Eliminates persisted credentials & static secrets:
#  - Container Apps authenticate to ACR via AcrPull Managed Identity role
#  - Container Apps authenticate to Azure Storage via Storage Blob Data Contributor
#  - Container Apps authenticate to PostgreSQL Flexible Server via Entra ID
# ==============================================================================

resource "azurerm_user_assigned_identity" "workload" {
  name                = "uami-${var.project_name}-${var.environment}-workload"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  tags                = local.common_tags
}

data "azurerm_container_registry" "acr" {
  count               = var.acr_name != "" ? 1 : 0
  name                = var.acr_name
  resource_group_name = data.azurerm_resource_group.rg.name
}

resource "azurerm_role_assignment" "workload_acr_pull" {
  count                = var.acr_name != "" ? 1 : 0
  scope                = data.azurerm_container_registry.acr[0].id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}

data "azurerm_storage_account" "lake" {
  count               = var.storage_account_name != "" ? 1 : 0
  name                = var.storage_account_name
  resource_group_name = data.azurerm_resource_group.rg.name
}

resource "azurerm_role_assignment" "workload_storage_blob_contributor" {
  count                = var.storage_account_name != "" ? 1 : 0
  scope                = data.azurerm_storage_account.lake[0].id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}
