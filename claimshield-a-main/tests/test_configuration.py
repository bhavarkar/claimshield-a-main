"""Isolation and Windows CLI regressions for the local verification workflow."""

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from claimshield import config
from claimshield.services import get_services
from claimshield.services.parsing import LocalPdfParser
from claimshield.services.retrieval import LocalRetriever
from claimshield.services.llm import TemplateLLM
from claimshield.services.storage import LocalStorage
from claimshield.services.store import SqliteStore


def test_local_settings_override_developer_dotenv(monkeypatch, tmp_path):
    # Simulate a cloud-enabled developer configuration without touching the real .env.
    (tmp_path / ".env").write_text(
        "\n".join(f"CLAIMSHIELD_USE_{name}=true" for name in
                  ("DOCAI", "BIGQUERY", "VERTEX_SEARCH", "GEMINI", "GCS"))
        + "\nCLAIMSHIELD_WORK_DIR=do-not-use\nCLAIMSHIELD_DATA_DIR=do-not-use\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    settings = config.load_settings()
    assert not any((settings.use_docai, settings.use_bigquery, settings.use_vertex_search,
                    settings.use_gemini, settings.use_gcs))
    assert settings.work_dir == tmp_path / "cases"
    assert settings.data_dir == Path(__file__).resolve().parents[1] / "data"


def test_services_never_attempt_cloud_initialization(monkeypatch, tmp_path):
    import claimshield.services as services

    def forbidden(*args, **kwargs):
        pytest.fail("A cloud service constructor was called during local tests")

    for name in ("DocumentAIParser", "BigQueryStore", "VertexSearchRetriever", "GeminiLLM", "GcsStorage"):
        monkeypatch.setattr(services, name, forbidden)
    svc = get_services()
    for service, expected in ((svc.parser, LocalPdfParser), (svc.store, SqliteStore),
                              (svc.retriever, LocalRetriever), (svc.llm, TemplateLLM),
                              (svc.storage, LocalStorage)):
        assert isinstance(service, expected)
    assert svc.warnings == []
    svc.storage.put("isolation-check.txt", b"synthetic")
    assert (tmp_path / "cases" / "isolation-check.txt").read_bytes() == b"synthetic"


@pytest.mark.parametrize("operation", ["connect", "connect_ex"])
def test_external_network_is_blocked(operation):
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="External network access forbidden"):
            getattr(sock, operation)(("192.0.2.1", 443))


@pytest.mark.parametrize("use_adk", ["true", "false"], ids=["adk", "plain"])
def test_sample_cli_handles_cp1252_console(use_adk, tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0",
           "CLAIMSHIELD_USE_ADK": use_adk,
           "CLAIMSHIELD_WORK_DIR": str(tmp_path / "cli-output")}
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "scripts/run_samples.py"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, timeout=120,
    )
    output = result.stdout.decode("utf-8")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert output.count(": complete") == 4
    for amount in ("24,846", "6,450", "42,800", "0"):
        assert f"likely recovery \u20b9{amount} (" in output
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8", errors="replace")


def test_sample_cli_reports_failed_audit_as_nonzero(monkeypatch):
    from scripts import run_samples

    async def failed_case(*args, **kwargs):
        return {"status": "failed", "error": "simulated parser failure"}

    monkeypatch.setattr(run_samples, "run_one", failed_case)
    assert asyncio.run(run_samples.main(["BILL-001"])) == 1
