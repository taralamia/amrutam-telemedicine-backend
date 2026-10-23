terraform {
  required_version = ">= 1.10.0"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = "amrutam-telemedicine"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
