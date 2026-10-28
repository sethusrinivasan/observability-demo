# ==============================================================================
# acr.tf — Frugal Azure Container Registry (Basic SKU)
#
# Enforces:
#  - Basic SKU (~$0.167/day) for low footprint
#  - admin_enabled = false (zero static credentials; authenticated via OIDC Managed Identity)
#  - Protected against accidental destruction via prevent_destroy = true
#  - ApplicationName = "Observability-Demo" tag
# ==============================================================================

resource "azurerm_container_registry" "acr" {
  name                = "acrobsdemo${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.foundation.name
  location            = azurerm_resource_group.foundation.location
  sku                 = "Basic"
  admin_enabled       = false

  tags = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}

# Grant GitHub Actions Managed Identity permission to push images without passwords
resource "azurerm_role_assignment" "github_acr_push" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}
