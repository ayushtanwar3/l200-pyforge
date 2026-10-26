terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required Google Cloud APIs for AgentOps, Vertex AI, Secret Manager & Observability
locals {
  required_apis = [
    "aiplatform.googleapis.com",
    "generativelanguage.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudtrace.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ]
}

resource "google_project_service" "agent_apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 2. Dedicated Least-Privilege Service Account for PyForge Agent Runtime
resource "google_service_account" "pyforge_agent" {
  account_id   = "${var.service_name}-agent-sa"
  display_name = "PyForge Autonomous CLI Studio Service Account"
  description  = "Least-privilege identity for PyForge multi-agent runtime"
}

resource "google_project_iam_member" "vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

resource "google_project_iam_member" "trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

resource "google_project_iam_member" "metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

# 3. Google Cloud Secret Manager for Secure API Key Injection
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = var.gemini_api_key_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.agent_apis]
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

# 4. Cloud Storage Bucket for Persistent Agent State & Evaluation Artifacts
resource "google_storage_bucket" "pyforge_state" {
  name                        = "${var.project_id}-${var.service_name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }
}

resource "google_storage_bucket_iam_member" "state_bucket_object_admin" {
  bucket = google_storage_bucket.pyforge_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pyforge_agent.email}"
}

# 5. Artifact Registry for PyForge Container Images
resource "google_artifact_registry_repository" "pyforge_repo" {
  location      = var.region
  repository_id = var.service_name
  description   = "Container registry for PyForge multi-agent studio"
  format        = "DOCKER"

  depends_on = [google_project_service.agent_apis]
}

# 6. Cloud Run v2 Service with Secret Manager Injection
resource "google_cloud_run_v2_service" "pyforge_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.pyforge_agent.email

    containers {
      image = var.container_image

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "PYFORGE_ARCHITECT_MODEL"
        value = var.architect_model
      }

      env {
        name  = "PYFORGE_CODER_MODEL"
        value = var.coder_model
      }

      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
    }
  }

  depends_on = [
    google_project_service.agent_apis,
    google_secret_manager_secret_iam_member.secret_accessor,
  ]
}

