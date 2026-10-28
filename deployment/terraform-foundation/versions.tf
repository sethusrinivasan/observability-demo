terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket = "observability-demo-telemetry-lake-596090776030"
    key    = "terraform/foundation/terraform.tfstate"
    region = "us-east-2"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
