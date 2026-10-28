variable "azure_location" {
  description = "The Azure region for foundational resources (cost-effective region)"
  type        = string
  default     = "eastus2"
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "prod"
}

variable "project_name" {
  description = "Name of the project"
  type        = string
  default     = "observability-demo"
}

variable "github_repository" {
  description = "The GitHub repository in 'owner/repo' format for OIDC workload federation"
  type        = string
  default     = "sethusrinivasan/observability-demo"
}
