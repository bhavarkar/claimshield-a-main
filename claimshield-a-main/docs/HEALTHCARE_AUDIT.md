# Person 3: healthcare reference data and deterministic audit logic

## Starting implementation assessment

The repository already had a Python/FastAPI backend, Pydantic `Bill`, `LineItem`, `PolicyDoc`,
`PolicyTerms`, `Finding` and `Reconciliation` models, and six deterministic audit steps orchestrated
by ADK or a plain Python runner. It used SQLite for local analytical SQL, optional BigQuery tables
for bill lines/findings/prices, and filesystem/GCS storage for case JSON, original documents and PDFs.

Existing checks covered duplicate charges, arithmetic, excess room days, printed totals, settlement
replay, informational price outliers and coding signals. Evidence helpers tied findings to source
bill rows and exact policy quotations. `entitlement()` calculated direct room-rent excess; there
was no policy-specific proportionate adjustment of other charges.

`RegulatoryRegistry` already supported an IRDAI document allowlist, original PDF hashing, exact
page-text quotations, administrator review, currentness checks, applicability, conflicts and
supersession. No non-payable item bindings were connected to the audit engine. Listing metadata
was not an imported or approved IRDAI rule. The reference CSVs were explicitly synthetic and had
no verified CGHS, PM-JAY or NPPA prices.

Existing APIs included `/api/audit`, `/api/claim-review`, case results/events/files, pre-claim,
policy history, rejection analysis, regulatory ingestion/review/applicability, and reviewer tools.
The starting suite contained 166 tests covering those features. No lint/type checker was configured.
Existing uncommitted work in the workspace was preserved. Extraction/Gemini and deployment
configuration were outside this change.

## Person 1 input contract

Use the existing normalized models. Call `run_healthcare_audit(input, services)` in Python, or send
`Content-Type: application/json` to the existing `POST /api/audit`. Authentication, origin and
`X-ClaimShield-Request: 1` protections are the existing API protections.

Top-level fields:

| Field | Meaning |
|---|---|
| `claim_id` | Required string, 1–100 characters; a caller label, not an access credential |
| `bill` | Existing `Bill`, with `line_items` using existing `LineItem` fields |
| `policy_terms` | Existing `PolicyTerms`; missing terms do not become regulatory approval |
| `policy_doc` | Existing `PolicyDoc` with original source clauses, identities, versions and real pages |
| `context` | Existing `ApplicabilityRequest`: insurance_category, event_date, claim_context, policy_type |

Each bill line uses `line_no`, `service_date`, `description`, `category`, `qty`, `unit_price`, `amount`,
and `unit`. `line_no` values must be unique. Quantities must be positive and monetary inputs finite
and non-negative. Credit/discount lines require upstream normalization. The limit is 2,000 lines
and 2 MiB of JSON. An empty bill explicitly needs review. `stated_gross_total`, when supplied, is
preserved; otherwise the structured input uses the line sum. It does not silently repair a printed total.

Optional additions to `LineItem` are `normalized_description`, `strength`, `dosage_form`, `pack_size`,
`manufacturer`, and `price_basis` (`unknown`, `net_excluding_tax`, or `tax_inclusive`). Matching
normalizes the original description conservatively; an LLM-supplied normalized description does
not override source text or silently create a verified synonym. Supply `source_page`/`source_text`
when available. Missing pages are not reconstructed.

Optional additions to `Bill` are `facility_category`, `room_category`, `admission_days`, and
`actual_room_rent_per_day`. `region` already existed. With one consistent normal-room rate, the
actual daily rate can come from the room line; mixed or conflicting rates require review.

`policy_terms.room_rent_limit` remains a rupee daily limit. For a percentage limit, set
`room_rent_limit_type: "percent_sum_insured_per_day"`, `room_rent_limit_pct` and `sum_insured`.
The other supported types are `per_day`, `category` and `unknown`; a category-only cap currently
requires manual review. Omitted type preserves the existing parser's daily numeric convention.
`clause_refs.room_rent` must identify a source clause in `policy_doc`; the source name and any
document/version identities must agree. An explicitly supplied page is required for a supported
room-cap citation. This is provided-policy evidence, not regulator-authenticated evidence.

