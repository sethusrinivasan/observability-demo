locals {
  common_tags = {
    ApplicationName = "Observability-Demo"
    Environment     = var.environment
    ManagedBy       = "Terraform"
    CloudProvider   = "Azure"
  }

  acr_server = var.acr_login_server != "" ? var.acr_login_server : "mcr.microsoft.com"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}
