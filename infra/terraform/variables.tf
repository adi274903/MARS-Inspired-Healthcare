variable "project_id" {
  description = "GCP project that hosts MARS (billing must be enabled)."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and Cloud Scheduler."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  type    = string
  default = "mars-healthcare"
}

variable "image_tag" {
  description = "Tag for the image Cloud Build produces; changing it triggers a rebuild and a new revision."
  type        = string
}

variable "source_dir" {
  description = "Local checkout uploaded to Cloud Build (includes data/ and models/, which are not in git)."
  type        = string
  default     = "../.."
}

variable "cpu" {
  type    = string
  default = "2"
}

variable "memory" {
  description = "8Gi: a 4Gi instance was OOM-killed while loading the embedding model (measured 4133 MiB)."
  type        = string
  default     = "8Gi"
}

variable "max_request_concurrency" {
  description = "Requests one instance handles at once; kept low because each question runs a CPU embedding pass."
  type        = number
  default     = 4
}

variable "max_instances" {
  description = "Hard cap on instances; with min 0 this bounds the worst-case bill."
  type        = number
  default     = 1
}

variable "cors_origins" {
  description = "Comma-separated origins allowed to call the API (the GitHub Pages site)."
  type        = string
  default     = "https://adi274903.github.io"
}

variable "keep_warm" {
  description = "Ping /healthz on a schedule so the instance (and its loaded embedding model) stays warm. Idle time is not billed."
  type        = bool
  default     = true
}

variable "keep_warm_schedule" {
  type    = string
  default = "*/10 * * * *"
}

variable "keep_warm_timezone" {
  type    = string
  default = "America/Los_Angeles"
}

variable "billing_account_id" {
  description = "Billing account for the budget alert (XXXXXX-XXXXXX-XXXXXX). Empty skips the budget."
  type        = string
  default     = ""
}

variable "budget_usd" {
  type    = number
  default = 5
}
