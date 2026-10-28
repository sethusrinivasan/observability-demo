# ==============================================================================
# container_apps.tf — Frugal Serverless Microservices via Azure Container Apps
#
# Enforces:
#  - CPU: 0.25 vCPU & Memory: 0.5GiB per replica (frugal baseline)
#  - min_replicas = 0 (scale-to-zero when idle = $0 idle compute cost)
#  - External HTTPS Ingress with managed TLS certificates
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
