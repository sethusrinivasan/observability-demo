# ==============================================================================
# postgres.tf — Frugal Azure Database for PostgreSQL Flexible Server
#
# Azure equivalent to AWS Aurora PostgreSQL (reality-check-cost-tracker):
#  - Burstable SKU: B_1ms (1 vCPU, 2GB RAM, ~$12/mo — most frugal tier in Azure)
#  - Storage: 32 GB minimum, auto_grow_enabled = false (strictly prevents billing surprises)
#  - Backup Retention: 7 days
#  - Protected against accidental destruction via lifecycle { prevent_destroy = true }
#  - Query fingerprinting enabled via shared_preload_libraries = "pg_stat_statements"
#  - ApplicationName = "Observability-Demo" tag
# ==============================================================================

resource "random_password" "postgres_admin" {
  length           = 24
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_string" "psql_suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_postgresql_flexible_server" "postgres" {
  name                   = "psql-${var.project_name}-${var.environment}-${random_string.psql_suffix.result}"
  resource_group_name    = data.azurerm_resource_group.rg.name
  location               = data.azurerm_resource_group.rg.location
  version                = "16"
  administrator_login    = "postgres_admin"
  administrator_password = random_password.postgres_admin.result

  # Maximum frugality: Burstable B_1ms (1 vCPU, 2GB RAM)
  sku_name   = "B_Standard_B1ms"
  storage_mb = 32768 # 32 GB minimum

  backup_retention_days = 7
  auto_grow_enabled     = false

  tags = local.common_tags

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "observability" {
  name      = "observability"
  server_id = azurerm_postgresql_flexible_server.postgres.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Allow Azure Container Apps and other internal Azure resources to access PostgreSQL
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure_internal" {
  name             = "allow-azure-internal"
  server_id        = azurerm_postgresql_flexible_server.postgres.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Enable pg_stat_statements for AST Query Fingerprinting & Cost Breakdown
resource "azurerm_postgresql_flexible_server_configuration" "pg_stat_statements" {
  name      = "shared_preload_libraries"
  server_id = azurerm_postgresql_flexible_server.postgres.id
  value     = "pg_stat_statements"
}

# Optional passwordless Entra ID (Azure AD) administrator
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "aad_admin" {
  count               = var.aad_admin_object_id != "" ? 1 : 0
  server_name         = azurerm_postgresql_flexible_server.postgres.name
  resource_group_name = data.azurerm_resource_group.rg.name
  tenant_id           = var.azure_tenant_id
  object_id           = var.aad_admin_object_id
  principal_name      = var.aad_admin_principal_name
  principal_type      = "User"
}
