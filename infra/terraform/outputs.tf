output "service_url" {
  value = google_cloud_run_v2_service.mars.uri
}

output "image" {
  value = local.image
}
