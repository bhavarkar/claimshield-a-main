# ClaimShield AI

**An agentic hospital-bill and insurance-claim auditor that works for the policyholder.**
Upload a hospital bill and an insurance policy. ClaimShield's agents audit the bill line by line against the policy wording and return:

- findings, each with the exact bill line, the exact policy clause, a plain-language explanation, a confidence label (**High** / **Worth reviewing**) and the rupee impact
- a reconciliation of billed vs covered vs owed, and how much you can likely recover
- ready-to-send dispute letters (one to the insurer, one to the hospital) and a PDF reconciliation report

Built for the Google Cloud AI hackathon, **BFSI theme** (intelligent risk, fraud and financial experiences). Flags are always framed as *worth disputing*, never as fraud.

---

## What it detects

| Check | How | Confidence |
|---|---|---|
| Duplicate charges | SQL window over (date, code, qty, price) | High; Worth reviewing for pharmacy/therapy, where repeats can be legitimate |
| Quantity x rate errors | SQL `qty * unit_price <> amount` | High |
| Room days beyond the length of stay | admission/discharge dates vs room quantity | High |
| Gross total â‰  sum of lines | totals reconciliation | High |
| Settlement differs from the policy (co-pay, sub-limit, room-rent cap, wrongly excluded items) | **policy replay**: the insurer's parameters are read back from its printed deductions, then each one is swapped for the policy's value in turn. Each finding's impact is that step's change in payout, so the impacts add up exactly to the total shortfall | High |
| Possible upcoding | SQL: billed complexity tier vs the typical tier for the procedure | Worth reviewing |
| Price far above benchmark | SQL: unit price > 1.3x the 90th-percentile reference price | Worth reviewing |
| Clinical consistency (Gemini mode only) | Gemini review of lines vs diagnosis | Worth reviewing |

**Design choice:** detection and money math are deterministic, so they are reproducible and testable. Gemini handles the language: plain-language explanations, letter drafting, cross-checking extracted policy terms, transcribing unfamiliar bill layouts, and an optional clinical review. Gemini output is guarded: if a drafted letter or explanation contains a rupee amount that isn't in the evidence, it is discarded and the template text is used.

## Evidence details

Each finding in `GET /api/cases/{id}` now includes an additive `evidence` object. It maps affected bill lines to their uploaded document key, extracted source row, and page when the parser provides it; policy citations are marked verified only when the clause ID, source name, and text match a clause in the parsed policy. Missing or mismatched evidence is reported as unavailable/partial, not reconstructed. Arithmetic checks include their input values and calculation steps. Existing fields and generated PDF reports remain unchanged for older clients. The browser shows page references rather than in-PDF highlights because the current vanilla frontend has no PDF rendering/highlighting library.

## Architecture

```
Browser (web/)  --POST /api/audit-->  FastAPI on Cloud Run  --SSE /events-->  live agent trace
                                          |
                    ADK  claimshield_orchestrator (SequentialAgent)
                      â”œâ”€ intake_agent          Document AI -> normalised bill + policy JSON; rows -> BigQuery
                      â”œâ”€ policy_agent          Vertex AI Search -> governing clauses -> structured terms (+ Gemini cross-check)
                      â”œâ”€ findings_team (ParallelAgent)
                      â”‚    â”œâ”€ audit_agent      duplicates / arithmetic / stay / totals SQL + policy replay
                      â”‚    â””â”€ benchmark_agent  reference-pricing + upcoding SQL
                      â”œâ”€ reconciliation_agent  billed vs covered vs owed; likely and upper-bound recovery
                      â””â”€ dispute_agent         Gemini explanations + letters; report PDFs -> Cloud Storage
```

Agents pass results through ADK session state (`EventActions.state_delta`). Every step writes to a per-case trace. The UI streams it live, and each event is also logged as one JSON line, which Cloud Run ships to Cloud Logging.

**Every Google Cloud service has a local fallback**, chosen per service by a flag. If a flagged service fails to initialise, the local fallback takes over and the trace says so, so a missing credential can't take the demo down:

