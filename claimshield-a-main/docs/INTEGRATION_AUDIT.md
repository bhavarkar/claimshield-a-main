# Final integration audit — 2026-10-16

**READY WITH ISSUES for the exercised local integration. Production async deployment remains blocked by the operational issues below.** No new product features or cloud redesign were introduced. All configured prices and quotations used for verification were explicitly synthetic test fixtures, not authoritative data.

## Actual data path

1. `claimshield/api/main.py`: multipart `POST /api/audit` saves bill/policy PDFs and queues a job; `POST /api/claim-review` saves the two document groups and claim type. Both return the existing `{ "case_id": "..." }` response.
2. `_process_job` invokes `claimshield/agents/runner.py:run_audit`; guided review first uses `claimshield/agents/claim_review.py:document_agent` to select/assemble documents. The guided claim type now reaches the audit state.
3. `claimshield/agents/steps.py:intake_step` calls the configured parser (`LocalPdfParser`/Document AI), `services/parsing.py:normalize_bill`, `normalize_policy`, and optional Gemini fallback. Existing `Bill`, `LineItem`, `PolicyDoc`, `PolicyTerms` objects are serialized into state.
4. `reconciliation_step` calls `services/healthcare_audit.py:audit_from_state`, which constructs `HealthcareAuditInput`; `run_healthcare_audit` loads the reviewed healthcare registry and applicable source-checked regulatory item rules. `domain/healthcare_audit.py:audit_healthcare` performs the deterministic checks.
5. The report is additive state under `healthcare_audit`. Existing case persistence writes `cases/{case_id}/case.json`. `GET /api/cases/{case_id}` includes the report, including after in-memory state is cleared; event and PDF endpoints retain their previous contracts.
6. `web/app.js:loadResults` now calls `renderHealthcareAudit(c.healthcare_audit)`. The small panel in `web/index.html` displays summary, counts, warnings and findings. Older cases without a report hide the panel.

JSON `POST /api/audit` takes a separate synchronous branch: validate `HealthcareAuditInput` → run the same service adapter → return the report. It does **not** create a stored case or return a `case_id`. The response retains the original flat report fields and adds `status: "complete"` and `healthcare_audit` containing the same report.

## Person 1 → Person 3 field contract

| Source | Audit interpretation / limitation |
|---|---|
| Pipeline `case_id` | Becomes `claim_id`; direct JSON requires `claim_id`. |
| `bill.line_items` / `line_no` | Preserved; string form of `line_no` is the report's `bill_item_id`. Unique IDs required. |
| `description` | Original description retained and conservatively normalized for matching. |
| `normalized_description` | Optional; extraction does not currently generate it. Never substitutes for original source evidence. |
| `category`, `qty`, `unit_price`, `amount` | Existing names are compatible; no incompatible item/quantity/total renaming. |
| `unit` | Explicit table units retained. Missing Gemini units now use the existing category convention, including ROOM/ICU per day. An explicit Unit column no longer becomes the currency rate. This is a normalization convention, not independent source verification. |
| Drug/device strength, dosage form, pack, manufacturer, price basis | Models accept them, but current local/Gemini extraction does not reliably supply them. NPPA automatic comparisons therefore remain unavailable when required qualifiers/basis are missing. No invented qualifiers. |
| Admission/discharge dates | Existing fields preserved. Admission date supplies event context in the state adapter; each service date controls applicable price dates. Missing dates remain unknown. |
| Admission days, actual room rate, facility/room category | Optional models accept them; extractors do not explicitly populate all of them. A unique consistent normal-room rate may be derived from bill rows; conflicts/missing information require review. |
| Policy terms | Existing rupee-per-day cap remains compatible. Explicit structured percentage caps work; percentage-only wording is not reliably extracted by the current policy extractor. Category-only limits require review. |
| Policy evidence | Numbered source clauses, actual page and SHA-derived document/version identity survive normalization. Exact selected citation required; missing/contradictory evidence cannot support a calculation. |
| Applicability context | Direct JSON uses caller context. Guided `cashless`/other claim type now survives queue→runner→adapter. Legacy PDF keeps hospitalization default. |
| Analytics projection | SQL/BigQuery line projection does not contain every optional clinical qualifier; healthcare checks consume the preserved state `Bill`, not that projection. |

These remaining extraction gaps limit supported automatic checks; they do not require renaming working Person 1 output. Mixed/unknown evidence is not whole-claim approval or rejection.

## API and frontend verification

