variable "aws_region" {
  description = "AWS region for resources"
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
  description = "Optional custom S3 bucket name. If empty, a unique name is generated."
  type        = string
  default     = ""
}

variable "aurora_db_host" {
  description = "Endpoint of existing shared Aurora Serverless PostgreSQL cluster"
  type        = string
  default     = "reality-check-cost-tracker.cluster-c52ymc6u0ruy.us-east-2.rds.amazonaws.com"
}

variable "aurora_db_port" {
  description = "Port of existing shared Aurora Serverless PostgreSQL cluster"
  type        = number
  default     = 5432
}

variable "aurora_db_name" {
  description = "Database name in shared Aurora Serverless PostgreSQL"
  type        = string
  default     = "observability"
}

variable "aurora_db_user" {
  description = "Database username for shared Aurora Serverless PostgreSQL"
  type        = string
  default     = "postgres"
}

variable "aurora_db_password" {
  description = "Database password for shared Aurora Serverless PostgreSQL"
  type        = string
  default     = ""
  sensitive   = true
}

variable "enable_eventbridge_cron" {
  description = "Enable EventBridge scheduled rules for 5-minute telemetry flush and 1-minute canary"
  type        = bool
  default     = true
}

variable "collector_reserved_concurrency" {
  description = "Reserved concurrency for telemetry collector Lambda (1 ensures maximum 10MB file compaction in warm MicroVM)"
  type        = number
  default     = 1
}

variable "java_memory_size" {
  description = "Memory allocation for Java service (1024 MB provides 1.0 full Graviton vCPU for fast Spring Boot cold start)"
  type        = number
  default     = 1024
}

variable "github_repository" {
  description = "GitHub repository (owner/repo) permitted to assume the OIDC role"
  type        = string
  default     = "sethusrinivasan/observability-demo"
}

variable "create_github_oidc_provider" {
  description = "Whether to create the GitHub Actions OIDC provider in IAM (set to false if already created at account or AWS Organization root)"
  type        = bool
  default     = false # Defaulted to false when account is governed by AWS Organization SCP or pre-existing IdP
}

variable "github_oidc_provider_arn" {
  description = "Optional custom GitHub OIDC provider ARN (used when create_github_oidc_provider is false)"
  type        = string
  default     = ""
}
