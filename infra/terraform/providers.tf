terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Same state bucket as the Agena and Seduh stacks, separate key: `terraform init -backend-config=backend.hcl`.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { project = "samawi-bot", ManagedBy = "terraform" }
  }
}