- `application/json` and `application/json; charset=utf-8`: synchronous HTTP 200; report exists both flat and under `healthcare_audit`.
- Malformed JSON, arrays/null and missing required fields: serializable HTTP 422 errors. Existing size and line-count limits remain.
- Multipart PDF: HTTP 200 with only `case_id`; polling, SSE, saved result and report PDF work. JSON caller labels are not stored-case access credentials.
- Frontend displays total billed, potential discrepancy, separate benchmark variance, clear/flagged/review counts, type/status/severity, billed/reference/difference, confidence, explanation, source/status/page/effective date/version/quote and calculation evidence.
- Unknown values display as undetermined; review findings cannot display a numeric possible difference. Source text is escaped. No benchmark or review amount is labelled confirmed savings.
- Nine Node tests cover frontend result rendering and existing browser authentication. This is DOM-harness coverage, not a visual browser review; no browser surface was available. Live Gemini/OCR/Google adapters and live IAP were not exercised.

## Concrete issues found and fixed

1. JSON consumers lacked the common `healthcare_audit` envelope: added it without removing flat keys.
2. Stored healthcare reports had no frontend consumer: added a small report panel and safe renderer.
3. Guided claim type was dropped before healthcare applicability: carried it through the existing runner.
4. Valid JSON with invalid registry shapes could crash; corrupt extracted-page structures could bypass safe handling: validate shape and fail closed.
5. Empty reference catalogs lacked explicit top-level unavailable warnings: added them.
6. Gemini fallback defaulted room units to per-item; explicit Unit column could be parsed as price: corrected normalization compatibility.
7. Binary floating-point rounding and the threshold for a one-paise variance produced incorrect results: use Decimal calculations and flag positive rounded differences.
8. Administrators could activate synthetic sources in production: block marked synthetic and `.invalid` source approvals outside demo; also exclude them and local-demo approvals at use time after a mode change.
9. Deployment env-file generation omitted the existing reference administrator setting: pass `CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS` through the existing script.

## Calculation, matching and security results

CGHS/PM-JAY variance remains separate from claim discrepancy and legacy recovery. Per-item maximum supported adjustments prevent overlapping exclusion/proportion rules or two benchmarks from double counting. Totals stay non-negative; signed differences remain finding-level evidence. Decimal two-place rounding covers fractional days and one-paise differences. Proportions require an active, applicable policy-specific rule and exact evidence; exclusions take precedence. NPPA checks respect quantity, unit, strength/form and explicit compatible price basis.

Configured CBC / Complete Blood Count / Hemogram aliases match strongly. CBC with ESR / CBC + Platelet Count / CBC Profile require review and produce no unsupported monetary estimate. Fuzzy matches do not become regulatory violations.

Imports remain pending/inactive. Source/version/effective date and review metadata are retained. Conflicting applicable versions require review. Corrupt catalogs/source text fail closed. Synthetic fixtures are excluded from the Docker build context and cannot be active outside demo when marked or using the reserved `.invalid` domain. This guard does not establish the authenticity of arbitrary unmarked third-party data; administrator source review remains required.

Signed local test identities exercise actual auth middleware: unauthenticated import/review is 401, a normal user is 403, allowlisted administrator import/rejection is 200, and synthetic production activation is 409. Existing case-owner isolation tests remain in the full suite. No live identity provider was contacted.

## Scenarios exercised

| Scenario | API/service result |
|---|---|
| A: CBC 90 against synthetic CGHS/PM-JAY 100 and approved synthetic item source | 3 clear, 0 flagged, 0 review; discrepancy and variance 0. |
| B: room 6000 × 2, consultation 1000, CBC 150; cap 3000; verified synthetic proportional rule, LAB excluded; benchmarks 100/120 | Total 13150; discrepancy 6500 (room 6000 + consultation 500); separate benchmark variance 50. Source and calculation evidence retained. |
| C: compound CBC descriptions | Review findings; no unsupported discrepancy or benchmark amount. |
| D: no external sources, CBC-only input | Explicit unavailable warnings, null reference values and zero discrepancy/variance. A separately evidenced supplied-policy room cap can still flag excess without external tariffs. |
| E: existing sample PDFs | Async identifier response, queue completion, stored/reloaded healthcare report, SSE end event and downloadable PDF; legacy recovery remains 24846. |

