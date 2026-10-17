"""Runtime configuration. Every Google Cloud service sits behind its own flag so the whole
pipeline runs locally with fallbacks until credentials are available."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be set to true or false (received {raw!r})")


def _positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer (received {raw!r})") from exc
    if value < 1 or (maximum is not None and value > maximum):
        limit = f" between 1 and {maximum}" if maximum is not None else " greater than zero"
        raise ValueError(f"{name} must be{limit} (received {value})")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number (received {raw!r})") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero (received {raw!r})")
    return value


@dataclass(frozen=True)
class Settings:
    project: str
    location: str
    use_docai: bool
    docai_location: str
    docai_processor_id: str
    use_bigquery: bool
    bq_dataset: str
    use_vertex_search: bool
    vertex_search_location: str
    vertex_search_datastore: str
    use_gemini: bool
    gemini_model: str
    use_gcs: bool
    gcs_bucket: str
    use_adk: bool
    data_dir: Path
    work_dir: Path
    outlier_ratio: float
    auth_mode: str = "iap"
    iap_audience: str = ""
    public_origin: str = ""
    auth_memberships: str = "{}"
    worker_count: int = 2
    queue_capacity: int = 100
    per_owner_pending_limit: int = 3

    @property
    def mode_summary(self) -> dict[str, str]:
        return {
            "parsing": "Document AI" if self.use_docai else "local pdfplumber",
            "warehouse": "BigQuery" if self.use_bigquery else "local SQLite",
            "retrieval": "Vertex AI Search" if self.use_vertex_search else "local keyword retrieval",
            "reasoning": f"Gemini ({self.gemini_model})" if self.use_gemini else "deterministic templates",
            "storage": "Cloud Storage" if self.use_gcs else "local filesystem",
            "orchestration": "ADK" if self.use_adk else "plain Python runner",
        }


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        use_docai=_flag("CLAIMSHIELD_USE_DOCAI"),
        docai_location=os.getenv("DOCAI_LOCATION", "us"),
        docai_processor_id=os.getenv("DOCAI_PROCESSOR_ID", ""),
        use_bigquery=_flag("CLAIMSHIELD_USE_BIGQUERY"),
        bq_dataset=os.getenv("BQ_DATASET", "claimshield"),
        use_vertex_search=_flag("CLAIMSHIELD_USE_VERTEX_SEARCH"),
        vertex_search_location=os.getenv("VERTEX_SEARCH_LOCATION", "global"),
        vertex_search_datastore=os.getenv("VERTEX_SEARCH_DATASTORE_ID", ""),
        use_gemini=_flag("CLAIMSHIELD_USE_GEMINI"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        use_gcs=_flag("CLAIMSHIELD_USE_GCS"),
        gcs_bucket=os.getenv("GCS_BUCKET", ""),
        use_adk=_flag("CLAIMSHIELD_USE_ADK", default=True),
        data_dir=Path(os.getenv("CLAIMSHIELD_DATA_DIR", PROJECT_ROOT / "data")),
        work_dir=Path(os.getenv("CLAIMSHIELD_WORK_DIR", PROJECT_ROOT / "var")),
        outlier_ratio=_positive_float("CLAIMSHIELD_OUTLIER_RATIO", 1.3),
        auth_mode=os.getenv("CLAIMSHIELD_AUTH_MODE", "iap"),
        iap_audience=os.getenv("CLAIMSHIELD_IAP_AUDIENCE", ""),
        public_origin=os.getenv("CLAIMSHIELD_PUBLIC_ORIGIN", ""),
        auth_memberships=os.getenv("CLAIMSHIELD_AUTH_MEMBERSHIPS", "{}"),
        worker_count=_positive_int("CLAIMSHIELD_WORKERS", 2, maximum=16),
        queue_capacity=_positive_int("CLAIMSHIELD_QUEUE_CAPACITY", 100),
        per_owner_pending_limit=_positive_int("CLAIMSHIELD_USER_PENDING_LIMIT", 3),
    )
