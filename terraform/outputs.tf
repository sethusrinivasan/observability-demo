output "application_name_tag" {
  description = "ApplicationName tag applied to all project resources"
  value       = "Observability-Demo"
}

output "s3_telemetry_lake_bucket" {
  description = "Name of the S3 Telemetry Lake bucket"
  value       = aws_s3_bucket.telemetry_lake.id
}

output "canary_dashboard_url" {
  description = "Direct HTTPS Function URL for Canary Dashboard & DuckDB Query Editor"
  value       = aws_lambda_function_url.canary_url.function_url
}

output "telemetry_collector_url" {
  description = "Direct HTTPS Function URL for Custom Telemetry Receiver (OTLP 4318)"
  value       = aws_lambda_function_url.collector_url.function_url
}

output "microservice_urls" {
  description = "Map of direct HTTPS Function URLs for all 7 polyglot microservices"
  value = {
    for k, v in aws_lambda_function_url.apps_url : k => v.function_url
  }
}

output "ecr_repository_urls" {
  description = "Map of ECR repository URLs for pushing arm64 images"
  value = {
    for k, v in aws_ecr_repository.services : k => v.repository_url
  }
}
