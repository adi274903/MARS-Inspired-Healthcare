terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # Budgets API calls are billed to this project when running with user credentials.
  billing_project       = var.project_id
  user_project_override = true
}
