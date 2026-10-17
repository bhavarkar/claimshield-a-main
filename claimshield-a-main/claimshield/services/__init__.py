"""Service container. Each Google Cloud backend is used when its flag is on and it initialises;
if it fails to initialise, the local fallback takes over and the failure is recorded in
`warnings` (surfaced in the agent trace) so a missing credential never takes the demo down."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..config import Settings, load_settings
from .llm import GeminiLLM, TemplateLLM
from .parsing import DocumentAIParser, LocalPdfParser
from .retrieval import LocalRetriever, VertexSearchRetriever
from .storage import GcsStorage, LocalStorage
from .store import BigQueryStore, SqliteStore

log = logging.getLogger("claimshield.services")


@dataclass
class Services:
    settings: Settings
    parser: Any
    local_parser: LocalPdfParser
    store: Any
    retriever: Any
    llm: Any
    storage: Any
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, str]:
        return {
            "parsing": self.parser.name,
            "warehouse": self.store.name,
            "retrieval": self.retriever.name,
            "reasoning": self.llm.name,
            "storage": self.storage.name,
        }


def _try(flag: bool, label: str, make, fallback, warnings: list[str]):
    if not flag:
        return fallback()
    try:
        return make()
    except Exception as exc:  # noqa: BLE001 - any init failure means "use the local fallback"
        # Detailed provider errors can contain project, endpoint, or credential context.
        # Keep diagnostics in server logs and expose only a stable status to API clients.
        msg = f"{label} unavailable ({type(exc).__name__}); using local fallback"
        log.warning("%s initialization failed; using local fallback", label, exc_info=True)
        warnings.append(msg)
        return fallback()


def build_services(settings: Settings | None = None) -> Services:
    s = settings or load_settings()
    w: list[str] = []
    return Services(
        settings=s,
        parser=_try(s.use_docai, "Document AI", lambda: DocumentAIParser(s.project, s.docai_location, s.docai_processor_id), LocalPdfParser, w),
        local_parser=LocalPdfParser(),
        store=_try(s.use_bigquery, "BigQuery", lambda: BigQueryStore(s.project, s.bq_dataset, reference_dir=s.data_dir / "reference"), lambda: SqliteStore(s.data_dir / "reference"), w),
        retriever=_try(s.use_vertex_search, "Vertex AI Search",
                       lambda: VertexSearchRetriever(s.project, s.vertex_search_location, s.vertex_search_datastore), LocalRetriever, w),
        llm=_try(s.use_gemini, "Gemini", lambda: GeminiLLM(s.project, s.location, s.gemini_model), TemplateLLM, w),
        storage=_try(s.use_gcs, "Cloud Storage", lambda: GcsStorage(s.project, s.gcs_bucket), lambda: LocalStorage(s.work_dir), w),
        warnings=w,
    )


@lru_cache(maxsize=1)
def get_services() -> Services:
    return build_services()
