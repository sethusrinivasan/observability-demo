terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.90"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }

  backend "azurerm" {
    # Backend configuration passed via CLI or deploy_azure.sh
    # container_name = "tfstate"
    # key            = "workload.terraform.tfstate"
  }
}

provider "azurerm" {
  features {}
}
