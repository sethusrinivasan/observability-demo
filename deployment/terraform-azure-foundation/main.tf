locals {
  common_tags = {
    ApplicationName = "Observability-Demo"
    Environment     = var.environment
    ManagedBy       = "Terraform"
    CloudProvider   = "Azure"
  }
}

resource "azurerm_resource_group" "foundation" {
  name     = "rg-${var.project_name}-${var.environment}"
  location = var.azure_location
  tags     = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}
