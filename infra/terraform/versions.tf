terraform {
  required_version = ">= 1.10"

  # Exact versions are recorded in .terraform.lock.hcl (committed). State is local: the
  # environment is disposable and created and destroyed from one workstation (ADR-0011).
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8.1"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  # Refuses to plan or apply against any other account, whatever profile is active.
  allowed_account_ids = [var.allowed_account_id]

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = var.repository_tag
    }
  }
}