IRDAI applicability uses the supplied event context, not today's date. Price records use each
line's service date, falling back to the supplied event date. Missing applicability data means
unavailable/needs review. Do not send `rates`, `room_rules` or verification flags as audit input;
only the server-side reviewed registry supplies active reference records.

An exact, complete **synthetic test-only** input is in
`tests/fixtures/healthcare/normalized_input.json`. It contains fictional policy wording and is
not authoritative insurance or pricing data.

## Person 2/frontend output contract

JSON audit requests return the deterministic result synchronously, retaining flat report fields
and adding `status: "complete"` plus the same report under `healthcare_audit`. JSON requests do
not create a stored case. Existing multipart PDF/sample
requests still return `{ "case_id": "..." }` and use the existing queue/events/result flow.
`GET /api/cases/{case_id}` now exposes the additive `healthcare_audit` object for PDF audits.
These reference findings do not alter the legacy `findings`, settlement reconciliation, dispute
letters, or recovery totals. The frontend now renders them in a separate healthcare report panel;
missing reports on older cases are hidden.

```text
claim_id: string
total_billed: number
total_billed_basis: "printed_total" | "sum_of_bill_items"
potential_discrepancy: number
benchmark_variance: number
finding_counts: { clear: integer, flagged: integer, needs_review: integer }
warning_count: integer
findings: HealthcareFinding[]
summary_basis: string
warnings: string[]  // Storage adapter adds this; pure domain function omits it.
```

`HealthcareFinding` extends the existing `Finding`: retain `type`, `title`, `bill_lines`, `policy`,
`explanation`, `impact`, `confidence`, `evidence`, `counterparty`, `detected_by` and `method`. Added fields:

```text
claim_id: string
bill_item_id: string | null  // Existing line_no rendered as a string; null for claim-level checks.
severity: "info" | "warning" | "high"
status: "clear" | "flagged" | "needs_review"
billed_amount: number | null
reference_amount: number | null  // Reference total for this line's quantity, not unit price.
potential_difference: number | null  // Signed billed-minus-reference difference; null when undetermined.
rule_id: string | null
rule_name: string
confidence_score: number  // 0..1; matching/calculation confidence, not probability of coverage/fraud.
match_status: "confirmed_match" | "possible_match" | "no_match" | "not_applicable"
comparison: string | null
```

Types added to the existing enum are `NON_PAYABLE_ITEM`, `ROOM_RENT_LIMIT`,
`ROOM_RENT_PROPORTIONATE`, `HEALTHCARE_BENCHMARK`, and `NPPA_REFERENCE`.
Comparisons include `above_benchmark`, `within_benchmark`, `below_benchmark`,
`benchmark_unavailable`, `needs_manual_review`, `no_match` and `reference_unavailable`.

`evidence.source` carries source name/type, URL or document ID, page if available, effective date,
version, verification status, imported/review metadata and notes. Policy-based calculations
identify the supplied quotation. `evidence` also contains calculation inputs/formula or missing
qualifiers, and the original bill row/page when supplied. Unknown values stay null.

`potential_discrepancy` sums the largest positive supported non-benchmark adjustment per item.
`benchmark_variance` separately sums the largest positive CGHS/PM-JAY variance per item, avoiding
double-counting the same item across two schemes. Neither is guaranteed recovery. Do not sum
these with each other or with the legacy reconciliation. A clear finding means that particular
check was clear; missing sources and fuzzy matches are never whole-claim approval/rejection.

An exact source-unavailable response for the synthetic input is in
`tests/fixtures/healthcare/source_unavailable_output.json`. `/openapi.json` also documents JSON
and multipart request formats and the `HealthcareFinding` schema.

## Source import and approval

