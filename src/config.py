"""Central configuration and Google Cloud Secret Manager integration for PyForge."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")
warnings.filterwarnings("ignore", message=r".*LibreSSL.*")

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SecretManagerResolver:
    """Secure secret loader supporting Google Cloud Secret Manager and environment injection."""

    @staticmethod
    def get_secret(
        env_var: str = "GOOGLE_API_KEY",
        fallback_env_var: str = "GEMINI_API_KEY",
        secret_id_env: str = "PYFORGE_GCP_SECRET_ID",
    ) -> str:
        """Resolve an API secret from environment injection or Google Cloud Secret Manager.

        Never uses hardcoded credentials. Checks injected environment variables first,
        then queries Google Cloud Secret Manager if `GOOGLE_CLOUD_PROJECT` is configured.
        """
        direct_value = os.getenv(env_var) or os.getenv(fallback_env_var)
        if direct_value:
            return direct_value

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
        secret_id = os.getenv(secret_id_env, "pyforge-gemini-api-key")
        if not project_id:
            return ""

        try:
            # Optional dynamic import for Google Cloud Secret Manager client
            from google.cloud import secretmanager  # type: ignore[import-untyped]

            client = secretmanager.SecretManagerServiceClient()
            resource_name = (
                f"projects/{project_id}/secrets/{secret_id}/versions/latest"
            )
            response = client.access_secret_version(request={"name": resource_name})
            return response.payload.data.decode("utf-8").strip()
        except Exception:
            return ""


@dataclass(frozen=True)
class PyForgeConfig:
    """Immutable configuration settings for PyForge."""

    # Project paths
    project_root: Path = PROJECT_ROOT
    bin_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("PYFORGE_BIN_DIR", str(PROJECT_ROOT / "bin"))
        )
    )
    db_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("PYFORGE_DB_PATH", str(PROJECT_ROOT / "configs" / "pyforge.db"))
        )
    )

    # Model settings (Pro for architectural planning, Flash for fast coding/compaction)
    google_api_key: str = field(
        default_factory=SecretManagerResolver.get_secret
    )
    architect_model: str = field(
        default_factory=lambda: os.getenv("PYFORGE_ARCHITECT_MODEL", "gemini-2.5-pro")
    )
    coder_model: str = field(
        default_factory=lambda: os.getenv("PYFORGE_CODER_MODEL", "gemini-2.5-flash")
    )
    architect_temperature: float = 0.2
    coder_temperature: float = 0.2

    # Execution & safety bounds
    sandbox_timeout_seconds: float = 5.0
    max_healing_iterations: int = 3
    memory_max_messages: int = 8

    # Observability settings
    service_name: str = "pyforge"
    enable_console_telemetry: bool = field(
        default_factory=lambda: os.getenv("PYFORGE_CONSOLE_TELEMETRY", "false").lower()
        in {"1", "true", "yes"}
    )


DEFAULT_CONFIG = PyForgeConfig()
