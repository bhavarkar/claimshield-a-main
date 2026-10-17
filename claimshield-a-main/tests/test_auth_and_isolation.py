"""Real signature verification with local keys; no IAP infrastructure is contacted."""

from dataclasses import replace
import json
import time
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import jwt
import pytest

from claimshield.api import auth, main
from claimshield.config import load_settings
from claimshield.services import get_services
from claimshield.services.cases import CaseRepository
from claimshield.services.storage import GcsStorage
from claimshield.trace import TRACES
from tests.test_api_and_guards import _wait

AUDIENCE = "/projects/123456/locations/us-central1/services/claimshield"
ORIGIN = "https://claims.example.test"
ALICE, BOB, CAROL = (f"accounts.google.com:{s}" for s in ("alice", "bob", "carol"))
MEMBERS = {ALICE: "tenant-a", BOB: "tenant-a", CAROL: "tenant-b"}
CASE = "case-" + "a" * 32
PATHS = ("", "/events", "/files/reconciliation_report.pdf", "/files/dispute_letter_insurer.pdf",
         "/files/dispute_letter_hospital.pdf", "/letters/insurer.txt", "/letters/hospital.txt")


@pytest.fixture
def identity(monkeypatch):
    for name, value in {"AUTH_MODE": "iap", "IAP_AUDIENCE": AUDIENCE, "PUBLIC_ORIGIN": ORIGIN,
                        "AUTH_MEMBERSHIPS": json.dumps(MEMBERS)}.items():
        monkeypatch.setenv("CLAIMSHIELD_" + name, value)
    key = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    public.update(kid="local-key", alg="ES256", use="sig")
    keys = jwt.PyJWKClient(auth.IAP_KEYS)
    monkeypatch.setattr(keys, "fetch_data", lambda: {"keys": [public]})
    monkeypatch.setattr(auth, "iap_keys", lambda: keys)

    def token(subject=ALICE, **overrides):
        now = int(time.time())
        claims = {"sub": subject, "iss": auth.IAP_ISSUER, "aud": AUDIENCE,
                  "iat": now - 60, "exp": now + 600, **overrides}
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "local-key"})
    return token


def headers(token):
    return {"X-Goog-IAP-JWT-Assertion": token, "X-ClaimShield-Request": "1", "Origin": ORIGIN}


def seed_case(legacy=False):
    storage = get_services().storage
    if not legacy:
        CaseRepository(storage).create(CASE, auth.Principal(ALICE, MEMBERS[ALICE], auth.IAP_ISSUER, 0))
    record = {"case_id": CASE, "status": "complete", "bill": {"patient_name": "Synthetic Alice"},
              "trace": [{"agent": "intake_agent", "message": "synthetic trace"}],
              "letters": [{"recipient": r, "to": "synthetic", "subject": "synthetic", "body": "private"}
                          for r in ("insurer", "hospital")],
              "artifacts": {v: k for k, v in main.ARTIFACT_FILES.items()}}
    storage.put(f"cases/{CASE}/case.json", json.dumps(record).encode())
    for name in main.ARTIFACT_FILES:
        storage.put(f"cases/{CASE}/{name}", b"%PDF-synthetic")
    main.CASES[CASE] = record


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "reloaded"])
@pytest.mark.parametrize("subject", [ALICE, BOB, CAROL], ids=["owner", "same-tenant-other-user", "other-tenant"])
def test_every_case_endpoint_enforces_owner_and_tenant(identity, cached, subject):
    seed_case()
    if not cached:
        main.CASES.clear()
        TRACES._traces.clear()
    with TestClient(main.app, base_url=ORIGIN) as client:
        for suffix in PATHS:
            response = client.get(f"/api/cases/{CASE}{suffix}", headers=headers(identity(subject)))
            assert response.status_code == (200 if subject == ALICE else 404), suffix
            assert response.headers["cache-control"] == "no-store"
            if subject != ALICE:
                assert "Synthetic Alice" not in response.text and "private" not in response.text
        if subject != ALICE:
            assert client.get("/api/cases/case-not-found", headers=headers(identity(subject))).json() == response.json()


def test_missing_identity_cannot_read_any_case_or_create_audit(identity):
    seed_case()
    with TestClient(main.app, base_url=ORIGIN) as client:
        for suffix in PATHS:
            assert client.get(f"/api/cases/{CASE}{suffix}").status_code == 401
        assert client.post("/api/audit", data={"sample_bill": "BILL-001"}).status_code == 401
        # Unsigned identity headers are not accepted as authentication.
        assert client.get("/api/samples", headers={"X-Goog-Authenticated-User-ID": ALICE}).status_code == 401


