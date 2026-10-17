"""HTTP API and static web UI (one Cloud Run service).

    POST /api/audit                      start an audit (upload bill + policy PDFs, or pick samples)
    GET  /api/cases/{id}/events          live agent trace (Server-Sent Events)
    GET  /api/cases/{id}                 findings, reconciliation, letters
    GET  /api/cases/{id}/files/{name}    reconciliation report / dispute letter PDFs
    GET  /api/samples                    synthetic demo bills and policies
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import csv
import io
import logging
import os
import re
import time
import threading
import uuid
import warnings
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="google")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from ..agents.runner import run_audit  # noqa: E402
from ..agents.claim_review import document_agent, guidance_agent, clause_review_agent
from ..services.jobs import JobQueue, QueueFull
from ..domain.healthcare_audit import HealthcareAuditInput, HealthcareFinding
from ..services.healthcare_audit import run_healthcare_audit
from ..services.healthcare_references import HealthcareReferenceRegistry, ReferenceImport, ReferenceReview
from ..config import PROJECT_ROOT, load_settings  # noqa: E402
from .auth import authenticate, configure_auth, Principal  # noqa: E402
from .job_worker import run_job_worker  # noqa: E402
from ..services.cases import CaseRepository  # noqa: E402
from ..services import get_services  # noqa: E402
from ..services.parsing import looks_like_pdf  # noqa: E402
from ..trace import TRACES  # noqa: E402
from ..services.preclaim import CostItem, PreClaimInput, WhatIfInput, assess_preclaim, simulate_what_if  # noqa: E402
from ..services.rejection import analyze_rejection  # noqa: E402
from ..services.preauthorization import (AuthorizationStatus, PreAuthorizationIntake,
                                         prepare_request)  # noqa: E402
from ..services.policy_history import PolicyHistoryRequest, check_policy_history  # noqa: E402
from ..services.document_check import check_documents  # noqa: E402
from ..services.parsing import ParseError, normalize_policy  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402
from ..services.timeline import ClaimEvent, chronological, draft_follow_up  # noqa: E402
from ..services.policy_explainer import ExplainRequest, explain_policy  # noqa: E402
from ..services.review import (ReviewDecision, build_review_item, record_outcome,
                               confusion_matrix, outcome_metrics)  # noqa: E402
from ..services.claim_dashboard import (ClaimOutcomeRecord, make_claim_summary,
                                        aggregate_summaries, owner_matches)  # noqa: E402
from ..services.regulatory import (ApplicabilityRequest, DocumentReview, RegulatoryRegistry,
                                   RegulatoryRule, RuleReview)  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("claimshield.api")

MAX_UPLOAD = 15 * 1024 * 1024
ARTIFACT_FILES = {
    "reconciliation_report.pdf": "report",
    "dispute_letter_insurer.pdf": "letter_insurer",
    "dispute_letter_hospital.pdf": "letter_hospital",
}
_INDEX_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate before any cloud services initialize; missing auth never enables demo.
    settings = load_settings()
    app.state.auth_config = configure_auth(settings)
    svc = get_services()
    app.state.jobs = JobQueue(settings.work_dir / 'jobs.sqlite3',
                              capacity=settings.queue_capacity,
                              per_owner=settings.per_owner_pending_limit,
                              storage=svc.storage)
    workers = [asyncio.create_task(_job_worker(app.state.jobs))
               for _ in range(settings.worker_count)]
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


app = FastAPI(title="ClaimShield AI", version="0.1.0", lifespan=lifespan)
CASES: dict[str, dict[str, Any]] = {}


async def _job_worker(queue):
    # Keep this application-level adapter so existing integrations can patch or call it.
    await run_job_worker(queue, _process_job, get_services, TRACES, log)


async def _process_job(case_id, payload):
    svc = get_services()
    # Persisted completion makes delivery after a worker restart idempotent.
    key = f'cases/{case_id}/case.json'
    existing = json.loads(await asyncio.to_thread(svc.storage.get, key))
    if existing.get('status') in {'complete', 'failed'}:
        return
    trace = TRACES.create(case_id)
    checklist = None
    if payload.get('manifest'):
        checklist = await asyncio.to_thread(document_agent, payload['manifest'], svc, trace, payload['claim_type'])
    analysis_case_id = f'{case_id}-attempt-{uuid.uuid4().hex}'
    context_args = {'claim_context': payload['claim_type']} if payload.get('claim_type') else {}
    state = await run_audit(case_id, payload['bill_key'], payload['policy_key'],
                            payload['bill_name'], payload['policy_name'],
                            owner_id=payload['owner_id'], tenant_id=payload['tenant_id'],
                            analysis_case_id=analysis_case_id, **context_args)
    if checklist:
        pages = checklist.pop('_medical_pages', [])
        state['ai_clause_review'] = await asyncio.to_thread(clause_review_agent, state, pages, svc, trace)
        trace.emit('guidance_agent', 'start', 'Matching evidence gaps to your next steps')
        state['claim_review'] = await asyncio.to_thread(guidance_agent, state, checklist, payload['manifest'])
        trace.emit('guidance_agent', 'result', 'Your clause review and next steps are ready')
        state['trace'] = trace.events
    await asyncio.to_thread(svc.storage.put, key, json.dumps(state, default=str).encode(), 'application/json')
    job_key = f'cases/{case_id}/job.json'
    if svc.storage.exists(job_key):
        try:
            job_record = json.loads(svc.storage.get(job_key))
            job_record['status'] = 'complete'
            job_record['lease'] = 0
            svc.storage.put(job_key, json.dumps(job_record).encode(), 'application/json')
        except Exception:
            pass
    TRACES.discard(case_id)
    if hasattr(svc.store, 'release_case'):
        await asyncio.to_thread(svc.store.release_case, analysis_case_id)



async def _enqueue_case(request, files, bill_index, policy_index, claim_type=None):
    svc, principal = get_services(), request.state.principal
    case_id = f'case-{uuid.uuid4().hex}'
    await asyncio.to_thread(CaseRepository(svc.storage).create, case_id, principal)
    manifest = []
    for index, (group, name, content) in enumerate(files):
        key = f'cases/{case_id}/uploads/{index}.pdf'
        await asyncio.to_thread(svc.storage.put, key, content, 'application/pdf')
        manifest.append({'group': group, 'name': name, 'key': key, 'primary': index in (bill_index, policy_index)})
    bill, policy = manifest[bill_index], manifest[policy_index]
    initial = {'case_id': case_id, 'status': 'running', 'bill_name': bill['name'], 'policy_name': policy['name']}
    await asyncio.to_thread(svc.storage.put, f'cases/{case_id}/case.json', json.dumps(initial).encode(), 'application/json')
    payload = dict(bill_key=bill['key'], policy_key=policy['key'], bill_name=bill['name'], policy_name=policy['name'],
                   owner_id=principal.subject, tenant_id=principal.tenant)
    if claim_type:
        payload.update(manifest=manifest, claim_type=claim_type)
    owner = hashlib.sha256(f'{principal.issuer}:{principal.tenant}:{principal.subject}'.encode()).hexdigest()
    try:
        await asyncio.to_thread(request.app.state.jobs.submit, case_id, owner, payload)
    except QueueFull as exc:
        failed = {**initial, 'status': 'failed', 'error': str(exc)}
        await asyncio.to_thread(svc.storage.put, f'cases/{case_id}/case.json', json.dumps(failed).encode(), 'application/json')
        raise HTTPException(429, str(exc), headers={'Retry-After': '10'}) from exc
    await asyncio.to_thread(_register_review_case, case_id, principal)
    await asyncio.to_thread(_register_history_case, case_id, principal)
    return {'case_id': case_id}


@app.post("/api/internal/jobs/{case_id}/process")
async def process_internal_job(case_id: str, request: Request) -> dict[str, Any]:
    """Internal idempotent processing endpoint for Cloud Tasks or async background workers."""
    token = request.headers.get("X-ClaimShield-Internal-Token", "")
    expected = os.getenv("CLAIMSHIELD_INTERNAL_TOKEN", "")
    is_demo = os.getenv("CLAIMSHIELD_AUTH_MODE", "iap").strip().lower() == "demo"
    is_loopback = request.client and request.client.host in ("127.0.0.1", "::1", "localhost", "testclient")
    token_valid = bool(expected and token and hmac.compare_digest(token, expected))
    authorized = token_valid or bool(is_demo and is_loopback)
    if not authorized:
        raise HTTPException(403, "Internal endpoint access forbidden.")

    svc = get_services()
    case_key = f"cases/{case_id}/case.json"
    if not svc.storage.exists(case_key):
        raise HTTPException(404, "Unknown case")

    try:
        case_data = json.loads(svc.storage.get(case_key))
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Job state is temporarily unavailable.") from exc
    if case_data.get("status") in {"complete", "failed"}:
        return {"case_id": case_id, "status": case_data["status"], "already_processed": True}

    job_key = f"cases/{case_id}/job.json"
    if not svc.storage.exists(job_key):
        raise HTTPException(409, "Job is not ready for processing.")
    try:
        job_record = json.loads(svc.storage.get(job_key))
        payload = job_record.get("payload")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("invalid job payload")
    except (OSError, ValueError, AttributeError) as exc:
        raise HTTPException(409, "Job record is invalid or unavailable.") from exc

    await _process_job(case_id, payload)
    final_case = json.loads(svc.storage.get(case_key))
    return {"case_id": case_id, "status": final_case.get("status", "complete")}



def _review_index_key(principal: Principal) -> str:
    issuer_key = hashlib.sha256(principal.issuer.encode()).hexdigest()[:16]
    tenant_key = re.sub(r"[^a-zA-Z0-9_-]", "_", principal.tenant)
    return f"review-queues/{issuer_key}/{tenant_key}.json"


def _require_reviewer(principal: Principal) -> None:
    configured = {subject.strip() for subject in os.getenv("CLAIMSHIELD_REVIEWER_SUBJECTS", "").split(",") if subject.strip()}
    if principal.issuer == "urn:claimshield:demo":
        configured.add("local-demo")
    if principal.subject not in configured:
        raise HTTPException(403, "Insurer reviewer access is not configured for this account.")


def _register_review_case(case_id: str, principal: Principal) -> None:
    storage = get_services().storage
    key = _review_index_key(principal)
    with _INDEX_LOCK:
        try:
            data = json.loads(storage.get(key)) if storage.exists(key) else {"case_ids": []}
        except (ValueError, OSError):
            data = {"case_ids": []}
        if case_id not in data["case_ids"]:
            data["case_ids"].append(case_id)
        storage.put(key, json.dumps(data).encode(), "application/json")


def _history_index_key(principal: Principal) -> str:
    issuer_key = hashlib.sha256(principal.issuer.encode()).hexdigest()[:16]
    tenant_key = hashlib.sha256(principal.tenant.encode()).hexdigest()[:16]
    subject_key = hashlib.sha256(principal.subject.encode()).hexdigest()[:24]
    return f"claim-history/{issuer_key}/{tenant_key}/{subject_key}.json"


def _register_history_case(case_id: str, principal: Principal) -> None:
    storage = get_services().storage
    key = _history_index_key(principal)
    with _INDEX_LOCK:
        try:
            data = json.loads(storage.get(key)) if storage.exists(key) else {"case_ids": []}
        except (ValueError, OSError):
            data = {"case_ids": []}
        if case_id not in data["case_ids"]:
            data["case_ids"].append(case_id)
        storage.put(key, json.dumps(data).encode(), "application/json")


def _review_item(case_id: str, principal: Principal) -> dict[str, Any]:
    svc = get_services()
    if not re.fullmatch(r"case-[a-f0-9]{32}", case_id):
        raise HTTPException(404, "Unknown review case")
    # Membership maps users to a tenant; tenant members share review access to that tenant's cases.
    owner_key = f"cases/{case_id}/owner.json"
    if not svc.storage.exists(owner_key):
        raise HTTPException(404, "Unknown review case")
    try:
        owner = json.loads(svc.storage.get(owner_key))
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Unknown review case") from exc
    if owner.get("tenant_id") != principal.tenant or owner.get("issuer") != principal.issuer:
        raise HTTPException(404, "Unknown review case")
    case_data = CASES.get(case_id)
    if case_data is None:
        case_key = f"cases/{case_id}/case.json"
        if not svc.storage.exists(case_key):
            raise HTTPException(404, "Unknown review case")
        case_data = json.loads(svc.storage.get(case_key))
    result = build_review_item(case_data)
    try:
        catalog = _catalog()
        result["sample_bill_id"] = next((sample_id for sample_id, sample in catalog.get("bills", {}).items()
                                         if Path(sample["file"]).name == Path(case_data.get("bill_name", "")).name), None)
    except (OSError, KeyError, ValueError):
        result["sample_bill_id"] = None
    review_key = f"cases/{case_id}/review.json"
    if svc.storage.exists(review_key):
        saved = json.loads(svc.storage.get(review_key))
        saved_by_id = {f["finding_id"]: f for f in saved.get("findings", [])}
        for finding in result["findings"]:
            previous = saved_by_id.get(finding["finding_id"], {})
            finding["human_outcome"] = previous.get("human_outcome")
            finding["reviewer_explanation"] = previous.get("reviewer_explanation")
            finding["reviewer_id"] = previous.get("reviewer_id")
            finding["reviewed_at"] = previous.get("reviewed_at")
            finding["review_history"] = previous.get("review_history", [])
            finding["disagreement"] = previous.get("disagreement", False)
    return result


@app.get("/api/reviewer/queue")
def reviewer_queue(request: Request) -> dict[str, Any]:
    principal = request.state.principal
    _require_reviewer(principal)
    storage = get_services().storage
    key = _review_index_key(principal)
    try:
        case_ids = json.loads(storage.get(key)).get("case_ids", []) if storage.exists(key) else []
    except (ValueError, OSError):
        case_ids = []
    items = []
    for case_id in case_ids:
        try:
            item = _review_item(case_id, principal)
        except HTTPException:
            continue
        if item.get("status") != "running":
            items.append(item)
    return {"items": items, "review_access": "Only explicitly configured reviewer accounts can review cases in their tenant.",
            "fraud_notice": "Unreviewed audit flags are not fraud determinations."}


@app.post("/api/reviewer/cases/{case_id}/findings/{finding_id}")
def submit_review_decision(case_id: str, finding_id: str, body: ReviewDecision,
                           request: Request) -> dict[str, Any]:
    principal = request.state.principal
    _require_reviewer(principal)
    item = _review_item(case_id, principal)
    try:
        updated = record_outcome(item, finding_id, body, principal.subject, date.today().isoformat())
    except KeyError as exc:
        raise HTTPException(404, "Unknown finding") from exc
    get_services().storage.put(f"cases/{case_id}/review.json", json.dumps(updated).encode(), "application/json")
    return updated


@app.get("/api/reviewer/evaluation")
def reviewer_evaluation(request: Request) -> dict[str, Any]:
    principal = request.state.principal
    _require_reviewer(principal)
    storage = get_services().storage
    try:
        case_ids = json.loads(storage.get(_review_index_key(principal))).get("case_ids", [])
    except (OSError, ValueError):
        case_ids = []
    outcomes = []
    for case_id in case_ids:
        try:
            item = _review_item(case_id, principal)
            outcomes.extend(f for f in item["findings"] if f.get("human_outcome"))
        except HTTPException:
            continue
    label_path = get_services().settings.data_dir / "evaluation" / "synthetic_review_labels.json"
    labels = json.loads(label_path.read_text(encoding="utf-8"))
    return {"synthetic_dataset_version": labels["version"],
            "synthetic_confusion_matrix": confusion_matrix(labels["labels"]),
            "reviewer_outcomes": outcome_metrics(outcomes),
            "notice": "Synthetic test labels are illustrative. Reviewer disagreement is retained as human outcome history; no unreviewed flag is treated as fraud."}

class FollowUpRequest(BaseModel):
    grievance: bool = False


class AuthorizationStatusUpdate(BaseModel):
    status: AuthorizationStatus


def _preauth_resource(resource_id: str, principal: Principal) -> dict[str, Any]:
    if not re.fullmatch(r"preauth-[a-f0-9]{32}", resource_id):
        raise HTTPException(404, "Unknown pre-authorization request")
    storage = get_services().storage
    prefix = f"preauthorizations/{resource_id}"
    try:
        owner = json.loads(storage.get(f"{prefix}/owner.json"))
        data = json.loads(storage.get(f"{prefix}/request.json"))
    except Exception as exc:
        raise HTTPException(404, "Unknown pre-authorization request") from exc
    if (owner.get("owner_id") != principal.subject or owner.get("tenant_id") != principal.tenant
            or owner.get("issuer") != principal.issuer):
        raise HTTPException(404, "Unknown pre-authorization request")
    return data


def _policy_history_resource(resource_id: str, principal: Principal) -> dict[str, Any]:
    if not re.fullmatch(r"policy-check-[a-f0-9]{32}", resource_id):
        raise HTTPException(404, "Unknown policy history check")
    storage = get_services().storage
    prefix = f"policy-history/{resource_id}"
    try:
        owner = json.loads(storage.get(f"{prefix}/owner.json"))
        result = json.loads(storage.get(f"{prefix}/result.json"))
    except Exception as exc:
        raise HTTPException(404, "Unknown policy history check") from exc
    if (owner.get("owner_id") != principal.subject or owner.get("tenant_id") != principal.tenant
            or owner.get("issuer") != principal.issuer):
        raise HTTPException(404, "Unknown policy history check")
    return result


def _timeline(case_id: str) -> dict[str, Any]:
    storage = get_services().storage
    key = f"cases/{case_id}/timeline.json"
    if not storage.exists(key):
        return {"events": []}
    return json.loads(storage.get(key))


def _save_timeline(case_id: str, timeline: dict[str, Any]) -> None:
    get_services().storage.put(f"cases/{case_id}/timeline.json", json.dumps(timeline).encode(), "application/json")


def _timeline_response(case_id: str) -> dict[str, Any]:
    timeline = _timeline(case_id)
    events = chronological(timeline["events"])
    latest = events[-1] if events else None
    actions = []
    if latest:
        if latest["claim_status"] == "query_pending":
            actions.append({"action": "Respond to insurer query", "required_documents": latest.get("required_documents", [])})
        elif latest["claim_status"] == "rejected":
            actions.append({"action": "Review rejection and consider a grievance", "required_documents": []})
        elif latest["claim_status"] == "grievance_open":
            actions.append({"action": "Await or request grievance response", "required_documents": []})
    return {"events": events, "claim_status": latest["claim_status"] if latest else None,
            "pending_actions": actions, "escalation_rules": _read_escalation_rules()}


def _read_escalation_rules() -> dict[str, Any]:
    path = get_services().settings.data_dir / "escalation_rules.json"
    if not path.exists():
        return {"available": False, "version": None, "rules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("version"), str) or not isinstance(data.get("rules"), list):
            raise ValueError("invalid rules file")
        return {"available": True, "version": data["version"], "rules": data["rules"]}
    except (ValueError, OSError):
        return {"available": False, "version": None, "rules": []}


@app.middleware("http")
async def identity_boundary(request: Request, call_next):
    try:
        request.state.principal = await asyncio.to_thread(authenticate, request, app.state.auth_config)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _require_regulatory_admin(principal: Principal) -> None:
    configured = {subject.strip() for subject in os.getenv("CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS", "").split(",") if subject.strip()}
    if principal.subject not in configured:
        raise HTTPException(403, "Regulatory administrator access is not configured for this account.")


def _regulatory_registry() -> RegulatoryRegistry:
    return RegulatoryRegistry(get_services().storage)


@app.post('/api/admin/healthcare-references/import')
def import_healthcare_references(batch: ReferenceImport, request: Request):
    _require_regulatory_admin(request.state.principal)
    return HealthcareReferenceRegistry(get_services().storage).import_records(batch)


@app.post('/api/admin/healthcare-references/{kind}/{record_id}/review')
def review_healthcare_reference(kind: str, record_id: str, review: ReferenceReview, request: Request):
    _require_regulatory_admin(request.state.principal)
    return HealthcareReferenceRegistry(get_services().storage).review(
        kind, record_id, review, request.state.principal.subject).model_dump(mode='json')


@app.get('/api/admin/healthcare-references/snapshot')
def healthcare_reference_snapshot(request: Request) -> dict[str, Any]:
    """List all imported healthcare reference records and room rules with their review status.

    This endpoint is for demo and administrative inspection only. It returns metadata (IDs, names,
    source types, verification status, enforcement flags) but does NOT return reference amounts or
    prices — those remain in the registry and are only used by the audit engine at audit time.
    """
    _require_regulatory_admin(request.state.principal)
    rates, room_rules = HealthcareReferenceRegistry(get_services().storage).snapshot()
    return {
        'rates': [
            {'record_id': r.record_id, 'item_name': r.item_name, 'category': r.category,
             'source_type': r.source_type, 'source_name': r.source_name,
             'verification_status': r.verification_status, 'enforcement_enabled': r.enforcement_enabled,
             'effective_date': r.effective_date.isoformat() if r.effective_date else None,
             'geography': r.geography, 'unit': r.unit}
            for r in rates
        ],
        'room_rules': [
            {'rule_id': r.rule_id, 'rule_name': r.rule_name, 'source_name': r.source_name,
             'verification_status': r.verification_status, 'enforcement_enabled': r.enforcement_enabled,
             'effective_date': r.effective_date.isoformat() if r.effective_date else None,
             'affected_categories': r.affected_categories, 'excluded_categories': r.excluded_categories,
             'policy_document_id': r.policy_document_id}
            for r in room_rules
        ],
        'counts': {'rates': len(rates), 'room_rules': len(room_rules),
                   'rates_active': sum(1 for r in rates if r.enforcement_enabled),
                   'room_rules_active': sum(1 for r in room_rules if r.enforcement_enabled)},
        'notice': (
            'This snapshot shows registry metadata only. Reference amounts are used by the audit engine '
            'and are not displayed here. All records start as pending_human_review and require explicit '
            'administrator approval before any rule can influence an audit finding.'
        ),
    }


@app.get("/api/regulatory/documents")
def regulatory_documents(request: Request) -> dict[str, Any]:
    registry = _regulatory_registry()
    return {"documents": [d.model_dump(mode="json") for d in registry.list_documents()],
            "notice": "Registry metadata is informational. Imported documents and approved status are shown separately."}


@app.get("/api/regulatory/rules/search")
def regulatory_rule_search(request: Request, q: str = "") -> dict[str, Any]:
    rules = _regulatory_registry().search_rules(q)
    return {"rules": [r.model_dump(mode="json") for r in rules],
            "notice": "Unreviewed rules are candidates only and are not legally authoritative."}


@app.post("/api/regulatory/rules/{rule_id}/applicability")
def regulatory_rule_applicability(rule_id: str, context: ApplicabilityRequest, request: Request) -> dict[str, Any]:
    return _regulatory_registry().applicability(rule_id, context)


@app.get("/api/regulatory/documents/{document_id}/evidence")
def regulatory_evidence(document_id: str, request: Request, page: int | None = None) -> dict[str, Any]:
    return _regulatory_registry().evidence(document_id, page)


@app.get("/api/regulatory/documents/{document_id}/pdf")
def regulatory_original_pdf(document_id: str, request: Request) -> Response:
    content = _regulatory_registry().original_pdf(document_id)
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{document_id}.pdf"'})


@app.post("/api/admin/regulatory/documents/ingest")
async def regulatory_document_ingest(request: Request, document_id: str = Form(...),
                                    source_url: str = Form(...), pdf: UploadFile = File(...)) -> dict[str, Any]:
    _require_regulatory_admin(request.state.principal)
    pdf_bytes = await _read_upload(pdf, "regulatory document")
    document = await asyncio.to_thread(_regulatory_registry().ingest, document_id, source_url, pdf_bytes)
    return document.model_dump(mode="json")


@app.post("/api/admin/regulatory/documents/preview")
async def regulatory_document_preview(request: Request, document_id: str = Form(...),
                                      source_url: str = Form(...), pdf: UploadFile = File(...)) -> dict[str, Any]:
    _require_regulatory_admin(request.state.principal)
    pdf_bytes = await _read_upload(pdf, "regulatory document")
    return await asyncio.to_thread(_regulatory_registry().preview, document_id, source_url, pdf_bytes)


@app.post("/api/admin/regulatory/documents/{document_id}/review")
def regulatory_document_review(document_id: str, review: DocumentReview, request: Request) -> dict[str, Any]:
    _require_regulatory_admin(request.state.principal)
    principal = request.state.principal
    return _regulatory_registry().review_document(document_id, review, reviewer_id=principal.subject).model_dump(mode="json")


@app.post("/api/admin/regulatory/rules")
def regulatory_rule_candidate(rule: RegulatoryRule, request: Request) -> dict[str, Any]:
    _require_regulatory_admin(request.state.principal)
    return _regulatory_registry().create_candidate(rule).model_dump(mode="json")


@app.post("/api/admin/regulatory/rules/{rule_id}/review")
def regulatory_rule_review(rule_id: str, review: RuleReview, request: Request) -> dict[str, Any]:
    _require_regulatory_admin(request.state.principal)
    principal = request.state.principal
    return _regulatory_registry().review_rule(rule_id, review, reviewer_id=principal.subject).model_dump(mode="json")


def _catalog() -> dict[str, Any]:
    return json.loads((get_services().settings.data_dir / "samples" / "catalog.json").read_text())


@app.get("/api/health")
def health() -> dict[str, Any]:
    svc = get_services()
    return {"status": "ok", "stack": svc.describe(), "warnings": svc.warnings}


@app.get("/api/samples")
def samples() -> dict[str, Any]:
    cat = _catalog()
    for bid, b in cat["bills"].items():
        b["id"] = bid
        b["url"] = f"/api/samples/{b['file']}"
        b["policy_plan"] = cat["policies"][b["policy"]]["plan"]
    for pid, p in cat["policies"].items():
        p["id"] = pid
        p["url"] = f"/api/samples/{p['file']}"
    return cat


@app.get("/api/samples/{kind}/{name}")
def sample_file(kind: str, name: str) -> FileResponse:
    if kind not in ("bills", "policies") or not re.fullmatch(r"[a-z0-9_]+\.pdf", name):
        raise HTTPException(404)
    path = get_services().settings.data_dir / "samples" / kind / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


async def _read_upload(upload: UploadFile, label: str) -> bytes:
    data = await upload.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"The {label} is larger than 15 MB.")
    if not looks_like_pdf(data):
        raise HTTPException(415, f"The {label} must be a PDF.")
    return data


@app.post("/api/policy-explainer")
async def policy_explainer(policy: UploadFile = File(...), question: str = Form(...),
                           language: str = Form("en")) -> dict[str, Any]:
    try:
        request = ExplainRequest(question=question, language=language)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    svc = get_services()
    data = await _read_upload(policy, "policy document")
    try:
        try:
            raw = await asyncio.to_thread(svc.parser.parse, data)
        except Exception:
            if svc.parser is svc.local_parser:
                raise
            raw = await asyncio.to_thread(svc.local_parser.parse, data)
        parsed_policy = normalize_policy(raw, Path(policy.filename or "policy.pdf").name)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return await asyncio.to_thread(explain_policy, parsed_policy, request.question,
                                       request.language, svc.retriever, svc.llm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/audit")
async def start_audit(
    request: Request,
    bill: Optional[UploadFile] = File(None),
    policy: Optional[UploadFile] = File(None),
    sample_bill: Optional[str] = Form(None),
    sample_policy: Optional[str] = Form(None),
) -> dict[str, Any]:
    if request.headers.get('content-type', '').split(';')[0].strip().lower() == 'application/json':
        content = await request.body()
        if len(content) > 2 * 1024 * 1024:
            raise HTTPException(413, 'Structured audit input exceeds 2 MiB')
        try:
            normalized = HealthcareAuditInput.model_validate_json(content)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False))) from exc
        report = await asyncio.to_thread(run_healthcare_audit, normalized, get_services())
        return {**report, 'status': 'complete', 'healthcare_audit': report}
    svc = get_services()
    cat = _catalog()
    samples_dir = svc.settings.data_dir / "samples"

    if bill is not None and bill.filename:
        bill_bytes, bill_name = await _read_upload(bill, "bill"), Path(bill.filename).name
    elif sample_bill in cat["bills"]:
        entry = cat["bills"][sample_bill]
        bill_bytes, bill_name = (samples_dir / entry["file"]).read_bytes(), Path(entry["file"]).name
        sample_policy = sample_policy or entry["policy"]
    else:
        raise HTTPException(400, "Upload a bill PDF or choose a sample bill.")

    if policy is not None and policy.filename:
        policy_bytes, policy_name = await _read_upload(policy, "policy"), Path(policy.filename).name
    elif sample_policy in cat["policies"]:
        entry = cat["policies"][sample_policy]
        policy_bytes, policy_name = (samples_dir / entry["file"]).read_bytes(), Path(entry["file"]).name
    else:
        raise HTTPException(400, "Upload a policy PDF or choose a sample policy.")

    return await _enqueue_case(request, [('medical', bill_name, bill_bytes), ('insurance', policy_name, policy_bytes)], 0, 1)


@app.post('/api/claim-review')
async def start_claim_review(request: Request, medical_documents: list[UploadFile] = File(...),
                             insurance_documents: list[UploadFile] = File(...),
                             main_bill: int = Form(0), main_policy: int = Form(0),
                             claim_type: str = Form('reimbursement')):
    if claim_type not in {'reimbursement', 'cashless', 'day_care'}:
        raise HTTPException(422, 'Choose reimbursement, cashless or day care.')
    if not 1 <= len(medical_documents) <= 10 or not 1 <= len(insurance_documents) <= 10:
        raise HTTPException(422, 'Upload 1 to 10 PDFs in each group.')
    if not 0 <= main_bill < len(medical_documents) or not 0 <= main_policy < len(insurance_documents):
        raise HTTPException(422, 'Select the main bill and main policy from your uploads.')
    files, total = [], 0
    for group, uploads in [('medical', medical_documents), ('insurance', insurance_documents)]:
        for upload in uploads:
            data = await _read_upload(upload, group + ' document')
            total += len(data)
            if total > 50 * 1024 * 1024:
                raise HTTPException(413, 'Upload at most 50 MB per claim review.')
            files.append((group, Path(upload.filename or 'document.pdf').name, data))
    return await _enqueue_case(request, files, main_bill, len(medical_documents) + main_policy, claim_type)


@app.post("/api/preclaim")
async def preclaim_assessment(
    policy: UploadFile = File(...),
    treatment: str = Form(...),
    estimated_total: float = Form(...),
    room_category: str = Form(...),
    hospitalization_date: str = Form(...),
    room_rate_per_day: float | None = Form(None),
    nights: int = Form(1),
    network_hospital: bool | None = Form(None),
    patient_age: int | None = Form(None),
    deductible: float | None = Form(None),
    cost_items: str | None = Form(None),
) -> dict[str, Any]:
    """Estimate policy-based coverage before treatment; does not determine claim approval."""
    svc = get_services()
    policy_bytes = await _read_upload(policy, "policy")
    try:
        raw = await asyncio.to_thread(svc.parser.parse, policy_bytes)
    except Exception as exc:  # cloud parsing failures follow the existing local fallback pattern
        try:
            raw = await asyncio.to_thread(svc.local_parser.parse, policy_bytes)
        except Exception as local_exc:
            raise HTTPException(422, f"Could not read policy PDF: {type(local_exc).__name__}") from exc
    try:
        policy_doc = normalize_policy(raw, Path(policy.filename or "policy.pdf").name)
        items = [CostItem.model_validate(v) for v in (json.loads(cost_items) if cost_items else [])]
        request_data = PreClaimInput(
            treatment=treatment, estimated_total=estimated_total, room_category=room_category,
            room_rate_per_day=room_rate_per_day, hospitalization_date=hospitalization_date, nights=nights,
            network_hospital=network_hospital, patient_age=patient_age, deductible=deductible, cost_items=items,
        )
        return await asyncio.to_thread(assess_preclaim, request_data, policy_doc, svc.retriever)
    except (ParseError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
        raise HTTPException(422, detail) from exc


@app.post("/api/what-if")
async def what_if_simulation(
    policy: UploadFile = File(...),
    base: str = Form(...),
    scenarios: str = Form(...),
) -> dict[str, Any]:
    """Compare hypothetical estimates using copies of the extracted policy terms."""
    svc = get_services()
    policy_bytes = await _read_upload(policy, "policy")
    try:
        request_data = WhatIfInput(base=json.loads(base), scenarios=json.loads(scenarios))
    except (json.JSONDecodeError, ValidationError) as exc:
        detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
        raise HTTPException(422, detail) from exc
    try:
        try:
            raw = await asyncio.to_thread(svc.parser.parse, policy_bytes)
        except Exception:
            if svc.parser is svc.local_parser:
                raise
            raw = await asyncio.to_thread(svc.local_parser.parse, policy_bytes)
        policy_doc = normalize_policy(raw, Path(policy.filename or "policy.pdf").name)
        return await asyncio.to_thread(simulate_what_if, request_data, policy_doc, svc.retriever)
    except (ParseError, ValueError) as exc:
        raise HTTPException(422, f"Could not simulate the supplied policy and scenarios: {exc}") from exc
    except Exception as exc:
        raise HTTPException(422, f"Could not simulate the supplied policy and scenarios: {type(exc).__name__}") from exc


@app.post("/api/rejection-analysis")
async def rejection_analysis(
    rejection_letter: UploadFile = File(...),
    policy: UploadFile = File(...),
) -> dict[str, Any]:
    """Compare a rejection notice with supplied policy wording and prepare a review draft."""
    svc = get_services()
    rejection_bytes = await _read_upload(rejection_letter, "rejection letter")
    policy_bytes = await _read_upload(policy, "policy")

    async def parse_with_fallback(data: bytes):
        try:
            return await asyncio.to_thread(svc.parser.parse, data)
        except Exception:
            if svc.parser is svc.local_parser:
                raise
            return await asyncio.to_thread(svc.local_parser.parse, data)

    try:
        notice, raw_policy = await asyncio.gather(parse_with_fallback(rejection_bytes), parse_with_fallback(policy_bytes))
        notice_text = "\n".join(page for page in notice.pages if page.strip())
        if not notice_text.strip():
            raise HTTPException(422, "No readable text was found in the rejection letter.")
        policy_doc = normalize_policy(raw_policy, Path(policy.filename or "policy.pdf").name)
        result = await asyncio.to_thread(analyze_rejection, notice_text, policy_doc, retriever=svc.retriever, llm=svc.llm)
        result["parser"] = {"rejection_letter": getattr(notice, "parser", ""), "policy": getattr(raw_policy, "parser", "")}
        return result
    except HTTPException:
        raise
    except Exception as exc:  # report malformed PDFs/policies as input errors, not server failures
        raise HTTPException(422, f"Could not analyze the supplied documents: {type(exc).__name__}") from exc


@app.post("/api/document-check")
async def document_check(
    files: list[UploadFile] = File(...),
    claim_type: str = Form("default"),
    insurer: str | None = Form(None),
) -> dict[str, Any]:
    """Classify uploaded claim documents from extracted content and compare a sourced checklist."""
    if not files or len(files) > 20:
        raise HTTPException(400, "Upload between 1 and 20 PDF documents.")
    svc = get_services()
    payloads = []
    total_bytes = 0
    for upload in files:
        data = await _read_upload(upload, upload.filename or "document")
        total_bytes += len(data)
        if total_bytes > 60 * 1024 * 1024:
            raise HTTPException(413, "The combined documents are larger than 60 MB.")
        payloads.append((upload.filename or "document.pdf", data))

    async def parse(data: bytes):
        try:
            return await asyncio.to_thread(svc.parser.parse, data)
        except Exception:
            if svc.parser is svc.local_parser:
                return None
            try:
                return await asyncio.to_thread(svc.local_parser.parse, data)
            except Exception:
                return None

    raws = await asyncio.gather(*(parse(data) for _, data in payloads))
    documents = [{"filename": name, "content": data, "raw": raw}
                 for (name, data), raw in zip(payloads, raws)]
    try:
        return await asyncio.to_thread(check_documents, documents, claim_type=claim_type,
                                       data_dir=svc.settings.data_dir, insurer=insurer)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/policy-history/check")
async def create_policy_history_check(request: Request, metadata: str = Form(...),
                                      documents: list[UploadFile] = File(...)) -> dict[str, Any]:
    try:
        check_request = PolicyHistoryRequest.model_validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    if len(documents) != len(check_request.documents):
        raise HTTPException(422, "Provide exactly one document type for each uploaded PDF.")
    if not documents or len(documents) > 10:
        raise HTTPException(400, "Upload between 1 and 10 policy PDFs.")
    svc = get_services()
    parsed = []
    total = 0
    for upload in documents:
        contents = await _read_upload(upload, upload.filename or "policy document")
        total += len(contents)
        if total > 60 * 1024 * 1024:
            raise HTTPException(413, "Policy documents exceed 60 MB combined.")
        try:
            try:
                raw = await asyncio.to_thread(svc.parser.parse, contents)
            except Exception:
                if svc.parser is svc.local_parser:
                    raise
                raw = await asyncio.to_thread(svc.local_parser.parse, contents)
            policy_doc = normalize_policy(raw, Path(upload.filename or "policy.pdf").name)
            parsed.append({"policy": policy_doc, "text": "\n".join(raw.pages), "bytes": contents,
                           "filename": Path(upload.filename or "policy.pdf").name})
        except Exception as exc:
            raise HTTPException(422, f"Could not read policy document {Path(upload.filename or 'policy.pdf').name}: {type(exc).__name__}") from exc
    try:
        result = await asyncio.to_thread(check_policy_history, check_request, parsed, svc.retriever)
    except Exception as exc:
        raise HTTPException(422, f"Could not check the supplied policy documents: {type(exc).__name__}") from exc
    resource_id = f"policy-check-{uuid.uuid4().hex}"
    prefix = f"policy-history/{resource_id}"
    principal = request.state.principal
    owner = {"version": 1, "owner_id": principal.subject, "tenant_id": principal.tenant, "issuer": principal.issuer}
    svc.storage.create(f"{prefix}/owner.json", json.dumps(owner).encode(), "application/json")
    result["check_id"] = resource_id
    result["retrieval_backend"] = getattr(svc.retriever, "name", "configured policy retrieval")
    svc.storage.put(f"{prefix}/result.json", json.dumps(result).encode(), "application/json")
    for doc in parsed:
        digest = hashlib.sha256(doc["bytes"]).hexdigest()
        svc.storage.put(f"{prefix}/documents/{digest}.pdf", doc["bytes"], "application/pdf")
    return result


@app.get("/api/policy-history/{resource_id}")
def get_policy_history_check(resource_id: str, request: Request) -> dict[str, Any]:
    return _policy_history_resource(resource_id, request.state.principal)


@app.post("/api/preauthorizations")
async def create_preauthorization(
    request: Request,
    intake: str = Form(...),
    policy: UploadFile | None = File(None),
    documents: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    """Prepare a user-authored request and checklist; never submits or grants authorization."""
    try:
        data = PreAuthorizationIntake.model_validate_json(intake)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    if len(documents) > 10:
        raise HTTPException(400, "Upload no more than 10 supporting PDFs.")
    policy_doc = None
    svc = get_services()
    if policy is not None and policy.filename:
        policy_bytes = await _read_upload(policy, "policy")
        try:
            try:
                raw_policy = await asyncio.to_thread(svc.parser.parse, policy_bytes)
            except Exception:
                if svc.parser is svc.local_parser:
                    raise
                raw_policy = await asyncio.to_thread(svc.local_parser.parse, policy_bytes)
            policy_doc = normalize_policy(raw_policy, Path(policy.filename).name)
        except Exception as exc:
            raise HTTPException(422, f"Could not read the supplied policy PDF: {type(exc).__name__}") from exc
    attachment_payloads = []
    total = 0
    for upload in documents:
        contents = await _read_upload(upload, upload.filename or "supporting document")
        total += len(contents)
        if total > 60 * 1024 * 1024:
            raise HTTPException(413, "Supporting documents exceed 60 MB combined.")
        attachment_payloads.append((Path(upload.filename or "document.pdf").name, contents))
    prepared = prepare_request(data, policy_doc, [name for name, _ in attachment_payloads])
    resource_id = f"preauth-{uuid.uuid4().hex}"
    prefix = f"preauthorizations/{resource_id}"
    principal = request.state.principal
    owner = {"version": 1, "owner_id": principal.subject, "tenant_id": principal.tenant, "issuer": principal.issuer}
    svc.storage.create(f"{prefix}/owner.json", json.dumps(owner).encode(), "application/json")
    for name, contents in attachment_payloads:
        safe_key = f"{uuid.uuid4().hex}-{name}"
        svc.storage.put(f"{prefix}/documents/{safe_key}", contents, "application/pdf")
    prepared.update({"preauthorization_id": resource_id, "status_history": []})
    svc.storage.put(f"{prefix}/request.json", json.dumps(prepared).encode(), "application/json")
    return prepared


@app.get("/api/preauthorizations/{resource_id}")
def get_preauthorization(resource_id: str, request: Request) -> dict[str, Any]:
    return _preauth_resource(resource_id, request.state.principal)


@app.patch("/api/preauthorizations/{resource_id}/status")
def update_preauthorization_status(resource_id: str, body: AuthorizationStatusUpdate,
                                  request: Request) -> dict[str, Any]:
    data = _preauth_resource(resource_id, request.state.principal)
    if body.status == AuthorizationStatus.NOT_RECORDED:
        raise HTTPException(422, "Choose a manually reported status.")
    entry = {"status": body.status.value, "source": "manual user entry", "recorded_at": date.today().isoformat()}
    data["authorization_status"] = body.status.value
    data["status_source"] = "manual user entry; not verified by ClaimShield"
    data.setdefault("status_history", []).append(entry)
    data["summary"] = data["summary"].replace(
        "Authorization status: manually entered status only; no insurer response was retrieved.",
        f"Authorization status: {body.status.value} (manually entered; not verified by ClaimShield).")
    get_services().storage.put(f"preauthorizations/{resource_id}/request.json",
                               json.dumps(data).encode(), "application/json")
    return data


def _case(case_id: str, principal: Principal) -> dict[str, Any]:
    svc = get_services()
    CaseRepository(svc.storage).authorize(case_id, principal)
    if case_id in CASES:
        return CASES[case_id]
    key = f"cases/{case_id}/case.json"
    if svc.storage.exists(key):
        return json.loads(svc.storage.get(key))
    raise HTTPException(404, "Unknown case")


def _dashboard_claim(case_id: str, principal: Principal):
    storage = get_services().storage
    owner_key = f"cases/{case_id}/owner.json"
    if not re.fullmatch(r"case-[a-f0-9]{32}", case_id) or not storage.exists(owner_key):
        raise HTTPException(404, "Unknown case")
    try:
        owner = json.loads(storage.get(owner_key))
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "Unknown case") from exc
    if not owner_matches(owner, principal.subject, principal.tenant, principal.issuer):
        raise HTTPException(404, "Unknown case")
    case_data = _case(case_id, principal)
    if case_data.get("status") == "running" or not case_data.get("bill"):
        return None
    svc = get_services()
    timeline = _timeline(case_id)
    key = f"cases/{case_id}/claim-outcome.json"
    outcome = json.loads(svc.storage.get(key)) if svc.storage.exists(key) else {}
    sample_id = None
    try:
        filename = Path(case_data.get("bill_name", "")).name
        sample_id = next((sample_key for sample_key, sample in _catalog().get("bills", {}).items()
                          if Path(sample["file"]).name == filename), None)
    except (OSError, ValueError, KeyError):
        pass
    return make_claim_summary(case_id, case_data, timeline, outcome, sample_id)


@app.get("/api/dashboard")
def claim_dashboard(request: Request) -> dict[str, Any]:
    """Return claim summaries indexed only for this authenticated owner."""
    principal = request.state.principal
    storage = get_services().storage
    key = _history_index_key(principal)
    try:
        case_ids = json.loads(storage.get(key)).get("case_ids", []) if storage.exists(key) else []
    except (ValueError, OSError):
        case_ids = []
    # The tenant review index also surfaces audits created before the per-owner dashboard index existed.
    try:
        tenant_ids = json.loads(storage.get(_review_index_key(principal))).get("case_ids", [])
        case_ids = list(dict.fromkeys([*case_ids, *tenant_ids]))
    except (ValueError, OSError):
        pass
    summaries = []
    for case_id in case_ids:
        try:
            summary = _dashboard_claim(case_id, principal)
        except HTTPException:
            continue
        if summary is not None:
            summaries.append(summary)
    summaries.sort(key=lambda item: (item.claim_date or "", item.claim_id), reverse=True)
    return {"summary": aggregate_summaries(summaries),
            "claims": [item.model_dump(mode="json") for item in summaries],
            "notice": "Policy assessments and recovery estimates are not insurer decisions. Actual settlements and confirmed recovery are manually recorded and not verified by ClaimShield.",
            "simulated_demo_claims": sum(item.simulated_demo_data for item in summaries)}


@app.put("/api/cases/{case_id}/claim-outcome")
def record_claim_outcome(case_id: str, body: ClaimOutcomeRecord, request: Request) -> dict[str, Any]:
    """Record a user-entered settlement or confirmed recovery on an owned case."""
    principal = request.state.principal
    case_data = _case(case_id, principal)
    if case_data.get("status") != "complete":
        raise HTTPException(409, "Claim outcomes can be recorded after the audit is complete.")
    svc = get_services()
    _register_history_case(case_id, principal)
    key = f"cases/{case_id}/claim-outcome.json"
    current = json.loads(svc.storage.get(key)) if svc.storage.exists(key) else {}
    update = body.model_dump(mode="json", exclude_none=True)
    current.update(update)
    current["recorded_by"] = principal.subject
    current["recorded_at"] = date.today().isoformat()
    svc.storage.put(key, json.dumps(current).encode(), "application/json")
    summary = _dashboard_claim(case_id, principal)
    return summary.model_dump(mode="json")


@app.get("/api/dashboard/export.csv")
def export_claim_dashboard(request: Request) -> Response:
    data = claim_dashboard(request)
    output = io.StringIO(newline="")
    fields = ["claim_id", "sample_id", "claim_date", "provider", "status", "dispute_status",
              "claimed_amount", "assessed_payable_amount", "actual_settled_amount", "deductions",
              "estimated_recovery", "estimated_recovery_upper", "confirmed_recovery",
              "outstanding_documents", "pending_actions", "simulated_demo_data"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for claim in data["claims"]:
        row = {key: claim.get(key) for key in fields}
        row["deductions"] = "; ".join(f"{item.get('label', 'Deduction')}: {item.get('amount', 0)}" for item in claim["deductions"])
        row["outstanding_documents"] = "; ".join(claim["outstanding_documents"])
        row["pending_actions"] = "; ".join(claim["pending_actions"])
        row = {key: ("'" + value if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"} else value)
               for key, value in row.items()}
        writer.writerow(row)
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=claim-journey.csv"})


@app.get("/api/cases/{case_id}/events")
async def events(case_id: str, request: Request) -> StreamingResponse:
    principal = request.state.principal
    case = await asyncio.to_thread(_case, case_id, principal)

    async def stream():
        if case.get("status") != "running" and "trace" in case:
            for e in case["trace"]:
                if time.time() >= principal.expires_at:
                    yield "event: auth_expired\ndata: {}\n\n"
                    return
                yield f"data: {json.dumps(e)}\n\n"
            yield "event: end\ndata: {}\n\n"
            return
        cursor, last_beat = 0, time.monotonic()
        while True:
            if time.time() >= principal.expires_at:
                yield "event: auth_expired\ndata: {}\n\n"
                return
            trace = TRACES.get(case_id)
            batch = trace.since(cursor)
            for e in batch:
                yield f"data: {json.dumps(e, default=str)}\n\n"
            cursor += len(batch)
            current = await asyncio.to_thread(_case, case_id, principal)
            if current.get('status') != 'running':
                TRACES.discard(case_id)
                yield "event: end\ndata: {}\n\n"
                return
            if time.monotonic() - last_beat > 10:
                yield ": keep-alive\n\n"
                last_beat = time.monotonic()
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/cases/{case_id}")
def case(case_id: str, request: Request) -> JSONResponse:
    c = _case(case_id, request.state.principal)
    if c.get("status") == "running":
        job = request.app.state.jobs.status(case_id) or {}
        return JSONResponse({"case_id": case_id, "status": "running", "processing_stage": job.get('status', 'queued')})
    keep = ("case_id", "status", "error", "bill", "policy_doc", "policy_terms", "findings", "benchmark_comparisons", "reconciliation", "letters", "stack", "bill_name", "policy_name", "claim_review", "healthcare_audit")
    out = {k: c.get(k) for k in keep if k in c}
    out["files"] = {name: f"/api/cases/{case_id}/files/{name}" for name, key in ARTIFACT_FILES.items() if key in c.get("artifacts", {})}
    if out.get("bill"):
        out["bill"].pop("parser", None)
    return JSONResponse(out)


@app.get("/api/cases/{case_id}/timeline")
def claim_timeline(case_id: str, request: Request) -> dict[str, Any]:
    _case(case_id, request.state.principal)
    return _timeline_response(case_id)


@app.post("/api/cases/{case_id}/timeline/events")
def add_claim_event(case_id: str, event: ClaimEvent, request: Request) -> dict[str, Any]:
    _case(case_id, request.state.principal)
    timeline = _timeline(case_id)
    payload = event.model_dump(mode="json")
    identity_fields = ("event_type", "date", "source", "claim_status", "notes", "required_documents")
    if any(all(e.get(field, "" if field == "notes" else []) == payload.get(field, "" if field == "notes" else [])
               for field in identity_fields) for e in timeline["events"]):
        raise HTTPException(409, "This claim event is already recorded.")
    payload.update({"id": uuid.uuid4().hex, "created_at": time.time()})
    timeline["events"].append(payload)
    _save_timeline(case_id, timeline)
    return _timeline_response(case_id)


@app.post("/api/cases/{case_id}/timeline/events/{event_id}/documents")
async def attach_claim_document(case_id: str, event_id: str, request: Request,
                                file: UploadFile = File(...)) -> dict[str, Any]:
    _case(case_id, request.state.principal)
    timeline = _timeline(case_id)
    event = next((e for e in timeline["events"] if e["id"] == event_id), None)
    if not event:
        raise HTTPException(404, "Unknown claim event")
    data = await _read_upload(file, "supporting document")
    name = Path(file.filename or "document.pdf").name
    object_name = f"{uuid.uuid4().hex}-{name}"
    key = f"cases/{case_id}/timeline-documents/{object_name}"
    get_services().storage.put(key, data, "application/pdf")
    event.setdefault("attachment_names", []).append(name)
    event.setdefault("attachments", []).append(object_name)
    _save_timeline(case_id, timeline)
    return _timeline_response(case_id)


@app.get("/api/cases/{case_id}/escalation-rules")
def escalation_rules(case_id: str, request: Request) -> dict[str, Any]:
    _case(case_id, request.state.principal)
    return _read_escalation_rules()


@app.post("/api/cases/{case_id}/timeline/draft")
def follow_up_draft(case_id: str, body: FollowUpRequest, request: Request) -> dict[str, str]:
    _case(case_id, request.state.principal)
    timeline = _timeline(case_id)
    events = chronological(timeline["events"])
    if body.grievance and (not events or events[-1]["claim_status"] != "rejected"):
        raise HTTPException(422, "A grievance draft is supported only after a recorded rejection.")
    try:
        return draft_follow_up(timeline["events"], body.grievance)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/cases/{case_id}/files/{name}")
def case_file(case_id: str, name: str, request: Request) -> Response:
    c = _case(case_id, request.state.principal)
    if name not in ARTIFACT_FILES or ARTIFACT_FILES[name] not in c.get("artifacts", {}):
        raise HTTPException(404)
    data = get_services().storage.get(f"cases/{case_id}/{name}")
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{case_id}-{name}"'})


@app.get("/api/cases/{case_id}/letters/{recipient}.txt")
def letter_text(case_id: str, recipient: str, request: Request) -> PlainTextResponse:
    c = _case(case_id, request.state.principal)
    for letter in c.get("letters", []):
        if letter["recipient"] == recipient:
            return PlainTextResponse(f"To,\n{letter['to']}\n\nSubject: {letter['subject']}\n\n{letter['body']}\n",
                                     headers={"Content-Disposition": f'attachment; filename="{case_id}-letter-{recipient}.txt"'})
    raise HTTPException(404)


_base_openapi = app.openapi


def _healthcare_openapi():
    """Document both existing multipart jobs and normalized JSON on the same route."""
    schema = _base_openapi()
    components = schema.setdefault('components', {}).setdefault('schemas', {})
    for model in (HealthcareAuditInput, HealthcareFinding):
        definition = model.model_json_schema(ref_template='#/components/schemas/{model}')
        components.update(definition.pop('$defs', {}))
        components[model.__name__] = definition
    content = schema['paths']['/api/audit']['post']['requestBody']['content']
    content['application/json'] = {'schema': {'$ref': '#/components/schemas/HealthcareAuditInput'}}
    operation = schema['paths']['/api/audit']['post']
    operation['description'] = ('Multipart uploads start the existing asynchronous audit and return case_id. '
                                'Normalized JSON runs deterministic healthcare reference checks synchronously and returns claim_id, '
                                'total_billed, potential_discrepancy, benchmark_variance, finding_counts, warning_count and findings. '
                                'It also returns status=complete and the same report under healthcare_audit for case consumers. '
                                'Benchmark variances do not determine insurer liability.')
    return schema


app.openapi = _healthcare_openapi
app.mount("/", StaticFiles(directory=PROJECT_ROOT / "web", html=True), name="web")
