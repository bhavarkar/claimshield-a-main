"""Ordinary tests always use synthetic data, local services and disposable storage."""

import ipaddress
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory

import pytest


# Apply before test-module imports: a developer's .env must never enable cloud clients
# or point collection-time service initialization at their normal case directory.
_collection_dir = TemporaryDirectory(prefix="claimshield-tests-")
LOCAL_ENV = {
    "CLAIMSHIELD_AUTH_MODE": "demo",
    "CLAIMSHIELD_IAP_AUDIENCE": "",
    "CLAIMSHIELD_PUBLIC_ORIGIN": "",
    "CLAIMSHIELD_AUTH_MEMBERSHIPS": "{}",
    "K_SERVICE": "",
    "K_REVISION": "",
    **{f"CLAIMSHIELD_USE_{name}": "false" for name in
       ("DOCAI", "BIGQUERY", "VERTEX_SEARCH", "GEMINI", "GCS")},
    "CLAIMSHIELD_USE_ADK": "true",
    "CLAIMSHIELD_DEMO_PACE": "0",
    "CLAIMSHIELD_OUTLIER_RATIO": "1.3",
    "CLAIMSHIELD_DATA_DIR": str(Path(__file__).resolve().parents[1] / "data"),
    "CLAIMSHIELD_WORK_DIR": _collection_dir.name,
    "GOOGLE_CLOUD_PROJECT": "",
    "GOOGLE_CLOUD_LOCATION": "us-central1",
    "DOCAI_LOCATION": "us",
    "DOCAI_PROCESSOR_ID": "",
    "BQ_DATASET": "claimshield",
    "VERTEX_SEARCH_LOCATION": "global",
    "VERTEX_SEARCH_DATASTORE_ID": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "GCS_BUCKET": "",
}
_original_env = {key: os.environ.get(key) for key in LOCAL_ENV}
os.environ.update(LOCAL_ENV)


def _clear_services():
    from claimshield.services import get_services

    if get_services.cache_info().currsize:
        svc = get_services()
        if hasattr(svc.store, "_db"):
            svc.store._db.close()
    get_services.cache_clear()
    from claimshield.trace import TRACES

    TRACES._traces.clear()
    if api := sys.modules.get("claimshield.api.main"):
        api.CASES.clear()


@pytest.fixture(autouse=True)
def isolated_local_services(monkeypatch, tmp_path):
    _clear_services()
    for key, value in LOCAL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLAIMSHIELD_WORK_DIR", str(tmp_path / "cases"))

    # Loopback is permitted for local tools and asyncio's Windows wakeup sockets.
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guard(method):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = address[0]
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host == "localhost"
                if not local:
                    raise AssertionError(f"External network access forbidden in local tests: {host}")
            return method(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(original_connect_ex))
    yield
    _clear_services()


def pytest_unconfigure(config):
    _collection_dir.cleanup()
    for key, value in _original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