def test_dashboard_claim_history_is_owner_scoped_even_if_index_contains_foreign_case(identity):
    seed_case()
    storage = get_services().storage
    alice = auth.Principal(ALICE, MEMBERS[ALICE], auth.IAP_ISSUER, 0)
    bob = auth.Principal(BOB, MEMBERS[BOB], auth.IAP_ISSUER, 0)
    storage.put(main._history_index_key(alice), json.dumps({"case_ids": [CASE]}).encode())
    storage.put(main._history_index_key(bob), json.dumps({"case_ids": [CASE]}).encode())
    with TestClient(main.app, base_url=ORIGIN) as client:
        owner_dashboard = client.get("/api/dashboard", headers=headers(identity(ALICE))).json()
        other_dashboard = client.get("/api/dashboard", headers=headers(identity(BOB))).json()
    assert owner_dashboard["summary"]["claim_count"] == 1
    assert other_dashboard["summary"]["claim_count"] == 0
    assert all(row["claim_id"] != CASE for row in other_dashboard["claims"])


@pytest.mark.parametrize("bad", ["expired", "issuer", "audience", "future", "signature", "algorithm", "unsigned", "missing-exp", "kid"])
def test_invalid_assertions_rejected(identity, bad):
    token = identity()
    if bad == "expired": token = identity(exp=int(time.time()) - 1)
    if bad == "issuer": token = identity(iss="https://attacker.example")
    if bad == "audience": token = identity(aud="another-service")
    if bad == "future": token = identity(iat=int(time.time()) + 60)
    payload = jwt.decode(token, options={"verify_signature": False})
    if bad == "signature":
        token = jwt.encode(payload, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256", headers={"kid": "local-key"})
    if bad == "algorithm": token = jwt.encode(payload, "x" * 32, algorithm="HS256", headers={"kid": "local-key"})
    if bad == "unsigned": token = jwt.encode(payload, "", algorithm="none")
    if bad == "missing-exp":
        token = identity(exp=None)
    if bad == "kid":
        token = jwt.encode(payload, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256", headers={"kid": "unknown"})
    with TestClient(main.app, base_url=ORIGIN) as client:
        assert client.get("/api/samples", headers=headers(token)).status_code == 401


def test_membership_is_server_managed(identity):
    with TestClient(main.app, base_url=ORIGIN) as client:
        assert client.get("/api/samples", headers=headers(identity("accounts.google.com:unregistered", tenant_id="tenant-a"))).status_code == 403


@pytest.mark.parametrize("mutation", ["missing-header", "foreign-origin", "missing-origin"])
def test_cookie_authenticated_mutations_require_csrf_protection(identity, mutation):
    request_headers = headers(identity())
    if mutation == "missing-header": request_headers.pop("X-ClaimShield-Request")
    if mutation == "foreign-origin": request_headers["Origin"] = "https://attacker.example"
    if mutation == "missing-origin": request_headers.pop("Origin")
    with TestClient(main.app, base_url=ORIGIN) as client:
        assert client.post("/api/audit", data={"sample_bill": "BILL-001"}, headers=request_headers).status_code == 403
    assert not main.CASES


def test_authenticated_upload_persists_ownership_before_processing(identity, monkeypatch):
    storage = get_services().storage
    original = main.run_audit

    async def checked_run(case_id, *args, **kwargs):
        owner = json.loads(storage.get(f"cases/{case_id}/owner.json"))
        assert owner["owner_id"] == ALICE and owner["tenant_id"] == "tenant-a"
        assert kwargs['owner_id'] == ALICE and kwargs['tenant_id'] == 'tenant-a'
        assert kwargs['analysis_case_id'].startswith(case_id + '-attempt-')
        assert set(kwargs) == {'owner_id', 'tenant_id', 'analysis_case_id'}
        return await original(case_id, *args, **kwargs)

    monkeypatch.setattr(main, "run_audit", checked_run)
    samples = get_services().settings.data_dir / "samples"
    files = {"bill": ("bill.pdf", (samples / "bills/bill_001_sunrise_appendectomy.pdf").read_bytes()),
             "policy": ("policy.pdf", (samples / "policies/suraksha_health_gold.pdf").read_bytes())}
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity(tenant_id="tenant-b"))) as client:
        result = client.post("/api/audit", files=files, data={"owner_id": CAROL, "tenant_id": "tenant-b"})
        assert result.status_code == 200
        case_id = result.json()["case_id"]
        assert len(case_id.removeprefix("case-")) == 32
        body = _wait(client, case_id)
        assert body["status"] == "complete" and body["reconciliation"]["recovery_likely"] == 24846
        assert "owner_id" not in body and "tenant_id" not in body
        for _ in range(100):
            if storage.exists(f"cases/{case_id}/case.json"): break
            time.sleep(0.01)
        main.CASES.clear()
        TRACES._traces.clear()
        assert client.get(f"/api/cases/{case_id}").json()["reconciliation"] == body["reconciliation"]
        assert "event: end" in client.get(f"/api/cases/{case_id}/events").text
        for url in body["files"].values():
            assert client.get(url).content.startswith(b"%PDF")
            assert client.get(url, headers=headers(identity(CAROL))).status_code == 404


