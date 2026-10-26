output "service_account_email" {
  description = "Email of the least-privilege PyForge agent service account."
  value       = google_service_account.pyforge_agent.email
}

output "cloud_run_service_uri" {
  description = "Deployed Cloud Run v2 endpoint URL for PyForge Studio."
  value       = google_cloud_run_v2_service.pyforge_service.uri
}

output "state_bucket_name" {
  description = "Cloud Storage bucket for persistent agent state and evaluation logs."
  value       = google_storage_bucket.pyforge_state.name
}

output "secret_manager_secret_id" {
  description = "Google Cloud Secret Manager resource ID for GOOGLE_API_KEY."
  value       = google_secret_manager_secret.gemini_api_key.id
}

