variable "project_id" {
  description = "Google Cloud Project ID hosting PyForge resources."
  type        = string
}

variable "region" {
  description = "Default Google Cloud region for Cloud Run and Vertex AI."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the PyForge agent service."
  type        = string
  default     = "pyforge-studio"
}

variable "container_image" {
  description = "Container image URI deployed to Cloud Run."
  type        = string
  default     = "us-central1-docker.pkg.dev/my-project/pyforge-studio/pyforge:latest"
}

variable "gemini_api_key_secret_id" {
  description = "Secret Manager secret ID storing the Gemini API key."
  type        = string
  default     = "pyforge-gemini-api-key"
}

variable "architect_model" {
  description = "Gemini Pro model routed for architectural specification planning."
  type        = string
  default     = "gemini-2.5-pro"
}

variable "coder_model" {
  description = "Gemini Flash model routed for fast code generation and self-healing."
  type        = string
  default     = "gemini-2.5-flash"
}