The new registry uses the existing storage backend at `healthcare-reference/registry.json` with
`schema_version: 1`, `rates` and `room_rules` maps. No existing SQLite/BigQuery migration or
reference CSV replacement is required. No prices or regulatory text are seeded.

`HealthcareRate` supports source types `CGHS`, `PMJAY`, `NPPA`; item/normalized names, aliases,
category, unit, reference amount/currency, geography, facility category, strength, dosage form,
pack size, manufacturer and price basis. Every record also has source name, URL/document ID,
optional page, effective/expiry date, version, review status, activation flag, import/review
timestamps, reviewer, currentness flag and notes. Use `geography: "national"` only when source
scope is explicitly nationwide. Missing geography cannot support an automatic comparison.
Omitted optional product/facility restrictions mean the reviewed record did not specify that
restriction; reviewers must verify this interpretation against the original tariff.

Import source-derived records as pending using either:

```powershell
.venv/Scripts/python.exe scripts/import_healthcare_references.py source-derived.json --validate-only
.venv/Scripts/python.exe scripts/import_healthcare_references.py source-derived.json
```

The input envelope is `{ "rates": [...], "room_rules": [...] }`. A synthetic pending envelope
is supplied under `tests/fixtures/healthcare/pending_reference_import.json` **for validation/tests
only; never import it as authoritative production data**. The script cannot approve or activate
records. It uses configured storage only on an actual import; validation does not initialize services.

Authenticated API controls reuse `CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS`:

- `POST /api/admin/healthcare-references/import`: typed import envelope; always resets review/activation.
- `POST /api/admin/healthcare-references/{kind}/{id}/review`: `kind` is `rates` or `room_rules`.

Review JSON contains `verification_status` (`approved`, `rejected`, `needs_update`),
`enforcement_enabled`, `currentness_checked`, `source_checked`, `interpretation_checked` and
`explanation`. Approval requires a source URL/document ID, verified effective date and explicit
source/interpretation/currentness confirmations by an authorized reviewer. URLs/IDs are provenance,
not automatic authentication of the source. The importer does not scrape or invent rates.

Mark test records `synthetic: true`. Marked synthetic records, reserved `.invalid` sources and
local-demo approvals cannot become active outside demo mode; production activation attempts are
rejected. This also applies to regulatory item rules. Administrator review remains responsible
for authenticating unmarked real sources.

IDs and record content are immutable: import corrections/new versions with new IDs, and disable
invalid old versions via review. Overlapping applicable versions require review rather than
arbitrarily picking the cheapest/latest. If historical effective/expiry intervals need correcting,
import reviewed replacement records with those intervals and deactivate the incorrect record.

IRDAI item rules reuse the existing regulatory PDF ingestion/candidate/review endpoints. The optional
`RegulatoryRule.item_rule` contains `canonical_description`, `aliases`, optional `categories`, and
`effect` (`policy_dependent` by default; `non_payable` requires explicit human-reviewed interpretation).
Canonical names and aliases must appear in the exact cited quotation before rule approval.
The existing source PDF, citation, effective date, applicability and activation gates still apply.
Document revocation or source integrity failure makes the binding unavailable at audit time.
Rule applicability is not a finding of a regulatory violation.

## Room-rent conditions

Direct daily excess is reported using the provided limit and exact selected policy citation.
For normal room proportions, a `RoomAdjustmentRule` must also be server-approved, enabled,
current, effective for the event and bound to the selected policy document ID, version ID and
exact policy citation. It must explicitly list affected categories/items. Excluded categories/items
take precedence. Room/ICU lines are not adjusted a second time by this proportionate module.

The only supported configured formula is `allowed_over_actual`:

```text
ratio = allowed_daily_room_rent / actual_daily_room_rent
reference_line_amount = round(billed_line_amount * ratio, 2)
potential_difference = billed_line_amount - reference_line_amount
```

No verified rule, missing/mixed rates, conflicting room days, unclear wording, wrong version or
multiple matching configurations returns needs_review. No rule means no automatic adjustment
across other items. ICU direct caps are supported separately; a universal ICU proportion is not inferred.
Calculations use decimal rounding to two paise digits. Existing financial recovery math is unchanged.

