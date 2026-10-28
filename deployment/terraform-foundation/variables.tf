variable "aws_region" {
  description = "AWS region for foundational infrastructure"
  type        = string
  default     = "us-east-2"
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "production"
}

variable "app_name" {
  description = "Application name tag and resource prefix"
  type        = string
  default     = "Observability-Demo"
}

variable "s3_bucket_name" {
  description = "Optional custom S3 bucket name. If empty, observability-demo-telemetry-lake-<ACCOUNT_ID> is used."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "GitHub repository (owner/repo) permitted to assume the OIDC role"
  type        = string
  default     = "sethusrinivasan/observability-demo"
}

variable "create_github_oidc_provider" {
  description = "Whether to create the GitHub Actions OIDC provider in IAM (false if already created at account root)"
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "Optional custom GitHub OIDC provider ARN (used when create_github_oidc_provider is false)"
  type        = string
  default     = ""
}