| Concern | Google Cloud | Local fallback |
|---|---|---|
| Parsing | Document AI Form Parser | pdfplumber |
| Warehouse + audit SQL | BigQuery | in-memory SQLite (same SQL files) |
| Clause retrieval | Vertex AI Search (chunked) | BM25 over parsed clauses |
| Reasoning / drafting | Gemini on Vertex AI | deterministic templates |
| Files | Cloud Storage | `var/` directory |
| Orchestration | ADK (also locally) | plain Python runner |

## Folder structure

```
claimshield/
  agents/      steps.py (the six agents' logic) Â· adk_agents.py (ADK graph) Â· runner.py
  domain/      coverage.py (entitlement engine + policy replay) Â· policy_terms.py
  services/    parsing.py Â· store.py Â· retrieval.py Â· llm.py Â· storage.py Â· __init__.py (fallback wiring)
  sql/         duplicates Â· arithmetic Â· benchmark_outliers Â· upcoding Â· schema_bigquery
  reports/     pdf.py (reconciliation report + letters)
  api/main.py  FastAPI + static UI
web/           index.html Â· app.js Â· styles.css (no build step)
data/
  generate.py  synthetic bills/policies generator (with planted issues)
  reference/   reference_pricing.csv Â· procedure_benchmarks.csv
  samples/     generated PDFs Â· catalog.json Â· expected.json (answer key)
scripts/       run_samples.py Â· setup_gcp.sh Â· load_bigquery.py Â· index_policies.py Â· deploy_cloud_run.sh
tests/         answer-key E2E tests (ADK and plain runners), API tests, Gemini guard tests
docs/          DEMO_SCRIPT.md
```

## Run locally (no cloud credentials needed)

```bash
make setup                 # python3.11 venv + deps
cp .env.example .env       # all cloud flags false by default
make test                  # answer key, money math, API, guards, isolation and CLI tests
make samples               # run all 4 synthetic bills, print findings
make run                   # http://localhost:8080
```

Open the UI, pick a sample case (or upload a bill and a policy PDF) and press **Audit**. `CLAIMSHIELD_DEMO_PACE=0.35` in `.env` slows the trace so it can be followed live. Local mode otherwise finishes in about a second.

To run in Docker on Linux instead: `make docker`. The local demo uses host networking
and binds to loopback. Docker Desktop requires its host-networking feature to be enabled;
otherwise use the native Windows setup below. Do not publish a demo container externally.

### Windows (PowerShell, Python 3.11)

