data "google_project" "this" {
  project_id = var.project_id
}

locals {
  apis = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
  image          = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.mars.repository_id}/mars:${var.image_tag}"
  build_sa_email = "${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

# ---------------------------------------------------------------- APIs
resource "google_project_service" "apis" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- image registry (keeps only the newest 2 images)
resource "google_artifact_registry_repository" "mars" {
  location               = var.region
  repository_id          = "mars"
  format                 = "DOCKER"
  description            = "MARS-Healthcare images"
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-newest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s"
    }
  }

  depends_on = [google_project_service.apis]
}

# Cloud Build runs as the Compute Engine default service account in newer projects; give it what a build needs.
resource "google_project_iam_member" "build_sa" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
    "roles/storage.objectViewer",
  ])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${local.build_sa_email}"
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- secrets (values are added by deploy.sh, never stored in tfstate)
resource "google_secret_manager_secret" "openrouter" {
  secret_id = "openrouter-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "admin" {
  secret_id = "mars-admin-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- runtime identity
resource "google_service_account" "run" {
  account_id   = "mars-run"
  display_name = "MARS-Healthcare Cloud Run runtime"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "run_access" {
  for_each  = { openrouter = google_secret_manager_secret.openrouter.id, admin = google_secret_manager_secret.admin.id }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------------------------------------------------------------- build (Cloud Build, from the local checkout)
resource "terraform_data" "image" {
  triggers_replace = [var.image_tag]

  provisioner "local-exec" {
    working_dir = var.source_dir
    command     = "gcloud builds submit . --project=${var.project_id} --tag=${local.image} --timeout=3600s"
  }

  depends_on = [google_artifact_registry_repository.mars, google_project_iam_member.build_sa]
}

# ---------------------------------------------------------------- service
resource "google_cloud_run_v2_service" "mars" {
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.run.email
    timeout                          = "300s"
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = true # request-based billing: no charge while idle
        startup_cpu_boost = true
      }

      env {
        name  = "CORS_ORIGINS"
        value = var.cors_origins
      }
      env {
        name = "OPENROUTER_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openrouter.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "ADMIN_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.admin.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        period_seconds    = 10
        timeout_seconds   = 5
        failure_threshold = 24
      }
    }
  }

  depends_on = [terraform_data.image, google_secret_manager_secret_iam_member.run_access]
}

# Public demo API (the GitHub Pages UI calls it from the browser).
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.mars.name
  location = google_cloud_run_v2_service.mars.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------- keep-warm ping (free tier covers 3 jobs per billing account)
resource "google_cloud_scheduler_job" "keep_warm" {
  count            = var.keep_warm ? 1 : 0
  name             = "${var.service_name}-keep-warm"
  region           = var.region
  schedule         = var.keep_warm_schedule
  time_zone        = var.keep_warm_timezone
  attempt_deadline = "60s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "GET"
    uri         = "${google_cloud_run_v2_service.mars.uri}/healthz"
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- budget alert (emails billing admins at 50/90/100%)
resource "google_billing_budget" "mars" {
  count           = var.billing_account_id == "" ? 0 : 1
  billing_account = var.billing_account_id
  display_name    = "${var.service_name}-budget"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.apis]
}
