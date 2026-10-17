# Guided claim review

The main user journey is medical documents + insurance documents â†’ evidence review â†’ next actions.
It accepts one claim at a time, with 1â€“10 PDFs in each group, at most 15 MB per file and 50 MB total.
Users explicitly select the itemised bill and main policy wording; the system does not guess which
renewal or bill governs the claim. `/api/claim-review` uses the same authentication and case ownership
checks as `/api/audit`. Original files remain in owner-protected case storage.

## Feature review

| User need | Implemented behavior | Remaining boundary |
|---|---|---|
| Upload once | Two multi-file inputs; related tools reuse selected files in the same tab | Reuse after a browser reload requires reselecting files |
| Know what is missing | Document agent identifies supported document types, exact duplicates and missing checklist fields | Generic recommendations are not insurer-specific requirements; scans require readable extraction/OCR |
| Check policy clauses | Every extracted numbered clause is listed with original text and page. Optional Gemini drafts compare medical evidence with policy conditions, with exact quotation checks | AI reasoning is not independently verified; unnumbered/missed clauses and full applicability still need review |
| Understand non-payable expenses | Expense review identifies potentially non-payable lines under the extracted terms | Does not turn a line-item exclusion into rejection of the whole claim |
| Recover eligible amounts | Existing deterministic bill, settlement and benchmark agents produce financial scenarios and evidence | Benchmarks are illustrative; eligibility, aggregate balance, deductibles and endorsements may change outcomes |
| Know the next step | Three immediate actions, expandable full action plan, links to relevant tools | No claim is automatically submitted, approved or rejected |
| Reduce follow-up delays | Existing timeline, query response, document attachments and follow-up drafts remain available | No insurer portal integration, automatic reminders or invented regulatory deadlines |
| Plan treatment | Existing pre-claim, what-if and cashless request tools remain under task-specific disclosures | These remain separate assessments and use their own inputs |
| Understand rejection/history | Existing rejection, multilingual explanation and policy history tools remain available | Supplementary insurance documents are not silently merged into the audit terms |
| Keep professional features separate | Reviewer and regulatory controls are collapsed and loaded on demand | Server-side access checks remain authoritative |

## Agent responsibilities

1. Document agent: classify the complete upload pack and build an evidence checklist.
2. Intake agent: normalize the selected bill and selected policy.
3. Policy agent: retrieve financial clauses and extract supported terms.
4. Audit agent: check charges and settlement deductions.
5. Benchmark agent: compare charges with configured references, concurrently with the audit agent.
6. Reconciliation agent: calculate the financial scenarios deterministically.
7. Dispute agent: produce evidence-bound draft letters and reports.
8. Clause review agent: compare medical evidence against policy clauses using optional Gemini; validate exact quotations and keep all interpretations as unconfirmed drafts.
9. Guidance agent: account for every extracted clause and prioritize evidence gaps and next actions.

These are tasks within the existing Python/ADK architecture, not separate unbounded LLM calls.
The new clause reviewer uses at most eight sequential model calls per claim, with six clauses and eight selected medical page excerpts per call. At most 48 clauses and 120,000 medical text characters enter this AI comparison stage; all extracted clauses remain visible regardless. Context omissions are disclosed. Gemini HTTP requests have a 60-second timeout. Malformed, duplicate or unsupported citations, unsupported currency amounts and missing evidence prevent a favorable/adverse draft. Missing information downgrades a proposal to needs verification.

Gemini remains optional; deterministic local operation does not claim to be live AI reasoning.
Supporting medical records contribute to document completeness and, when Gemini is configured, draft
clause comparisons. These do not establish clinical necessity. The overall claim status remains `needs_verification`; this implementation does
not yet establish all facts needed to label an entire claim eligible or ineligible.

## Capacity and durability

Both audit entry points now submit persisted jobs instead of spawning unlimited background audits.
SQLite transactions enforce queue capacity and per-owner outstanding limits. Worker leases use tokens,
renew every 30 seconds and expire after 120 seconds for restart recovery. Infrastructure exceptions
retry with backoff up to three attempts; invalid documents reported by the audit runner are terminal.
Completed result persistence makes redelivery skip finished work. Each processing attempt has an isolated analytical ID so partial warehouse rows from a previous attempt cannot inflate a later calculation. Final results and traces are read from
storage; completed HTTP jobs release transient local analytical rows. Local file replacement is atomic.

| Setting | Default | Scope |
|---|---|---|
| `CLAIMSHIELD_WORKERS` | 2 | Concurrent claim jobs per application process; must be an integer from 1 to 16 |
| `CLAIMSHIELD_QUEUE_CAPACITY` | 100 | Outstanding queued and running jobs |
| `CLAIMSHIELD_USER_PENDING_LIMIT` | 3 | Outstanding jobs per issuer, tenant and user |

The application rejects invalid boolean flags, non-positive queue limits, worker counts above 16,
and invalid outlier ratios at startup. See the README environment-variable table for accepted boolean
spellings and the internal job token.

When full, the API returns HTTP 429 with `Retry-After: 10`. Upload validation happens before enqueueing.
Rejected enqueue attempts can leave an inaccessible staging case on disk; storage retention must include
such staging artifacts. Queue records and case files require an operational retention policy.

**Deployment boundary:** use one application process on one host with persistent local disk for this
queue. SQLite is not a horizontally distributed queue. Ephemeral Cloud Run filesystems do not provide
restart durability, and request-based CPU allocation can suspend background work. The deployment
script pins Cloud Run to one instance and enables CPU outside requests, but does not turn the local
SQLite queue into Cloud Tasks. An internal processing endpoint exists for trusted dispatch; requests
must pass the normal IAP boundary and present `CLAIMSHIELD_INTERNAL_TOKEN` (local loopback demo is the
exception). The `CLAIMSHIELD_USE_CLOUD_TASKS` and `CLOUD_TASKS_QUEUE` variables are currently forwarded
by the deploy script but are not consumed by runtime dispatch code. Other standalone assessment
endpoints still execute within their HTTP requests and are not governed by this job queue.

Before high-volume production: replace the local queue with a managed durable queue (such as Cloud
Tasks), dispatch authenticated workers, use shared durable job/trace/index storage with conditional
writes, enforce end-to-end task deadlines and provider quotas, configure CPU availability, and load-test the actual
OCR/model service quotas. Tune worker counts from measured memory and provider limits. Multiple web
processes currently multiply worker concurrency and cannot safely share all JSON history-index writes.
No production throughput or maximum-user guarantee is made by the local concurrency tests.

Local filesystem keys are checked after path resolution so stored objects cannot escape the configured
storage root, including through symlinks. This protects the local adapter; Cloud Storage object names
remain logical keys within the configured bucket.

## Verification

Tests cover the full multi-file HTTP flow, original clause quotations, duplicate files, missing evidence,
conflicting policy identities, atomic queue limits, expired-lease recovery, stale worker acknowledgement,
AI draft citation/amount guards and downgrade behavior, an eight-request burst with a maximum of two active workers, HTTP 429 backpressure and transient retry.
The existing feature suite and browser-authentication tests are also run. Live cloud services and visual
browser rendering require separate verification; this session had no connected browser.

