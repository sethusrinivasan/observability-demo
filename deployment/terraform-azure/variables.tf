variable "azure_location" {
  description = "The Azure region for workload deployment"
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

variable "resource_group_name" {
  description = "Name of the foundational resource group hosting storage and ACR"
  type        = string
  default     = "rg-observability-demo-prod"
}

variable "storage_account_name" {
  description = "Name of the foundational storage account for telemetry lake"
  type        = string
  default     = ""
}

variable "acr_login_server" {
  description = "ACR login server endpoint (or custom container registry)"
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Image tag to deploy for microservice containers"
  type        = string
  default     = "latest"
}

variable "min_replicas" {
  description = "Minimum replicas for Container Apps (0 enables scale-to-zero for maximum frugality)"
  type        = number
  default     = 0
}

variable "max_replicas" {
  description = "Maximum replicas for Container Apps"
  type        = number
  default     = 1
}

variable "cpu_allocation" {
  description = "vCPU allocation per Container App replica (frugal baseline)"
  type        = number
  default     = 0.25
}

variable "memory_allocation" {
  description = "Memory allocation per Container App replica (frugal baseline)"
  type        = string
  default     = "0.5Gi"
}
