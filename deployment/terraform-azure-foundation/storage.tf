# ==============================================================================
# storage.tf — Frugal Azure Blob Telemetry Lake & Terraform Backend Storage
#
# Enforces:
#  - Standard Tier + LRS (Locally Redundant Storage for maximum frugality)
#  - Blob lifecycle management policy (tier to Cool @ 30d, Archive @ 90d)
#  - Protected against accidental destruction via prevent_destroy = true
#  - ApplicationName = "Observability-Demo" tag
# ==============================================================================

resource "azurerm_storage_account" "lake" {
  name                     = "stobsdemo${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.foundation.name
  location                 = azurerm_resource_group.foundation.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  access_tier              = "Hot"
  min_tls_version          = "TLS1_2"

  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
    container_delete_retention_policy {
      days = 7
    }
  }

  tags = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_name  = azurerm_storage_account.lake.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "telemetry_lake" {
  name                  = "telemetry-lake"
  storage_account_name  = azurerm_storage_account.lake.name
  container_access_type = "private"
}

# Frugal Lifecycle Policy: Auto-tier telemetry to Cool after 30 days, Archive after 90 days
resource "azurerm_storage_management_policy" "lake_lifecycle" {
  storage_account_id = azurerm_storage_account.lake.id

  rule {
    name    = "frugal-telemetry-archival"
    enabled = true
    filters {
      prefix_match = ["telemetry-lake/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 30
        tier_to_archive_after_days_since_modification_greater_than = 90
        delete_after_days_since_modification_greater_than          = 365
      }
      snapshot {
        delete_after_days_since_creation_greater_than = 30
      }
    }
  }
}
