provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      ApplicationName = "Observability-Demo"
      Project         = "Observability-Demo"
      ManagedBy       = "Terraform"
      Environment     = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
