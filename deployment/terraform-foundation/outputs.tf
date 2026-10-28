output "s3_telemetry_lake_bucket" {
  description = "Name of the foundational S3 telemetry lake & backend state bucket"
  value       = aws_s3_bucket.telemetry_lake.id
}

output "s3_telemetry_lake_arn" {
  description = "ARN of the foundational S3 telemetry lake bucket"
  value       = aws_s3_bucket.telemetry_lake.arn
}

output "github_oidc_role_arn_output" {
  description = "ARN of the CI OIDC deployment role"
  value       = aws_iam_role.github_actions_terraform.arn
}

output "ecr_repositories" {
  description = "Map of service names to ECR repository URLs"
  value = {
    for k, v in aws_ecr_repository.services : k => v.repository_url
  }
}