From the repository directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_local.ps1
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -W ignore scripts/run_samples.py
$env:CLAIMSHIELD_DEMO_PACE = "0.35"
$env:CLAIMSHIELD_AUTH_MODE = "demo"
.\.venv\Scripts\python.exe -m uvicorn claimshield.api.main:app --host 127.0.0.1 --port 8080
```

The setup script accepts `-Python C:\path\to\python.exe` if `python` is not Python 3.11.
It creates an isolated environment, installs `requirements.lock`, and checks dependency
consistency. It does not create or overwrite `.env`. With no existing environment overrides,
the processing pipeline uses local services and ADK. The HTTP app requires an explicit
authentication mode, as shown above. The sample CLI emits UTF-8, including redirected output,
and returns a nonzero exit code if any audit fails.

### Reproducible dependencies and isolated tests

`requirements.txt` and `requirements-dev.txt` declare direct dependencies;
`requirements.lock` includes those manifests and pins the resolved transitive versions.
The dependency snapshot is validated on **CPython 3.11, Windows x64**. Other platforms
can require additional platform-specific dependencies (for example uvloop); their clean
installation must be verified separately. The Docker build still uses its existing runtime
manifest and is not covered by this Windows dependency snapshot.

Ordinary pytest runs force the five cloud service flags off before test collection, use
the repository's synthetic fixtures, and give each test temporary storage. Developer `.env`
settings cannot redirect these tests into normal case storage. External socket connections
in the pytest process are blocked; loopback remains available for local tools. CLI subprocess
tests inherit the isolated local configuration. ADK-labelled pipeline tests assert that ADK
actually ran, and both runners must match the same findings and monetary results.

To update dependencies, resolve the changed manifests in a fresh Python 3.11 environment,
regenerate the lock while preserving its platform markers and manifest include, then run
`pip check` and the full local suite. A successful local run does not verify live cloud adapters.

## Synthetic data

`python data/generate.py` regenerates everything. All people, hospitals, insurers and policy numbers are fictional, and every PDF is watermarked *SYNTHETIC - FOR DEMO*.

| Bill | Policy | Planted issues | Result |
|---|---|---|---|
| BILL-001 Sunrise, appendectomy | Suraksha Gold | duplicate CBC; Ceftriaxone 6 x â‚¹180 billed â‚¹1,800; Grade IV anaesthesia for a routine case; 20% co-pay at a network hospital (policy: 0%); admission kit deducted though the policy covers consumables | recovery â‚¹24,846 |
| BILL-002 Netra, cataract | Suraksha Gold | eye drops 2 x â‚¹450 billed â‚¹1,350; gross total â‚¹1,000 above the lines; sub-limit applied at â‚¹35,000 (policy: â‚¹40,000) | recovery â‚¹6,450 |
| BILL-003 Lotus, knee replacement | CareFirst Silver | room billed 6 days for 5 nights; physio twice on the same day; MRI 1.6x the benchmark; sub-limit at â‚¹1,00,000 (policy: â‚¹1,50,000) | â‚¹42,800 likely, up to â‚¹49,100 |
| BILL-004 Sanjeevani, dengue | CareFirst Silver | **none (clean bill)**: repeat CBCs on different days, correct 20% co-pay, correctly excluded admission kit | **0 findings** |

Correctly applied exclusions (attendant food on BILL-001, gloves and masks on BILL-003) are deliberately *not* flagged.

## Authentication and case access

The HTTP app defaults to **IAP mode** and refuses to start without valid authentication
configuration. The local synthetic demo requires `CLAIMSHIELD_AUTH_MODE=demo` (included
in `.env.example`), accepts only loopback clients and hosts, and refuses to start on Cloud Run.
Demo cases belong to one local demo identity; this mode is not multi-user authentication.

Production uses [native Cloud Run IAP](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)
for hosted Google sign-in and browser cookies. The application independently validates each
`X-Goog-IAP-JWT-Assertion` against Google's fixed public-key endpoint, with ES256, the exact
issuer and audience, issuance time and expiry. Unsigned user headers and caller-supplied
tenant IDs are never trusted. See [IAP signed-header verification](https://docs.cloud.google.com/iap/docs/signed-headers-howto).

Configure these server-side values before starting in IAP mode:

```dotenv
CLAIMSHIELD_AUTH_MODE=iap
CLAIMSHIELD_IAP_AUDIENCE=/projects/123456789/locations/us-central1/services/claimshield-ai
CLAIMSHIELD_PUBLIC_ORIGIN=https://your-service.example
CLAIMSHIELD_AUTH_MEMBERSHIPS='{"accounts.google.com:VERIFIED_SUBJECT_ID":"tenant-a"}'
CLAIMSHIELD_REVIEWER_SUBJECTS=accounts.google.com:VERIFIED_REVIEWER_SUBJECT_ID
```

Replace the examples with the service's actual signed-header audience, exact browser origin
(no trailing slash), and verified stable Google subject IDs. Membership is an operator-managed
allowlist, not a self-service field. Restart/redeploy with updated configuration when changing
membership. IAP access policy and application membership are both required. No administrative
sharing is implemented for ordinary case access: another member of the same tenant cannot access the owner's case.

### Insurer-side reviewer access and privacy assumptions

The separate review queue is available only to an authenticated reviewer subject explicitly
listed in the server-side `CLAIMSHIELD_REVIEWER_SUBJECTS` comma-separated allowlist. Reviewer
accounts can see case findings only within their existing tenant; tenant membership is still
verified from the signed IAP subject mapping. This is a tenant-wide reviewer privilege, not
per-case assignment or an insurer-to-hospital integration. In local loopback demo mode, the
single `local-demo` identity can use the synthetic review UI.

Review evidence and explanations are stored under the existing case storage prefix and inherit
the same storage encryption, access, backup and retention controls as the case. Configure the
reviewer allowlist and storage retention to match organizational privacy requirements; the app
does not currently provide per-field redaction or reviewer audit export. Reviewer decisions are
evaluation labels, not automated determinations of fraud. Synthetic false-positive metrics use
the versioned labels in `data/evaluation/synthetic_review_labels.json`.

The claim journey dashboard uses an owner-scoped history index and the same case ownership record
before reading any audit, timeline, or manually recorded settlement. Actual settlement and
confirmed recovery fields are user-entered when not present in the bill settlement; ClaimShield
does not verify those entries. Synthetic catalog bills are labeled as simulated demo data. CSV
exports are available only after the dashboard's owner checks pass.

Ownership is created once in `cases/<id>/owner.json` before processing begins. Every result,
event stream, PDF and text-letter request checks this record before consulting memory or
loading stored results. Legacy cases without this metadata, including standalone CLI outputs,
are inaccessible over HTTP. They are left untouched; any migration requires an explicit,
trusted owner assignment. A case ID is an identifier, never an access credential.

The browser retains the existing layout and uses IAP's session for requests and downloads.
Mutating requests require the exact configured `Origin` and `X-ClaimShield-Request: 1`;
the UI supplies the latter automatically. Programmatic callers must send these too, along
with credentials accepted by IAP. The application does not create its own session cookie
or store tokens in browser storage. Live streams close when their verified assertion expires;
the UI asks the user to reload to refresh sign-in. Existing jobs continue independently.

The deployment script now validates auth settings, enables native IAP and disables anonymous
Cloud Run invocation. Before any separately authorized deployment, enable the IAP API, set up
the OAuth client if needed, grant the IAP service agent Cloud Run Invoker, and configure IAP
user access. Use `PYTHON` to select an interpreter containing the project dependencies when
running the script. The script safely transports JSON membership values via an env-vars file.
These infrastructure prerequisites and the real hosted login flow have not been verified
against a live project. Local tests use real signatures with synthetic keys and identities.

## Pre-claim assessment

The **Pre-Claim Assessment** form accepts a policy PDF, proposed treatment, estimated total,
room category and rate, planned admission date, expected nights, and optional hospital network
status, patient age, deductible, and itemized expense lines with categories. `POST /api/preclaim` parses the uploaded wording through the
configured parser and retrieves relevant policy clauses through the configured retrieval service
(with local retrieval fallback). The service reuses the deterministic entitlement calculation for
room-rent limits, procedure sublimits, co-payment, covered non-medical categories, and known sum
insured amounts. A deductible is applied after co-payment and sublimit when stated in the policy
or explicitly entered by the user; the result identifies that assumed order.

The response includes estimated insurer and patient payments, the arithmetic breakdown, policy
clause evidence, missing inputs, and uncertainty notes. If treatment eligibility, the applicable
co-payment, a relevant network status, or a listed expense's exclusion status is unresolved or
conflicting, payment amounts are withheld instead of treating missing terms as confirmed coverage.
An estimate cannot establish waiting-period eligibility, remaining aggregate sum insured, medical
necessity, network arrangements, or actual insurer approval. Confirm these with the insurer before
incurring costs.

## Claim rejection analysis

The **Claim Rejection Analysis** form accepts a rejection-letter PDF and the applicable policy
PDF. `POST /api/rejection-analysis` extracts denial reasons and document requests from parsed
notice text, retrieves matching policy clauses, and classifies each reason as supported by the
provided evidence, potentially inconsistent with the supplied policy wording, or insufficiently
documented. Gemini may propose verbatim reason excerpts when enabled; excerpts not found in the
parsed notice are discarded. The appeal or clarification draft uses the existing guarded letter
drafting service and is shown for review before the user copies it.

Document requests quoted from the notice are shown separately from clarification suggestions.
The analysis does not determine whether a rejection is lawful, predict an appeal outcome, or
invent regulatory deadlines. OCR/parsing quality and incomplete policy wording can affect results;
users should verify extracted text, cited clauses, and the draft before sending.

## Smart claim document checker

The document checker accepts multiple PDFs and classifies each from extracted page text; filenames
do not determine document type. It recognizes hospital bills, discharge summaries, receipts,
prescriptions, investigation reports, claim forms, and policy documents. It reports exact-file
duplicates and fields that were not detected; a field not detected in extracted text must be checked
against the original document.

Generic required and recommended document lists by claim type live in
`data/document_requirements.json`. These entries are labeled generic recommendations, not insurer
rules. The same file can hold insurer-specific requirements. Such entries are enforced and labeled
verified only when the insurer entry has `"verified": true` and a non-empty `"source"` describing
the verification basis. No insurer-specific requirements are configured by default. Image-only
scans with no extractable text remain uncertain unless the configured parser can read them.
`POST /api/document-check` returns document classifications, checklist statuses, and CSV text for
export in the browser.

## IRDAI regulatory reference

The regulatory registry stores document metadata in the configured existing storage backend and
original PDFs / extracted page text as SHA-256 keyed objects. The initial allowlist contains the
May 29, 2024 Health Insurance Business Master Circular and its English and Hindi annexures, based
on the official IRDAI circular listing. The listing metadata is not an imported or verified PDF;
provisions remain unavailable until an administrator supplies the official PDF and reviews its
source and currentness. No regulatory rules are pre-approved or enforcement-enabled.

`GET /api/regulatory/documents`, `GET /api/regulatory/rules/search`,
`POST /api/regulatory/rules/{rule_id}/applicability`, and
`GET /api/regulatory/documents/{document_id}/evidence` provide authenticated reference access.
Regulatory modifications use `/api/admin/regulatory/*` and require a verified subject in
`CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS`, independently of the insurer reviewer allowlist.
Candidate rule quotations must match the extracted source page; approval requires a reviewed
source document, a checked effective date, and a currentness review. Applicability results do not
determine whether an insurer violated a rule. The administrator's currentness review is manual;
the service does not monitor IRDAI for later amendments.

## Deploy to Google Cloud

### Services to enable
Cloud Run, Cloud Build, Artifact Registry, Document AI, BigQuery, Vertex AI Search (Discovery Engine), Vertex AI (Gemini), Cloud Storage, Cloud Logging. `scripts/setup_gcp.sh` enables them all.

### Steps
```bash
gcloud auth login && gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project GOOGLE_CLOUD_LOCATION=us-central1
make gcp-setup        # APIs, bucket, BigQuery dataset, service account + roles, Form Parser, search data store
                      # -> prints the .env values; paste them into .env
make bq-load          # tables + reference pricing into BigQuery
make index-policies   # sample policies -> GCS -> Vertex AI Search (indexing takes a few minutes)
# Configure IAP and the authentication values above before deploying; demo mode is rejected.
make deploy           # Cloud Build + Cloud Run; prints the service URL
```

You can turn services on one at a time, e.g. only `CLAIMSHIELD_USE_GEMINI=true`, and check the badges in the UI header and the trace. Each one shows the backend actually used.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | - | project id |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Cloud Run + Gemini region |
| `CLAIMSHIELD_USE_DOCAI` / `DOCAI_LOCATION` / `DOCAI_PROCESSOR_ID` | `false` / `us` / - | Document AI Form Parser |
| `CLAIMSHIELD_USE_BIGQUERY` / `BQ_DATASET` | `false` / `claimshield` | BigQuery warehouse |
| `CLAIMSHIELD_USE_VERTEX_SEARCH` / `VERTEX_SEARCH_LOCATION` / `VERTEX_SEARCH_DATASTORE_ID` | `false` / `global` / `claimshield-policies` | clause retrieval |
| `CLAIMSHIELD_USE_GEMINI` / `GEMINI_MODEL` | `false` / `gemini-2.5-flash` | reasoning + drafting |
| `CLAIMSHIELD_USE_GCS` / `GCS_BUCKET` | `false` / - | uploads + reports |
| `CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS` | - | verified IAP subjects allowed to ingest and review regulatory references |
| `CLAIMSHIELD_USE_ADK` | `true` | ADK orchestration (plain runner if `false` or ADK missing) |
| `CLAIMSHIELD_OUTLIER_RATIO` | `1.3` | benchmark threshold (x p90) |
| `CLAIMSHIELD_DEMO_PACE` | `0` | seconds to pause per trace step |
| `CLAIMSHIELD_WORKERS` | `2` | local queue workers per process; integer from 1 to 16 |
| `CLAIMSHIELD_QUEUE_CAPACITY` | `100` | maximum queued and running jobs |
| `CLAIMSHIELD_USER_PENDING_LIMIT` | `3` | maximum pending jobs per issuer, tenant and user |
| `CLAIMSHIELD_INTERNAL_TOKEN` | - | shared secret required by the internal job-processing endpoint outside local demo mode |
| `CLAIMSHIELD_USE_CLOUD_TASKS` / `CLOUD_TASKS_QUEUE` | `false` / - | reserved deployment variables; runtime job submission still uses the local SQLite queue |

Boolean flags accept `true`/`false`, `yes`/`no`, `on`/`off`, or `1`/`0` (case-insensitive). Invalid boolean values, non-positive worker limits, worker counts above 16, and non-finite or non-positive outlier ratios stop startup with a configuration error. The deployment script passes `CLAIMSHIELD_INTERNAL_TOKEN` to Cloud Run when set; store a high-entropy value as a secret and configure the caller to provide it. The internal processing endpoint also passes through the app's IAP authentication middleware; local demo access is limited to loopback.

Local file storage resolves every object key beneath its configured root, including symlink targets, and rejects keys that escape that root. Provider initialization failures continue to use the configured local fallback; detailed diagnostics stay in server logs while the health response exposes a sanitized warning.

## What has and hasn't been verified

- **Verified locally:** the full pipeline through ADK `InMemoryRunner` and the plain runner on all four bills, matched against the answer key; reconciliation amounts; the HTTP API including uploads and validation; PDF generation; the UI, rendered in headless Chrome; and the Gemini guardrails (with a stubbed model).
- **Written but not yet run against real Google Cloud**, because no project was available while building: the Document AI, BigQuery, Vertex AI Search, Gemini and Cloud Storage adapters, plus `setup_gcp.sh`, `index_policies.py` and `deploy_cloud_run.sh`. The SQL is written in the subset both engines accept (including integer casts, since BigQuery can't partition on FLOAT64). Expect small fixes on first contact, and try one flag at a time.
- **Vertex AI Agent Engine:** the agent graph (`claimshield/agents/adk_agents.py: root_agent`) passes everything through session state, so it can be deployed there. That isn't scripted yet because the demo serves from Cloud Run.

## Limits and next steps

- The local parser expects an itemised table with Qty and Amount columns. With Gemini on, unfamiliar layouts are transcribed by Gemini instead.
- Benchmarks and procedure tiers are a small synthetic table. A real deployment would use regional tariff data such as PM-JAY/CGHS package rates.
- Scaling beyond the demo: the live trace lives in instance memory, so the deploy pins one instance with session affinity. To scale, move traces to Firestore or Pub/Sub, and the audit itself to Cloud Run Jobs or Cloud Tasks.

## Guided two-group claim review

The main page now accepts medical documents and insurance documents together, identifies missing evidence, lists every extracted policy clause, and prioritizes next actions. Audit and guided-review requests use a bounded persistent single-host queue. See [claim review behavior, feature boundaries, and capacity guidance](docs/CLAIM_REVIEW.md). This does not yet establish full claim eligibility or provide horizontally scaled cloud processing.

