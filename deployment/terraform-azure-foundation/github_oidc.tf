# ==============================================================================
# github_oidc.tf — Azure Workload Identity Federation (OIDC) for GitHub Actions
#
# Eliminates all persisted secrets (no client secrets, certificates, or passwords).
# GitHub Actions issues an OIDC JWT; Microsoft Entra ID validates the signature
# and exchanges it for a short-lived Azure Access Token.
# ==============================================================================

resource "azurerm_user_assigned_identity" "github_actions" {
  name                = "uami-observability-demo-github-ci"
  resource_group_name = azurerm_resource_group.foundation.name
  location            = azurerm_resource_group.foundation.location
  tags                = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}

# Federated Identity Credential for Main Branch pushes
resource "azurerm_federated_identity_credential" "github_main" {
  name                = "gh-actions-main"
  resource_group_name = azurerm_resource_group.foundation.name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  parent_id           = azurerm_user_assigned_identity.github_actions.id
  subject             = "repo:${var.github_repository}:ref:refs/heads/main"
}

# Federated Identity Credential for Pull Requests
resource "azurerm_federated_identity_credential" "github_pr" {
  name                = "gh-actions-pr"
  resource_group_name = azurerm_resource_group.foundation.name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  parent_id           = azurerm_user_assigned_identity.github_actions.id
  subject             = "repo:${var.github_repository}:pull_request"
}

# Role Assignment: Contributor on Foundation & Workload Resource Group
resource "azurerm_role_assignment" "github_rg_contributor" {
  scope                = azurerm_resource_group.foundation.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# Role Assignment: User Access Administrator on Resource Group (allows assigning AcrPull / Blob roles to workload Managed Identity)
resource "azurerm_role_assignment" "github_rg_user_access_admin" {
  scope                = azurerm_resource_group.foundation.id
  role_definition_name = "User Access Administrator"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# Role Assignment: Storage Blob Data Contributor on Foundational Telemetry Lake
resource "azurerm_role_assignment" "github_storage_blob_contributor" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}