A–E run through FastAPI's real API/service path. An additional independent uvicorn process exercises loopback HTTP JSON A–D, reference import/review, and multipart E with actual sockets and disposable local storage. The process is terminated after the test; the user's existing preview server is untouched. In the independent A test the absent item-rule source correctly remains review while the two available benchmarks clear.

## Deployment findings and remaining blockers

- **Pending jobs are not durable on Cloud Run:** startup always opens `settings.work_dir / 'jobs.sqlite3'`, even when case objects use GCS. Docker `/app/var` is writable but ephemeral. Replacement can lose queued jobs while persisted cases remain running. One minimum/maximum instance does not fix this.
- **Background CPU is not assured:** the deployment script does not request always-allocated CPU. A disconnected client can leave the worker without request-time CPU; active polling/SSE can hide this limitation.
- **Registry updates are not distributed transactions:** metadata uses whole-object storage writes with process-local locking. Overlapping revisions/instances can overwrite concurrent administrator updates. A single local process is covered; distributed writers are not.
- **Authoritative data remains absent:** real, applicable, source-reviewed regulatory/pricing/policy rules must be supplied before production comparisons can be activated. Synthetic fixtures are not a substitute.
- Docker copies backend/web/data; registry objects are generated in configured storage, not expected as baked-in source files. No new package dependencies, new runtime Windows paths or extra SQLite schema are required by this audit implementation. The admin env transport test passes. Linux image startup, actual Cloud Run lifecycle and live Google service configuration remain unverified. No deployment was attempted.

Resolving durable queue/CPU/distributed writes is deployment work and was not silently implemented during this audit. The current implementation is not verified for high-volume production use.

## Exact requests and expected response

For a self-contained source-unavailable demonstration in isolated demo storage, use:

- Exact request: `tests/fixtures/healthcare/normalized_input.json`.
- Exact expected HTTP 200 body: `tests/fixtures/healthcare/source_unavailable_output.json` (the published pair is compared through the API in tests).
- This fixture totals 10350 and has a policy-backed room discrepancy of 4000, separate benchmark variance 0, counts clear=0/flagged=1/needs_review=10. External source absence does not disable the provided-policy cap.

```powershell
curl.exe -X POST http://127.0.0.1:8080/api/audit -H "Content-Type: application/json" -H "X-ClaimShield-Request: 1" --data-binary "@tests/fixtures/healthcare/normalized_input.json"
```

Do not expect that exact response if the server already has active applicable reference records. Use isolated storage for deterministic replay. Production requires the existing authenticated same-origin session.

The additional exact B request is `tests/fixtures/healthcare/integration_demo_request.json`; its verified setup is executable in `tests/test_healthcare_integration.py:test_real_http_server_json_and_pdf_paths`. It requires the test's explicitly synthetic demo-only reference setup. Without that setup, proportional/benchmark findings correctly remain unavailable.

The JSON response structure is:

```text
{
  status: "complete",
  claim_id, total_billed, total_billed_basis,
  potential_discrepancy, benchmark_variance,
  finding_counts: {clear, flagged, needs_review}, warning_count,
  findings: [{finding_id, type, title, bill_lines, explanation, evidence,
    claim_id, bill_item_id, severity, status, billed_amount, reference_amount,
    potential_difference, rule_id, rule_name, confidence_score, match_status,
    comparison, ...existing Finding fields}],
  summary_basis, warnings,
  healthcare_audit: { ...the same report fields, without the envelope/status }
}
```

For multipart, submission is exactly `{ "case_id": "<server-generated id>" }`; subsequent case JSON contains `healthcare_audit` and existing case fields. If normalized state itself fails validation, the additive report is explicitly `needs_review` with null monetary totals and a warning; the frontend handles this partial report.

## Reproducible validation

```powershell
.venv/Scripts/python.exe -m pytest -q
node --test tests/test_browser_auth.cjs tests/test_browser_healthcare.cjs
.venv/Scripts/python.exe -m compileall -q claimshield scripts/import_healthcare_references.py
node --check web/app.js
.venv/Scripts/python.exe scripts/import_healthcare_references.py tests/fixtures/healthcare/pending_reference_import.json --validate-only
git diff --check
```

Results: **226 Python tests passed in 100.51 seconds; all 9 Node frontend/authentication tests passed.** Compilation, JavaScript syntax, validation-only import (1 rate, 1 room rule; nothing activated), and diff whitespace checks passed. Git emitted existing LF/CRLF conversion notices. No lint or static type-check configuration exists in the repository. No cloud resource or authoritative reference dataset was changed.
