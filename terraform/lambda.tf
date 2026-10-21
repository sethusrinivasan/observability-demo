# ---------------------------------------------------------------------------
# 1. Polyglot Microservices on 128 MB Graviton MicroVMs (Firecracker)
# ---------------------------------------------------------------------------
locals {
  microservices_config = {
    "c" = {
      image_name  = "observability-c-app"
      port        = 8080
      memory_size = 128
    }
    "rust" = {
      image_name  = "observability-rust-app"
      port        = 8081
      memory_size = 128
    }
    "go" = {
      image_name  = "observability-go-app"
      port        = 8080
      memory_size = 128
    }
    "node" = {
      image_name  = "observability-node-app"
      port        = 8080
      memory_size = 128
    }
    "python" = {
      image_name  = "observability-python-app"
      port        = 5000
      memory_size = 256
    }
    "dotnet" = {
      image_name  = "observability-dotnet-app"
      port        = 8080
      memory_size = 128
    }
    "java" = {
      image_name  = "observability-java-app"
      port        = 8080
      memory_size = var.java_memory_size
    }
  }
}

resource "aws_lambda_function" "apps" {
  for_each      = local.microservices_config
  function_name = "observability-${each.key}-app"
  role          = aws_iam_role.microservice_role.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.services[each.value.image_name].repository_url}:latest"
  architectures = ["arm64"]
  memory_size   = each.value.memory_size
  timeout       = each.key == "java" ? 30 : 15

  environment {
    variables = {
      PORT                              = tostring(each.value.port)
      AWS_LWA_READINESS_CHECK_PATH      = "/version"
      AWS_LWA_INVOKE_MODE               = "buffered"
      AWS_LWA_POOL_IDLE_TIMEOUT_SECONDS = "0"
      POSTGRES_HOST                     = var.aurora_db_host
      POSTGRES_PORT                     = tostring(var.aurora_db_port)
      POSTGRES_DB                       = var.aurora_db_name
      POSTGRES_USER                     = var.aurora_db_user
      POSTGRES_PASSWORD                 = var.aurora_db_password
      APPLICATION_NAME                  = "observability-${each.key}-app"
      DATABASE_URL                      = "postgres://${var.aurora_db_user}:${var.aurora_db_password}@${var.aurora_db_host}:${var.aurora_db_port}/${var.aurora_db_name}?application_name=observability-${each.key}-app"
      SPRING_DATASOURCE_URL             = "jdbc:postgresql://${var.aurora_db_host}:${var.aurora_db_port}/${var.aurora_db_name}?sslmode=prefer&ApplicationName=observability-${each.key}-app"
      SPRING_DATASOURCE_USERNAME        = var.aurora_db_user
      SPRING_DATASOURCE_PASSWORD        = var.aurora_db_password
      MAX_DB_CONNECTIONS                = "2" # Frugal pool cap per MicroVM
      OTEL_EXPORTER_OTLP_ENDPOINT       = aws_lambda_function_url.collector_url.function_url
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "apps_url" {
  for_each           = aws_lambda_function.apps
  function_name      = each.value.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}

# ---------------------------------------------------------------------------
# 2. Custom High-Throughput Telemetry Receiver (Lambda MicroVM)
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "collector" {
  function_name                  = "observability-telemetry-collector"
  role                           = aws_iam_role.collector_role.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.services["custom-telemetry-receiver"].repository_url}:latest"
  architectures                  = ["arm64"]
  memory_size                    = 256
  timeout                        = 30
  reserved_concurrent_executions = var.collector_reserved_concurrency

  environment {
    variables = {
      PORT                              = "4318"
      AWS_LWA_READINESS_CHECK_PATH      = "/health"
      AWS_LWA_INVOKE_MODE               = "buffered"
      AWS_LWA_POOL_IDLE_TIMEOUT_SECONDS = "0"
      S3_BUCKET_NAME                    = aws_s3_bucket.telemetry_lake.id
      S3_ENDPOINT_URL                   = "https://s3.${var.aws_region}.amazonaws.com"
      BUFFER_DIR                        = "/tmp/telemetry_buffer"
      MAX_PARQUET_FILE_SIZE_BYTES       = "10485760" # 10 MB Compaction limit
      DRAIN_BATCH_SIZE                  = "1000"
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "collector_url" {
  function_name      = aws_lambda_function.collector.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}

# ---------------------------------------------------------------------------
# 3. Canary Dashboard & DuckDB OLAP Engine (Lambda Web Function)
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "canary" {
  function_name = "observability-canary-dashboard"
  role          = aws_iam_role.canary_role.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.services["observability-canary"].repository_url}:latest"
  architectures = ["arm64"]
  memory_size   = 256 # 256 MB accommodates DuckDB in-process OLAP execution
  timeout       = 30

  environment {
    variables = {
      PORT                              = "8085"
      AWS_LWA_READINESS_CHECK_PATH      = "/health"
      AWS_LWA_INVOKE_MODE               = "buffered"
      AWS_LWA_POOL_IDLE_TIMEOUT_SECONDS = "0"
      S3_BUCKET_NAME                    = aws_s3_bucket.telemetry_lake.id
      S3_ENDPOINT_URL                   = "https://s3.${var.aws_region}.amazonaws.com"
      TELEMETRY_RECEIVER_URL            = aws_lambda_function_url.collector_url.function_url
      POSTGRES_HOST                     = var.aurora_db_host
      POSTGRES_PORT                     = tostring(var.aurora_db_port)
      POSTGRES_DB                       = var.aurora_db_name
      POSTGRES_USER                     = var.aurora_db_user
      POSTGRES_PASSWORD                 = var.aurora_db_password
      APPLICATION_NAME                  = "observability-canary-dashboard"
      C_APP_BASE_URL                    = aws_lambda_function_url.apps_url["c"].function_url
      RUST_APP_BASE_URL                 = aws_lambda_function_url.apps_url["rust"].function_url
      GO_APP_BASE_URL                   = aws_lambda_function_url.apps_url["go"].function_url
      NODE_APP_BASE_URL                 = aws_lambda_function_url.apps_url["node"].function_url
      PYTHON_APP_BASE_URL               = aws_lambda_function_url.apps_url["python"].function_url
      APP_BASE_URL                      = aws_lambda_function_url.apps_url["python"].function_url
      DOTNET_APP_BASE_URL               = aws_lambda_function_url.apps_url["dotnet"].function_url
      JAVA_APP_BASE_URL                 = aws_lambda_function_url.apps_url["java"].function_url
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "canary_url" {
  function_name      = aws_lambda_function.canary.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}

resource "aws_lambda_permission" "canary_url_allow" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.canary.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "canary_url_invoke" {
  statement_id  = "FunctionURLInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.canary.function_name
  principal     = "*"
}

resource "aws_lambda_permission" "collector_url_allow" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.collector.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "collector_url_invoke" {
  statement_id  = "FunctionURLInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "*"
}

resource "aws_lambda_permission" "apps_url_invoke" {
  for_each      = aws_lambda_function.apps
  statement_id  = "FunctionURLInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = each.value.function_name
  principal     = "*"
}
