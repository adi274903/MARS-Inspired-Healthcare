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
    "compute.googleapis.com", # creates the Compute default service account Cloud Build runs as
    "cloudtrace.googleapis.com",
    "monitoring.googleapis.com",
  ])
  repo           = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.mars.repository_id}"
  image          = "${local.repo}/mars:${var.image_tag}"
  base_image     = "${local.repo}/mars:base"
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
    id     = "keep-base"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["base"]
    }
  }
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

# Telemetry: the runtime account writes traces and custom metrics (OpenTelemetry -> Cloud Trace / Cloud Monitoring).
resource "google_project_iam_member" "run_telemetry" {
  for_each   = toset(["roles/cloudtrace.agent", "roles/monitoring.metricWriter"])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${google_service_account.run.email}"
  depends_on = [google_project_service.apis]
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
    # Code-only layer on the `base` image (deps + model + data); upload excludes data/ and models/.
    command = "gcloud builds submit . --project=${var.project_id} --config=cloudbuild.app.yaml --ignore-file=.gcloudignore.app --timeout=1800s --substitutions=_BASE=${local.base_image},_IMAGE=${local.image}"
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
    max_instance_request_concurrency = var.max_request_concurrency

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
        name  = "PUBLIC_API_ONLY" # default-deny API, rate limits, header hardening (core/security.py)
        value = "1"
      }
      env {
        name  = "RATE_LIMIT_PER_MIN"
        value = tostring(var.rate_limit_per_min)
      }
      env {
        name  = "DAILY_QUESTION_CAP"
        value = tostring(var.daily_question_cap)
      }
      env {
        name  = "GUIDED_JSON" # the free Gemma endpoint has no structured-output support (OpenRouter 404 "no endpoints")
        value = "0"
      }
      env {
        name  = "LLM_TIMEOUT" # a hung upstream call must free its concurrency slot quickly
        value = "45"
      }
      env {
        name  = "CHAT_TIMEOUT_S"
        value = "60"
      }
      env {
        name  = "OTEL_ENABLED"
        value = "1"
      }
      env {
        name  = "OTEL_EXPORTER"
        value = "gcp"
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

  depends_on = [terraform_data.image, google_secret_manager_secret_iam_member.run_access, google_project_iam_member.run_telemetry]
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
    # Not /healthz: Cloud Run's front end reserves that path and answers 404 itself, so it never wakes the container.
    uri = "${google_cloud_run_v2_service.mars.uri}/"
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

# ---------------------------------------------------------------- latency dashboard (p50 / p95 / p99)
locals {
  pct = { p50 = "ALIGN_PERCENTILE_50", p95 = "ALIGN_PERCENTILE_95", p99 = "ALIGN_PERCENTILE_99" }

  # One chart of three percentile lines for a distribution metric.
  latency_widget = { for title, spec in {
    "Cloud Run request latency, all requests (ms)" = {
      filter = "metric.type=\"run.googleapis.com/request_latencies\" resource.type=\"cloud_run_revision\" resource.label.\"service_name\"=\"${var.service_name}\""
    }
    "Question latency, end to end (ms)" = {
      filter = "metric.type=\"workload.googleapis.com/mars.chat.duration_ms\""
    }
    "Time to first token (ms)" = {
      filter = "metric.type=\"workload.googleapis.com/mars.chat.ttft_ms\""
    }
    "Retrieval until answer starts (ms)" = {
      filter = "metric.type=\"workload.googleapis.com/mars.chat.retrieval_ms\""
    }
    } : title => {
    title = title
    xyChart = {
      dataSets = [for label, aligner in local.pct : {
        plotType       = "LINE"
        legendTemplate = label
        timeSeriesQuery = {
          timeSeriesFilter = {
            filter = spec.filter
            aggregation = {
              alignmentPeriod    = "300s"
              perSeriesAligner   = aligner
              crossSeriesReducer = "REDUCE_MEAN"
            }
          }
        }
      }]
      yAxis = { scale = "LINEAR" }
    }
  } }

  stage_widget = {
    title = "Per-stage p95 (ms): sqlite / vector / graph / llm / answer"
    xyChart = {
      dataSets = [{
        plotType       = "LINE"
        legendTemplate = "$${metric.labels.stage}"
        timeSeriesQuery = {
          timeSeriesFilter = {
            filter = "metric.type=\"workload.googleapis.com/mars.stage.duration_ms\""
            aggregation = {
              alignmentPeriod    = "300s"
              perSeriesAligner   = "ALIGN_PERCENTILE_95"
              crossSeriesReducer = "REDUCE_MEAN"
              groupByFields      = ["metric.label.\"stage\""]
            }
          }
        }
      }]
      yAxis = { scale = "LINEAR" }
    }
  }
}

resource "google_monitoring_dashboard" "latency" {
  dashboard_json = jsonencode({
    displayName = "MARS latency (p50 / p95 / p99)"
    mosaicLayout = {
      columns = 12
      tiles = [for i, w in concat(values(local.latency_widget), [local.stage_widget]) : {
        xPos   = (i % 2) * 6
        yPos   = floor(i / 2) * 4
        width  = 6
        height = 4
        widget = w
      }]
    }
  })
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- privacy: questions appear in request logs, keep them briefly
resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  bucket_id      = "_Default"
  retention_days = var.log_retention_days
  depends_on     = [google_project_service.apis]
}