## Matching and rate safety

Normalization handles Unicode/case/spacing while preserving numbers, decimals, ratios and dosage
qualifiers. Exact matches score 1.0; record-specific approved aliases score .98. Fuzzy similarities
at or above .78 are only candidates, capped at .89 and always needs_review without an estimated
deduction. Lower scores are no automatic match. No general CBC/Hemogram equivalence is assumed.

Price comparison requires a verified, enabled, currently reviewed record with matching effective
period, category, geographic/facility scope, qualifiers and unit. NPPA pharmacy checks additionally
require recorded strength/dosage form and matching explicit tax/price basis. No quantity/unit/tax
conversion is guessed. Arithmetic-inconsistent lines require review before price comparison.
CGHS/PM-JAY rates are informational comparisons; NPPA differences remain potential discrepancies
requiring confirmation of applicability and specifications, not automatic legal findings.

## Outstanding data and operational boundaries

Real IRDAI item-source PDFs and reviewed interpretations; current and historically applicable
CGHS tariffs with city/facility/unit rules; PM-JAY package/test mappings and applicable state/version
rules; NPPA medicine/device notifications with product specifications, ceiling basis and effective
dates; and actual policy proportion clauses with affected/excluded categories are still required.

No authoritative dataset was available in the repository, so all such production rules remain
unconfigured/unavailable. Synthetic rates/quotations exist only in tests and test fixtures.
The new reference metadata follows the existing JSON storage pattern; distributed update coordination,
source refresh automation, OCR/clinical extraction and deployment changes remain outside Person 3 scope.

## Validation

Tests cover clean, flagged, ambiguous and missing-source claims; approval gating and unsupported
aliases; verified proportional rules and exclusions; wrong/missing policy evidence; percentages;
conflicting versions; geography, dates, units and product/tax qualifiers; non-finite/negative inputs;
empty bills, missing pages, source corruption; JSON API integration and administrator authorization.
The existing pipeline answer-key suite verifies that prior recovery calculations remain unchanged.

Final integration checks: 226 Python tests passed, plus 9 frontend/authentication tests. Backend/importer
compilation, frontend JavaScript syntax, reference-import validation-only mode and `git diff --check`
passed. No lint/type checker was configured. No live cloud adapter or authoritative dataset was
tested or activated. The published synthetic input/output pair is checked through the HTTP API.
An independent uvicorn process also exercised actual loopback HTTP JSON and queued PDF flows.
See [the final integration audit](INTEGRATION_AUDIT.md) for field gaps, fixes, exact replay fixtures
and the unresolved production queue/CPU/distributed-write limitations. No visual browser review
or actual Cloud Run deployment was performed.

## Files in this change

- `claimshield/models.py`: optional normalized qualifiers and finding types.
- `claimshield/domain/medical_matching.py`: reusable conservative matching.
- `claimshield/domain/healthcare_audit.py`: input/output contract, checks, calculations and summary.
- `claimshield/services/healthcare_references.py`: pending imports and administrator-reviewed records.
- `claimshield/services/healthcare_audit.py`: storage/regulatory integration and legacy-state adapter.
- `claimshield/services/regulatory.py`: source-bound item rules and use-time source verification.
- `claimshield/agents/steps.py`: additive healthcare report in the existing pipeline.
- `claimshield/api/main.py`: JSON audit input, reference administration and API documentation.
- `scripts/import_healthcare_references.py`: operator import/validation path.
- `tests/test_healthcare_audit.py`: 23 positive, negative and ambiguous test cases including parametrization.
- `tests/fixtures/healthcare/normalized_input.json`: exact synthetic handoff input.
- `tests/fixtures/healthcare/source_unavailable_output.json`: exact corresponding API response.
- `tests/fixtures/healthcare/pending_reference_import.json`: synthetic test-only import envelope.
- `docs/HEALTHCARE_AUDIT.md`: assessment, contracts and operating boundaries.