def test_legacy_cases_are_denied_even_when_cached(identity):
    seed_case(legacy=True)
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity())) as client:
        for suffix in PATHS:
            assert client.get(f"/api/cases/{CASE}{suffix}").status_code == 404
    assert not get_services().storage.exists(f"cases/{CASE}/owner.json")


def test_changed_tenant_membership_cannot_read_old_tenant_case(identity, monkeypatch):
    seed_case()
    monkeypatch.setenv("CLAIMSHIELD_AUTH_MEMBERSHIPS", json.dumps({ALICE: "tenant-b"}))
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity())) as client:
        assert client.get(f"/api/cases/{CASE}").status_code == 404


def test_key_service_outage_denies_access_without_demo_fallback(identity, monkeypatch):
    monkeypatch.setattr(auth.iap_keys(), "fetch_data", Mock(side_effect=jwt.PyJWKClientConnectionError("unavailable")))
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity())) as client:
        response = client.get("/api/samples")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"]


def test_malformed_owner_record_is_denied(identity):
    seed_case()
    get_services().storage.put(f"cases/{CASE}/owner.json", b"[]")
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity())) as client:
        assert client.get(f"/api/cases/{CASE}").status_code == 404


def test_ownership_records_cannot_be_overwritten(identity):
    seed_case()
    repository = CaseRepository(get_services().storage)
    with pytest.raises(FileExistsError):
        repository.create(CASE, auth.Principal(CAROL, "tenant-b", auth.IAP_ISSUER, 0))
    repository.authorize(CASE, auth.Principal(ALICE, "tenant-a", auth.IAP_ISSUER, 0))


def test_owner_write_failure_never_starts_processing(identity, monkeypatch):
    run = Mock()
    monkeypatch.setattr(main, "run_audit", run)
    monkeypatch.setattr(get_services().storage, "create", Mock(side_effect=OSError("simulated storage failure")))
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity()), raise_server_exceptions=False) as client:
        assert client.post("/api/audit", data={"sample_bill": "BILL-001"}).status_code == 500
    run.assert_not_called()
    assert not main.CASES


def test_gcs_ownership_create_uses_generation_precondition():
    storage = object.__new__(GcsStorage)
    storage._bucket = Mock()
    storage.create("cases/test/owner.json", b"{}", "application/json")
    storage._bucket.blob.return_value.upload_from_string.assert_called_once_with(
        b"{}", content_type="application/json", if_generation_match=0)


def test_live_stream_stops_when_assertion_expires(identity):
    seed_case()
    main.CASES[CASE]["status"] = "running"
    TRACES.create(CASE)
    token = identity(exp=int(time.time()) + 2)
    with TestClient(main.app, base_url=ORIGIN, headers=headers(token)) as client:
        response = client.get(f"/api/cases/{CASE}/events")
        assert "event: auth_expired" in response.text
        assert client.get(f"/api/cases/{CASE}").status_code == 401


@pytest.mark.parametrize("changes", [
    {"auth_mode": "off"}, {"auth_mode": "iap"},
    {"auth_mode": "iap", "iap_audience": AUDIENCE, "public_origin": "http://claims.example.test"},
    {"auth_mode": "iap", "iap_audience": AUDIENCE, "public_origin": ORIGIN, "auth_memberships": "{}"},
])
def test_missing_or_invalid_auth_config_fails_closed(changes):
    with pytest.raises(ValueError):
        auth.configure_auth(replace(load_settings(), **changes))


def test_cloud_run_cannot_start_demo_mode(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "claimshield")
    with pytest.raises(ValueError, match="forbidden"):
        with TestClient(main.app): pass


@pytest.mark.parametrize("address,hostname", [("203.0.113.1", "localhost"), ("127.0.0.1", "attacker.example")])
def test_demo_rejects_remote_access_and_rebinding(address, hostname):
    with TestClient(main.app, base_url=f"http://{hostname}", client=(address, 50000)) as client:
        assert client.get("/api/samples").status_code == 403
