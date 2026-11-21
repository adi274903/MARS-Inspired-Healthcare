output "service_url" {
  value = google_cloud_run_v2_service.mars.uri
}

output "image" {
  value = local.image
}

output "dashboard" {
  value = "https://console.cloud.google.com/monitoring/dashboards?project=${var.project_id}"
}
