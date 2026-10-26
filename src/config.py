"""Central configuration for PyForge."""

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

    # Model settings
    google_api_key: str = field(
        default_factory=lambda: os.getenv("GOOGLE_API_KEY", os.getenv("GEMINI_API_KEY", ""))
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

