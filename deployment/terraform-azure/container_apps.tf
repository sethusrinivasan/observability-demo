# ==============================================================================
# container_apps.tf — Frugal Serverless Microservices via Azure Container Apps
#
# Enforces:
#  - CPU: 0.25 vCPU & Memory: 0.5GiB per replica (frugal baseline)
#  - min_replicas = 0 (scale-to-zero when idle = $0 idle compute cost)
#  - External HTTPS Ingress with managed TLS certificates
#  - Passwordless ACR pulls via User-Assigned Managed Identity
#  - Mandatory ApplicationName = "Observability-Demo" tag
# ==============================================================================

locals {
  services = {
    "observability-python-app" = {
      port = 8000
    }
    "observability-java-app" = {
      port = 8080
    }
    "observability-rust-app" = {
      port = 8080
    }
    "observability-node-app" = {
      port = 3000
    }
    "observability-go-app" = {
      port = 8080
    }
    "observability-dotnet-app" = {
      port = 8080
    }
    "observability-c-app" = {
      port = 8080
    }
    "observability-php-app" = {
      port = 8080
    }
    "observability-swift-app" = {
      port = 8080
    }
    "observability-canary" = {
      port = 8000
    }
    "custom-telemetry-receiver" = {
      port = 4318
    }
  }
}

resource "azurerm_container_app" "services" {
  for_each = local.services

  name                         = each.key
  container_app_environment_id = azurerm_container_app_environment.aca_env.id
  resource_group_name          = data.azurerm_resource_group.rg.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.workload.id]
  }

  dynamic "registry" {
    for_each = var.acr_login_server != "" ? [var.acr_login_server] : []
    content {
      server   = registry.value
      identity = azurerm_user_assigned_identity.workload.id
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = each.key
      image  = "${local.acr_server}/${each.key}:${var.image_tag}"
      cpu    = var.cpu_allocation
      memory = var.memory_allocation

      env {
        name  = "APPLICATION_NAME"
        value = "Observability-Demo"
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "http://custom-telemetry-receiver:4318"
      }
      env {
        name  = "TELEMETRY_LAKE_CONTAINER"
        value = "telemetry-lake"
      }

      dynamic "env" {
        for_each = each.key == "observability-canary" ? [
          { name = "PORT", value = "8000" },
          { name = "C_APP_BASE_URL", value = "http://observability-c-app:8080" },
          { name = "RUST_APP_BASE_URL", value = "http://observability-rust-app:8080" },
          { name = "GO_APP_BASE_URL", value = "http://observability-go-app:8080" },
          { name = "NODE_APP_BASE_URL", value = "http://observability-node-app:3000" },
          { name = "PYTHON_APP_BASE_URL", value = "http://observability-python-app:8000" },
          { name = "APP_BASE_URL", value = "http://observability-python-app:8000" },
          { name = "DOTNET_APP_BASE_URL", value = "http://observability-dotnet-app:8080" },
          { name = "PHP_APP_BASE_URL", value = "http://observability-php-app:8080" },
          { name = "SWIFT_APP_BASE_URL", value = "http://observability-swift-app:8080" },
          { name = "JAVA_APP_BASE_URL", value = "http://observability-java-app:8080" },
          { name = "TELEMETRY_RECEIVER_URL", value = "http://custom-telemetry-receiver:4318" },
          { name = "POSTGRES_HOST", value = azurerm_postgresql_flexible_server.postgres.fqdn },
          { name = "POSTGRES_PORT", value = "5432" },
          { name = "POSTGRES_DB", value = azurerm_postgresql_flexible_server_database.observability.name },
          { name = "POSTGRES_USER", value = azurerm_postgresql_flexible_server.postgres.administrator_login },
          { name = "POSTGRES_PASSWORD", value = random_password.postgres_admin.result },
          { name = "CLOUD_PROVIDER", value = "azure" },
          { name = "AZURE_STORAGE_ACCOUNT", value = var.storage_account_name },
          { name = "CANARY_RATE_UNIT", value = "TPM" },
          { name = "CANARY_TPM", value = "6" }
        ] : []
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      dynamic "env" {
        for_each = each.key == "custom-telemetry-receiver" ? [
          { name = "PORT", value = "4318" },
          { name = "AZURE_STORAGE_ACCOUNT", value = var.storage_account_name },
          { name = "MAX_PARQUET_FILE_SIZE_MB", value = "10" },
          { name = "OTEL_FLUSH_COUNT_THRESHOLD", value = "1000" },
          { name = "OTEL_FLUSH_TIME_THRESHOLD_SECONDS", value = "300" },
          { name = "CLOUD_PROVIDER", value = "azure" }
        ] : []
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = each.value.port
    transport        = "auto"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = local.common_tags
}
