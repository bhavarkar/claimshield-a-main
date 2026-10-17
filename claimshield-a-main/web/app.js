"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inr(n) {
  const v = Number(n || 0);
  return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: v % 1 ? 2 : 0 });
}

const TYPE_LABEL = {
  DUPLICATE_CHARGE: "Duplicate charge",
  ARITHMETIC_ERROR: "Quantity x rate error",
  TOTAL_MISMATCH: "Total doesn't add up",
  QUANTITY_MISMATCH: "Quantity exceeds stay",
  UPCODING_SIGNAL: "Possible upcoding",
  PRICE_OUTLIER: "Above benchmark",
  COVERAGE_MISMATCH: "Settled differently from policy",
  CLINICAL_REVIEW: "Clinical question",
};

const state = { billFile: null, policyFile: null, billFiles: [], policyFiles: [], sample: null, samples: null, caseId: null, source: null, preauthId: null, poll: null };

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options, credentials: "same-origin",
    headers: { ...options.headers, "X-ClaimShield-Request": "1" },
  });
  if (response.status === 401 || response.status === 403 || response.redirected ||
      (response.headers.get("content-type") || "").includes("text/html")) {
    state.source?.close();
    throw new Error("Your session has expired or access was denied. Reload this page to sign in through Google.");
  }
  return response;
}

// ---------------------------------------------------------------- setup

async function init() {
  const [health, samples] = await Promise.all([apiFetch("/api/health").then((r) => r.json()), apiFetch("/api/samples").then((r) => r.json())]);
  renderStack(health.stack);
  state.samples = samples;
  renderSamples(samples);
  wireDrop("bill");
  wireDrop("policy");
  const dateInput = document.querySelector('#preclaim-form [name="hospitalization_date"]');
  dateInput.value = new Date().toISOString().slice(0, 10);
  document.querySelector('#what-if-form [name="hospitalization_date"]').value = dateInput.value;
  addWhatIfScenario();
  $("#preclaim-form").addEventListener("submit", submitPreclaim);
  $("#add-cost-line").addEventListener("click", addPreclaimCostLine);
  $("#rejection-form").addEventListener("submit", submitRejectionAnalysis);
  $("#document-check-form").addEventListener("submit", submitDocumentCheck);
  $("#add-scenario").addEventListener("click", addWhatIfScenario);
  $("#what-if-form").addEventListener("submit", submitWhatIf);
  $("#preauth-form").addEventListener("submit", submitPreAuthorization);
  $("#policy-history-files").addEventListener("change", renderPolicyDocumentTypeInputs);
  $("#policy-history-form").addEventListener("submit", submitPolicyHistoryCheck);
  $("#policy-explainer-form").addEventListener("submit", submitPolicyExplanation);
  $("#reviewer-refresh").addEventListener("click", loadReviewerQueue);
  $("#reviewer-evaluation-refresh").addEventListener("click", loadReviewerEvaluation);
  $("#reviewer-queue").addEventListener("click", submitReviewerDecision);
  $("#regulatory-refresh").addEventListener("click", loadRegulatoryReference);
  $("#regulatory-search-form").addEventListener("submit", (event) => { event.preventDefault(); loadRegulatoryReference(); });
  $("#regulatory-evaluate-form").addEventListener("submit", evaluateRegulatoryRule);
  $("#regulatory-documents").addEventListener("click", reviewRegulatoryDocument);
  $("#regulatory-rules").addEventListener("click", reviewRegulatoryRule);
  $("#regulatory-rules").addEventListener("click", inspectRegulatoryEvidence);
  $("#regulatory-ingest-form").addEventListener("submit", ingestRegulatoryDocument);
  $("#regulatory-preview-button").addEventListener("click", previewRegulatoryDocument);
  $("#regulatory-ingest-form [name=pdf]").addEventListener("change", () => { regulatoryPreviewHash = null; $("#regulatory-ingest-button").disabled = true; $("#regulatory-pdf-preview").textContent = "File changed; validate and preview it again."; });
  $("#regulatory-rule-form").addEventListener("submit", createRegulatoryCandidate);
  $("#dashboard-refresh").addEventListener("click", loadClaimDashboard);
  $("#dashboard-export").addEventListener("click", () => { location.href = "/api/dashboard/export.csv"; });
  $("#dashboard-claims").addEventListener("click", saveClaimOutcome);
  for (const [id, load] of [['claim-dashboard-panel', loadClaimDashboard], ['insurer-reviewer-panel', async () => { await Promise.all([loadReviewerQueue(), loadReviewerEvaluation()]); }], ['regulatory-reference-panel', loadRegulatoryReference]]) {
    const disclosure = $(`[data-tool="${id}"]`);
    disclosure.addEventListener('toggle', () => { if (disclosure.open) load(); });
  }
  $("#audit").addEventListener("click", startAudit);
  $("#again").addEventListener("click", reset);
  const linked = new URLSearchParams(location.search).get("case");
  if (linked && /^[a-z0-9-]{6,64}$/.test(linked)) showRun(linked);
}

async function loadClaimDashboard() {
  const error = $("#dashboard-error"), body = $("#dashboard-claims"), bars = $("#dashboard-reconciliation");
  error.classList.add("hidden"); body.innerHTML = '<tr><td colspan="8" class="muted">Loading claim history…</td></tr>';
  try {
    const response = await apiFetch("/api/dashboard"), data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load your claim history.");
    const totals = data.summary;
    $("#dashboard-notice").textContent = `${data.notice}${data.simulated_demo_claims ? ` ${data.simulated_demo_claims} synthetic sample claim(s) are labeled simulated demo data.` : ""}`;
    $("#dashboard-cards").innerHTML = [["Claims", totals.claim_count, "Stored claim records"], ["Claimed", inr(totals.claimed_amount), "Gross amount on bill"], ["Assessed payable", inr(totals.assessed_payable_amount), "ClaimShield policy replay estimate"], ["Actual settled", inr(totals.actual_settled_amount), `${totals.settlements_recorded} manually recorded`], ["Deductions", inr(totals.deductions), "Extracted from submitted bills"], ["Estimated recovery", inr(totals.estimated_recovery), `Upper estimate ${inr(totals.estimated_recovery_upper)}`], ["Confirmed recovery", inr(totals.confirmed_recovery), `${totals.confirmed_recovery_records} manually recorded`]].map(([label,value,detail]) => `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><small>${esc(detail)}</small></div>`).join("");
    body.innerHTML = data.claims.length ? data.claims.map(claim => {
      const outstanding = [...claim.outstanding_documents.map(v => `Document: ${v}`), ...claim.pending_actions];
      return `<tr class="dashboard-row"><td><b>${esc(claim.sample_id || claim.claim_id)}</b><br><small>${esc(claim.claim_date || "Date not recorded")}</small><div class="outcome-entry"><label>Actual settled<input type="number" min="0" step="0.01" data-outcome-settled="${esc(claim.claim_id)}" value="${claim.actual_settled_amount ?? ""}" placeholder="Unreported"></label><label>Confirmed recovery<input type="number" min="0" step="0.01" data-outcome-recovery="${esc(claim.claim_id)}" value="${claim.confirmed_recovery ?? ""}" placeholder="Unreported"></label><label>Dispute status<select data-outcome-dispute="${esc(claim.claim_id)}">${[["not_recorded","Not recorded"],["no_dispute","No dispute"],["drafting","Drafting"],["submitted","Submitted"],["pending","Pending"],["resolved","Resolved"]].map(([v,l]) => `<option value="${v}" ${claim.dispute_status === v ? "selected" : ""}>${l}</option>`).join("")}</select></label><button type="button" class="secondary" data-save-outcome="${esc(claim.claim_id)}">Save outcome</button></div></td><td>${esc(claim.provider || "Provider not recorded")}<br><small>${esc(claim.status.replaceAll("_", " "))}</small></td><td class="num">${inr(claim.claimed_amount)}</td><td class="num">${claim.assessed_payable_amount == null ? "Not available" : inr(claim.assessed_payable_amount)}<br><small>Estimated</small></td><td class="num">${claim.actual_settled_amount == null ? "Not recorded" : inr(claim.actual_settled_amount)}<br><small>${esc(claim.actual_settlement_source)}</small></td><td class="num">${claim.deductions.length ? claim.deductions.map(d => `${esc(d.label)}: ${inr(d.amount)}`).join("<br>") : "None recorded"}</td><td>${esc(claim.dispute_status.replaceAll("_", " "))}${outstanding.length ? `<div class="note">${outstanding.map(esc).join(" · ")}</div>` : ""}</td><td>${claim.simulated_demo_data ? '<span class="pill review">Simulated demo</span>' : '<span class="muted">User claim</span>'}</td></tr>`;
    }).join("") : '<tr><td colspan="8" class="empty">No completed claims are recorded for your account.</td></tr>';
    bars.innerHTML = data.claims.length ? data.claims.map(claim => {
      const max = Math.max(claim.claimed_amount, claim.assessed_payable_amount || 0, claim.actual_settled_amount || 0, 1);
      const row = (label, value, cls, missing = false) => `<div class="reconciliation-line"><span>${esc(label)} ${missing ? '<small>Not recorded</small>' : inr(value)}</span><div class="reconciliation-track"><div class="reconciliation-bar ${cls}" style="width:${missing ? 0 : Math.max(0, Math.min(100, (value / max) * 100))}%"></div></div></div>`;
      return `<article class="reconciliation-claim"><b>${esc(claim.sample_id || claim.claim_id)} · ${esc(claim.provider)}</b>${row("Claimed", claim.claimed_amount, "claimed")}${row("Assessed payable (estimate)", claim.assessed_payable_amount || 0, "assessed", claim.assessed_payable_amount == null)}${row("Actual settled", claim.actual_settled_amount || 0, "settled", claim.actual_settled_amount == null)}<small>Estimated recovery ${inr(claim.estimated_recovery)} · Confirmed recovery ${claim.confirmed_recovery == null ? "Not recorded" : inr(claim.confirmed_recovery)}</small></article>`;
    }).join("") : '<p class="muted">Reconciliation charts will appear after a completed audit.</p>';
  } catch (e) { body.innerHTML = ""; error.textContent = e.message; error.classList.remove("hidden"); }
}

async function saveClaimOutcome(event) {
  const button = event.target.closest("[data-save-outcome]"); if (!button) return;
  const caseId = button.dataset.saveOutcome, selector = `[data-outcome-settled="${CSS.escape(caseId)}"]`, recovery = `[data-outcome-recovery="${CSS.escape(caseId)}"]`, dispute = `[data-outcome-dispute="${CSS.escape(caseId)}"]`;
  const body = { dispute_status: $(dispute).value };
  if ($(selector).value !== "") body.actual_settled_amount = Number($(selector).value);
  if ($(recovery).value !== "") body.confirmed_recovery = Number($(recovery).value);
  button.disabled = true;
  try {
    const response = await apiFetch(`/api/cases/${encodeURIComponent(caseId)}/claim-outcome`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not save claim outcome.");
    await loadClaimDashboard();
  } catch (e) { const error = $("#dashboard-error"); error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; }
}

async function loadReviewerQueue() {
  const target = $("#reviewer-queue"), error = $("#reviewer-error");
  error.classList.add("hidden"); target.innerHTML = '<p class="muted">Loading review queue…</p>';
  try {
    const response = await apiFetch("/api/reviewer/queue"), data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load the reviewer queue.");
    target.innerHTML = data.items.length ? data.items.map(item => `<article class="review-case"><div class="panel-head"><div><h3>${esc(item.sample_bill_id || item.bill_name || item.case_id)} <span class="pill ${item.clean_bill ? "high" : "review"}">${item.clean_bill ? "0 findings" : `${item.finding_count} findings`}</span></h3><small>Case ${esc(item.case_id)} · ${esc(item.bill_name)}</small></div><span class="muted">${esc(item.status)}</span></div>${item.clean_bill ? `<p class="note">${esc(item.message)} This clean bill stays visible for false-positive analysis.</p>` : item.findings.map(f => `<div class="review-finding"><div class="panel-head"><b>${esc(f.title)}</b><span class="pill ${f.confidence === "High" ? "high" : "review"}">${esc(f.confidence)} confidence · ${esc(f.classification.replaceAll("_", " "))}</span></div><small>${esc(f.type)} · Impact: ${inr(f.impact)}</small><pre class="review-evidence">${esc(JSON.stringify(f.evidence, null, 2))}</pre><p class="muted small">${esc(f.disclaimer)}</p>${f.human_outcome ? `<div class="note">Outcome: <b>${esc(f.human_outcome.replaceAll("_", " "))}</b> · ${esc(f.reviewer_explanation)}${f.disagreement ? ' <span class="pill review">Reviewer disagreement recorded</span>' : ""}</div>` : ""}<label class="field">Reviewer explanation<textarea data-review-explanation="${esc(f.finding_id)}" rows="2" maxlength="2000" placeholder="Describe the evidence behind your decision"></textarea></label><div class="actions">${[["confirmed_issue", "Confirm issue"], ["legitimate_charge", "Legitimate charge"], ["inconclusive", "Inconclusive"]].map(([value,label]) => `<button type="button" class="secondary" data-review-case="${esc(item.case_id)}" data-review-finding="${esc(f.finding_id)}" data-review-outcome="${value}">${label}</button>`).join("")}</div></div>`).join("")}</article>`).join("") : '<p class="muted">No completed claim audits are available in this tenant review queue yet.</p>';
  } catch (e) { target.innerHTML = ""; error.textContent = e.message; error.classList.remove("hidden"); }
}

async function submitReviewerDecision(event) {
  const button = event.target.closest("[data-review-outcome]"); if (!button) return;
  const explanation = $(`[data-review-explanation="${CSS.escape(button.dataset.reviewFinding)}"]`).value.trim();
  if (explanation.length < 3) { alert("Enter a short reviewer explanation before recording an outcome."); return; }
  button.disabled = true;
  try {
    const response = await apiFetch(`/api/reviewer/cases/${encodeURIComponent(button.dataset.reviewCase)}/findings/${encodeURIComponent(button.dataset.reviewFinding)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ outcome: button.dataset.reviewOutcome, explanation }) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Could not save the review outcome.");
    await Promise.all([loadReviewerQueue(), loadReviewerEvaluation()]);
  } catch (e) { const error = $("#reviewer-error"); error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; }
}

async function loadReviewerEvaluation() {
  const target = $("#reviewer-evaluation");
  try {
    const response = await apiFetch("/api/reviewer/evaluation"), data = await response.json();
    if (!response.ok) throw new Error("Could not load false-positive analysis.");
    const matrix = data.synthetic_confusion_matrix, outcomes = data.reviewer_outcomes;
    target.innerHTML = `<div class="card"><div class="label">Synthetic test set · ${esc(data.synthetic_dataset_version)}</div><div class="value small">False positives: ${matrix.false_positive} · True negatives: ${matrix.true_negative} · True positives: ${matrix.true_positive} · False negatives: ${matrix.false_negative} · Inconclusive: ${matrix.inconclusive}</div><small>False-positive rate: ${matrix.false_positive_rate == null ? "n/a" : `${(matrix.false_positive_rate * 100).toFixed(1)}%`}</small></div><div class="card"><div class="label">Human review outcomes</div><div class="value small">Reviewed: ${outcomes.reviewed_findings} · Marked legitimate: ${outcomes.false_positive_count} · Reviewer disagreements: ${outcomes.reviewer_disagreements}</div><small>${esc(outcomes.note)}</small></div><p class="muted small">${esc(data.notice)}</p>`;
  } catch (e) { target.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

async function loadRegulatoryReference() {
  const docsTarget = $("#regulatory-documents"), rulesTarget = $("#regulatory-rules"), error = $("#regulatory-error");
  error.classList.add("hidden");
  try {
    const [docResponse, ruleResponse] = await Promise.all([
      apiFetch("/api/regulatory/documents").then(r => r.json()),
      apiFetch(`/api/regulatory/rules/search?q=${encodeURIComponent($("#regulatory-query").value.trim())}`).then(r => r.json()),
    ]);
    if (docResponse.detail || ruleResponse.detail) throw new Error(docResponse.detail || ruleResponse.detail);
    const docs = docResponse.documents, rules = ruleResponse.rules;
    const selectOptions = docs.map(d => `<option value="${esc(d.document_id)}">${esc(d.title)}</option>`).join("");
    $("#regulatory-ingest-form [name=document_id]").innerHTML = selectOptions;
    $("#regulatory-rule-form [name=source_document_id]").innerHTML = selectOptions;
    $("#regulatory-rule-select").innerHTML = '<option value="">Select a rule</option>' + rules.map(r => `<option value="${esc(r.rule_id)}">${esc(r.rule_title)} · ${esc(r.human_review_status.replaceAll("_", " "))}</option>`).join("");
    docsTarget.innerHTML = docs.map(d => `<article class="review-case"><div class="panel-head"><div><h3>${esc(d.title)}</h3><small>${esc(d.circular_number)} · Published ${esc(d.publication_date || "date unavailable")} · Effective ${esc(d.effective_date || "not established")}</small></div><span class="pill ${d.human_review_status === "approved" ? "high" : "review"}">${esc(d.human_review_status.replaceAll("_", " "))}</span></div><p>${esc(d.ingestion_note)}</p><small>SHA-256: ${esc(d.sha256 || "not imported")} · Retrieved ${esc(d.retrieval_date || "not recorded")} · Last currentness review ${esc(d.last_verification_date || "not recorded")} · Reviewed by ${esc(d.reviewed_by || "nobody")}</small><div><a href="${esc(d.official_source_url)}" target="_blank" rel="noopener">Official IRDAI listing</a>${d.sha256 ? ` · <a href="/api/regulatory/documents/${encodeURIComponent(d.document_id)}/pdf" target="_blank" rel="noopener">Original PDF</a>` : ""}</div>${d.sha256 ? `<label class="field"><input type="checkbox" data-doc-currentness="${esc(d.document_id)}"> I checked subsequent amendments for this document</label><label class="field">Review explanation<textarea data-doc-explanation="${esc(d.document_id)}" rows="2" placeholder="Record source and currentness review"></textarea></label><button type="button" class="secondary" data-doc-review="${esc(d.document_id)}">Approve document reference</button><button type="button" class="secondary" data-doc-evidence="${esc(d.document_id)}">Inspect extracted text</button>` : ""}</article>`).join("");
    rulesTarget.innerHTML = rules.length ? rules.map(r => `<article class="review-case"><div class="panel-head"><h3>${esc(r.rule_title)}</h3><span class="pill ${r.human_review_status === "approved" ? "high" : "review"}">${esc(r.human_review_status.replaceAll("_", " "))}${r.enforcement_enabled ? " · enabled" : " · inactive"}</span></div><p>${esc(r.source_citation)}${r.source_page ? ` · page ${esc(r.source_page)}` : " · page unavailable"}${r.source_paragraph ? ` · ${esc(r.source_paragraph)}` : ""}</p><blockquote>${esc(r.source_quote)}</blockquote><small>Effective ${esc(r.effective_date || "unknown")} · Expires ${esc(r.expiry_date || "not recorded")} · ${esc(r.classification.replaceAll("_", " "))}</small><label class="field">Reviewer explanation<textarea data-rule-explanation="${esc(r.rule_id)}" rows="2" placeholder="Record the human review decision"></textarea></label><label><input type="checkbox" data-rule-enable="${esc(r.rule_id)}"> Enable reviewed rule for applicability checks</label><div class="actions"><button type="button" class="secondary" data-rule-review="${esc(r.rule_id)}" data-review-status="approved">Approve</button><button type="button" class="secondary" data-rule-review="${esc(r.rule_id)}" data-review-status="rejected">Reject</button><button type="button" class="secondary" data-rule-evidence="${esc(r.source_document_id)}" data-rule-page="${esc(r.source_page || "")}">Inspect source evidence</button></div></article>`).join("") : '<p class="muted">No regulatory rules are registered. Documents remain metadata-only until an approved PDF is ingested and an exact quotation is reviewed.</p>';
    $("#regulatory-notice").textContent = ruleResponse.notice;
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
}

async function reviewRegulatoryDocument(event) {
  const button = event.target.closest("[data-doc-review]"); if (!button) return;
  const id = button.dataset.docReview, explanation = $(`[data-doc-explanation="${CSS.escape(id)}"]`).value.trim();
  try {
    const response = await apiFetch(`/api/admin/regulatory/documents/${encodeURIComponent(id)}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ human_review_status: "approved", currentness_checked: $(`[data-doc-currentness="${CSS.escape(id)}"]`).checked, reviewer_explanation: explanation }) });
    const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Document review failed.");
    await loadRegulatoryReference();
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

async function inspectRegulatoryEvidence(event) {
  const button = event.target.closest("[data-doc-evidence], [data-rule-evidence]"); if (!button) return;
  const id = button.dataset.docEvidence || button.dataset.ruleEvidence, page = button.dataset.rulePage;
  try {
    const query = page ? `?page=${encodeURIComponent(page)}` : "";
    const response = await apiFetch(`/api/regulatory/documents/${encodeURIComponent(id)}/evidence${query}`), body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Source evidence unavailable.");
    alert(page ? `Page ${body.page}\n\n${body.text}` : `${body.pages} extracted pages · SHA-256 ${body.sha256}`);
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

async function reviewRegulatoryRule(event) {
  const button = event.target.closest("[data-rule-review]"); if (!button) return;
  const id = button.dataset.ruleReview, explanation = $(`[data-rule-explanation="${CSS.escape(id)}"]`).value.trim();
  try {
    const response = await apiFetch(`/api/admin/regulatory/rules/${encodeURIComponent(id)}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ human_review_status: button.dataset.reviewStatus, enforcement_enabled: button.dataset.reviewStatus === "approved" && $(`[data-rule-enable="${CSS.escape(id)}"]`).checked, reviewer_explanation: explanation }) });
    const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Rule review failed.");
    await loadRegulatoryReference();
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

let regulatoryPreviewHash = null;
async function previewRegulatoryDocument() {
  const form = $("#regulatory-ingest-form"), error = $("#regulatory-error");
  regulatoryPreviewHash = null; $("#regulatory-ingest-button").disabled = true; error.classList.add("hidden");
  try {
    const response = await apiFetch("/api/admin/regulatory/documents/preview", { method: "POST", body: new FormData(form) });
    const body = await response.json(); if (!response.ok) throw new Error(body.detail || "PDF validation failed.");
    regulatoryPreviewHash = body.sha256;
    $("#regulatory-pdf-preview").innerHTML = `<article class="review-case"><b>${esc(body.title)}</b><p>${body.page_count} pages · ${body.bytes} bytes · SHA-256 ${esc(body.sha256)}</p><p class="muted">${esc(body.notice)} Provenance: ${esc(body.provenance_status)}.</p>${body.pages.map((page, i) => `<details><summary>Page ${i + 1}</summary><pre class="small">${esc(page || "[No text extracted]")}</pre></details>`).join("")}</article>`;
    $("#regulatory-ingest-button").disabled = false;
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
}

async function ingestRegulatoryDocument(event) {
  event.preventDefault();
  if (!regulatoryPreviewHash) { $("#regulatory-error").textContent = "Validate and preview this file before storing it."; $("#regulatory-error").classList.remove("hidden"); return; }
  try {
    const response = await apiFetch("/api/admin/regulatory/documents/ingest", { method: "POST", body: new FormData(event.currentTarget) });
    const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Document ingestion failed.");
    if (body.sha256 !== regulatoryPreviewHash) throw new Error("Stored file differs from the previewed PDF. It remains unapproved; reload and validate the selected file again.");
    $("#regulatory-admin-result").textContent = `Stored source SHA-256 ${body.sha256}; review is required before use.`;
    regulatoryPreviewHash = null; $("#regulatory-ingest-button").disabled = true; $("#regulatory-pdf-preview").textContent = "Source stored pending administrator verification.";
    await loadRegulatoryReference();
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

async function createRegulatoryCandidate(event) {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const split = name => String(form.get(name) || "").split(",").map(value => value.trim()).filter(Boolean);
  const body = { rule_id: form.get("rule_id"), rule_title: form.get("rule_title"), source_document_id: form.get("source_document_id"), source_citation: form.get("source_citation"), source_page: Number(form.get("source_page")), source_paragraph: form.get("source_paragraph") || null, source_quote: form.get("source_quote"), effective_date: form.get("effective_date") || null, applicability: { insurance_categories: split("insurance_categories"), claim_contexts: split("claim_contexts"), policy_types: split("policy_types") }, human_review_status: "pending_human_review", enforcement_enabled: false };
  try {
    const response = await apiFetch("/api/admin/regulatory/rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || "Could not save candidate rule.");
    $("#regulatory-admin-result").textContent = "Saved as an inactive candidate. Human approval is required.";
    event.currentTarget.reset(); await loadRegulatoryReference();
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

async function evaluateRegulatoryRule(event) {
  event.preventDefault(); const form = new FormData(event.currentTarget), id = $("#regulatory-rule-select").value;
  if (!id) return;
  const body = Object.fromEntries(["insurance_category", "event_date", "claim_context", "policy_type"].map(name => [name, form.get(name) || null]));
  try {
    const response = await apiFetch(`/api/regulatory/rules/${encodeURIComponent(id)}/applicability`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || "Applicability check failed.");
    $("#regulatory-evaluation").innerHTML = `<div class="note"><b>${esc(result.status)}</b> · ${esc(result.assessment_kind.replaceAll("_", " "))}<p>${result.reasons.map(esc).join(" ")}</p><small>${esc(result.source_citation)} · page ${esc(result.source_page || "unavailable")} · ${esc(result.source_document_id)} · SHA-256 ${esc(result.source_sha256 || "unavailable")}</small></div>`;
  } catch (e) { const target = $("#regulatory-error"); target.textContent = e.message; target.classList.remove("hidden"); }
}

async function submitPolicyExplanation(event) {
  event.preventDefault();
  const form = event.currentTarget, error = $("#policy-explainer-error"), result = $("#policy-explainer-result"), button = $("#policy-explainer-submit");
  error.classList.add("hidden"); result.classList.add("hidden"); button.disabled = true; button.textContent = "Finding policy clauses…";
  try {
    const response = await apiFetch("/api/policy-explainer", { method: "POST", body: new FormData(form) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not explain this policy document.");
    const clauses = data.passages.map(p => `<article class="policy-version"><h3>Original clause ${esc(p.clause_id)} · ${esc(p.title)}</h3><blockquote>${esc(p.text)}</blockquote><small>Page ${esc(p.page)} · ${esc(p.source)}</small></article>`).join("");
    result.innerHTML = `<p class="note">${esc(data.notice)}</p>${data.uncertain ? `<div class="verdict fail">Uncertainty: ${esc(data.uncertainty || "The source wording needs confirmation.")}</div>` : ""}<div class="summary"><article class="card"><div class="label">Plain-language explanation · ${esc(data.language.toUpperCase())}</div><div class="value small" lang="${data.language === "hi" ? "hi" : data.language === "kn" ? "kn" : "en"}">${esc(data.explanation)}</div><small>Method: ${esc(data.method.replaceAll("_", " "))}</small></article></div><section class="preclaim-list"><h3>Original policy clauses for comparison</h3>${clauses || '<p class="muted">No matching source clause was found.</p>'}</section>`;
    result.classList.remove("hidden");
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; button.textContent = "Explain policy clause"; }
}

function renderPolicyDocumentTypeInputs() {
  const files = [...$("#policy-history-files").files];
  const options = [["current_policy", "Current policy wording / schedule"], ["renewal", "Renewal schedule / notice"], ["endorsement", "Endorsement"], ["prior_policy", "Prior policy document"], ["portability_certificate", "Portability / continuity certificate"]];
  $("#policy-document-types").innerHTML = files.map((file, i) => `<label class="field">${esc(file.name)}<select data-document-type="${i}" required>${options.map(([value,label]) => `<option value="${value}" ${i === 0 && value === "current_policy" ? "selected" : ""}>${label}</option>`).join("")}</select></label>`).join("");
}

async function submitPolicyHistoryCheck(event) {
  event.preventDefault();
  const form = event.currentTarget, files = [...$("#policy-history-files").files], error = $("#policy-history-error"), result = $("#policy-history-result"), button = $("#policy-history-submit");
  error.classList.add("hidden"); result.classList.add("hidden"); button.disabled = true; button.textContent = "Checking documents…";
  const metadata = { treatment: form.elements.treatment.value, treatment_date: form.elements.treatment_date.value,
    continuity_status: form.elements.continuity_status.value,
    documents: [...document.querySelectorAll("[data-document-type]")].map(select => ({ document_type: select.value })) };
  const payload = new FormData(); payload.append("metadata", JSON.stringify(metadata)); files.forEach(file => payload.append("documents", file));
  try {
    const response = await apiFetch("/api/policy-history/check", { method: "POST", body: payload }); const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check the policy PDFs and treatment date.");
    renderPolicyHistory(body);
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; button.textContent = "Check policy history"; }
}

function renderPolicyHistory(data) {
  const versions = data.policy_versions.map(v => `<article class="policy-version"><h3>${esc(v.document_type.replaceAll("_", " "))}</h3><div><b>${esc(v.filename)}</b></div><div>Version ID: <code>${esc(v.version_id)}</code></div><div>Insurer / plan: ${esc(v.insurer || "Not extracted")} · ${esc(v.plan_name || "Not extracted")}</div><div>Policy number: ${esc(v.policy_number || "Not extracted")}</div><div>Valid dates: ${esc(v.policy_start_date || "Unknown")} to ${esc(v.policy_expiry_date || "Unknown")}</div><div>Renewal date: ${esc(v.renewal_date || "Not found")}</div><div>SHA-256: <code>${esc(v.sha256)}</code></div>${v.endorsements.length ? `<p>Endorsements: ${v.endorsements.map(esc).join(" · ")}</p>` : ""}</article>`).join("");
  const waits = data.waiting_periods.length ? data.waiting_periods.map(w => `<article class="note"><b>${esc(w.condition)} · ${esc(w.duration)} · ${esc(w.status.replaceAll("_", " "))}</b><div>Clause ${esc(w.clause_id)} (${esc(w.version_id)}) · treatment date ${esc(data.treatment_date)} · elapsed from selected start ${w.months_since_current_start == null ? "unknown" : `${esc(w.months_since_current_start)} months`}</div><blockquote>${esc(w.text)}</blockquote></article>`).join("") : '<p class="muted">No relevant waiting period was found in the parsed clauses.</p>';
  const missing = data.missing_historical_documents.map(x => `<li>${esc(x)}</li>`).join("");
  const conflicts = data.conflicts.length ? data.conflicts.map(x => `<div class="verdict fail">${esc(x.message)}</div>`).join("") : '<p class="note">No conflicting identifiers or same-type start dates detected in the uploaded documents.</p>';
  $("#policy-history-result").innerHTML = `<p class="note">${esc(data.notice)}</p><div class="summary"><div class="card"><div class="label">Applicable document version</div><div class="value small">${esc(data.applicable_policy_version || "Unresolved")}</div></div><div class="card"><div class="label">Dates used</div><div class="value small">${esc(data.applicable_policy_dates?.start || "Unknown")} → ${esc(data.applicable_policy_dates?.expiry || "Unknown")}</div></div><div class="card"><div class="label">Continuity input</div><div class="value small">${esc(data.continuity_status.replaceAll("_", " "))}</div></div></div>
    <div class="preclaim-list"><h3>Policy information & document versions</h3><div class="policy-version-list">${versions}</div></div>
    <div class="preclaim-list"><h3>Potentially relevant waiting periods</h3>${waits}</div>
    <div class="preclaim-list"><h3>Missing historical documents</h3>${missing ? `<ul>${missing}</ul>` : '<p class="note">No continuity documents are missing from the supplied set.</p>'}</div>
    <div class="preclaim-list"><h3>Document conflicts</h3>${conflicts}</div>
    <div class="preclaim-list"><h3>Review notes</h3>${data.warnings.map(w => `<div class="note">${esc(w)}</div>`).join("")}</div>`;
  result.classList.remove("hidden");
}

async function submitPreAuthorization(event) {
  event.preventDefault();
  const form = event.currentTarget, fields = new FormData(form), error = $("#preauth-error"), result = $("#preauth-result"), button = $("#preauth-submit");
  error.classList.add("hidden"); result.classList.add("hidden"); button.disabled = true; button.textContent = "Preparing…";
  const intake = Object.fromEntries(["patient_name", "treatment", "estimated_cost", "cost_notes", "hospital_name", "hospital_address", "hospital_contact", "network_status", "insurer_name", "tpa_name", "policy_number", "plan_name", "policy_start", "policy_end"].map(key => [key, fields.get(key)]));
  intake.estimated_cost = Number(intake.estimated_cost);
  intake.patient_consent = fields.get("patient_consent") === "on";
  intake.document_types = fields.getAll("document_types");
  for (const key of ["policy_start", "policy_end"]) if (!intake[key]) intake[key] = null;
  const payload = new FormData(); payload.append("intake", JSON.stringify(intake));
  const policy = fields.get("policy"); if (policy?.size) payload.append("policy", policy);
  for (const file of fields.getAll("documents")) if (file.size) payload.append("documents", file);
  try {
    const response = await apiFetch("/api/preauthorizations", { method: "POST", body: payload });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check consent and request details.");
    state.preauthId = body.preauthorization_id; renderPreAuthorization(body);
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; button.textContent = "Prepare request summary"; }
}

function renderPreAuthorization(data) {
  const warnings = data.warnings.length ? data.warnings.map(w => `<div class="note"><b>${esc(w.kind.replaceAll("_", " "))}</b>: ${esc(w.message)}${w.status ? ` (${esc(w.status)})` : ""}</div>`).join("") : '<p class="note">No additional warning detected from supplied information.</p>';
  const checklist = data.document_checklist.map(d => `<li class="${d.provided ? "provided" : d.required ? "missing" : ""}">${d.provided ? "✓" : d.required ? "!" : "○"} ${esc(d.label)}${d.required ? " (required)" : " (recommended)"}</li>`).join("");
  const evidence = data.coverage_evidence.length ? `<div class="preclaim-list"><h3>Relevant policy wording</h3>${data.coverage_evidence.map(e => `<blockquote><b>${esc(e.clause_id)} · ${esc(e.title)}</b><p>${esc(e.text)}</p><small>Page ${esc(e.page)}</small></blockquote>`).join("")}</div>` : "";
  $("#preauth-result").innerHTML = `<div class="verdict ${data.documents_complete ? "good" : "money"}">${data.documents_complete ? "Checklist complete" : `Preparation incomplete · ${data.missing_required_documents.map(esc).join(", ") || "confirm policy details"}`}</div>
    <div class="preclaim-list"><h3>Document checklist</h3><ul class="preauth-checklist">${checklist}</ul></div>
    <div class="preclaim-list"><h3>Coverage and information warnings</h3>${warnings}${evidence}</div>
    <div class="preclaim-list"><div class="panel-head"><h3>Insurer-reviewable summary</h3><button type="button" class="secondary" id="export-preauth-summary">Export summary (.txt)</button></div><pre id="preauth-summary" class="letter">${esc(data.summary)}</pre></div>
    <div class="preclaim-list"><h3>Manually recorded authorization status</h3><p class="muted small">Status is entered by you and is not verified by ClaimShield.</p><div class="actions"><select id="preauth-status"><option value="pending">Pending</option><option value="approved">Approved</option><option value="partially_approved">Partially approved</option><option value="denied">Denied</option><option value="more_information_requested">More information requested</option></select><button type="button" class="secondary" id="save-preauth-status">Record status</button><span id="preauth-status-label">${esc(data.authorization_status)}</span></div></div>
    <p class="note">${esc(data.approval_disclaimer)}</p>`;
  $("#export-preauth-summary").addEventListener("click", () => {
    const blob = new Blob([data.summary], { type: "text/plain;charset=utf-8" }); const link = document.createElement("a");
    link.href = URL.createObjectURL(blob); link.download = `preauthorization-${data.preauthorization_id}.txt`; link.click(); URL.revokeObjectURL(link.href);
  });
  $("#save-preauth-status").addEventListener("click", async () => {
    const button = $("#save-preauth-status"); button.disabled = true;
    try { const response = await apiFetch(`/api/preauthorizations/${state.preauthId}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: $("#preauth-status").value }) });
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Could not save status.");
      $("#preauth-status-label").textContent = `${body.authorization_status} · manually entered, not verified`;
    } catch (e) { $("#preauth-error").textContent = e.message; $("#preauth-error").classList.remove("hidden"); }
    finally { button.disabled = false; }
  });
  $("#preauth-result").classList.remove("hidden");
}

function addWhatIfScenario() {
  if ($("#scenario-inputs").children.length >= 5) return;
  const index = $("#scenario-inputs").children.length + 1;
  const row = document.createElement("div"); row.className = "scenario-input";
  row.innerHTML = `<label class="field">Scenario name<input data-key="name" value="Option ${index}" maxlength="80" required></label>
    <label class="field">Estimated cost (₹)<input data-key="estimated_total" type="number" min="0.01" step="0.01" placeholder="Use current" ></label>
    <label class="field">Room category<select data-key="room_category"><option value="">Use current</option><option>General</option><option>Shared</option><option>Semi-private</option><option>Private</option><option>ICU</option></select></label>
    <label class="field">Room rate/day (₹)<input data-key="room_rate_per_day" type="number" min="0" step="0.01" placeholder="Use current"></label>
    <label class="field">Hypothetical sublimit (₹)<input data-key="hypothetical_sub_limit" type="number" min="0" step="0.01" placeholder="Keep real rule"></label>
    <label class="field">Hypothetical network co-pay (%)<input data-key="hypothetical_copay_network_pct" type="number" min="0" max="100" step="0.1" placeholder="Keep real rule"></label>
    <label class="field">Hypothetical non-network co-pay (%)<input data-key="hypothetical_copay_non_network_pct" type="number" min="0" max="100" step="0.1" placeholder="Keep real rule"></label>
    <button type="button" class="secondary remove-scenario">Remove option</button>`;
  row.querySelector(".remove-scenario").addEventListener("click", () => row.remove());
  $("#scenario-inputs").appendChild(row);
}

async function submitWhatIf(event) {
  event.preventDefault();
  const form = event.currentTarget, data = new FormData(form), error = $("#what-if-error"), result = $("#what-if-result"), button = $("#what-if-submit");
  error.classList.add("hidden"); result.classList.add("hidden"); button.disabled = true; button.textContent = "Comparing…";
  const numeric = (value) => value === "" || value == null ? null : Number(value);
  const base = { treatment: data.get("treatment"), estimated_total: Number(data.get("estimated_total")), room_category: data.get("room_category"), room_rate_per_day: numeric(data.get("room_rate_per_day")), hospitalization_date: data.get("hospitalization_date"), nights: Number(data.get("nights")), network_hospital: data.get("network_hospital") === "true" };
  const scenarios = [...document.querySelectorAll(".scenario-input")].map(row => {
    const v = Object.fromEntries([...row.querySelectorAll("[data-key]")].map(input => [input.dataset.key, input.value]));
    return { name: v.name.trim(), estimated_total: numeric(v.estimated_total), room_category: v.room_category || null, room_rate_per_day: numeric(v.room_rate_per_day), hypothetical_sub_limit: numeric(v.hypothetical_sub_limit), hypothetical_copay_network_pct: numeric(v.hypothetical_copay_network_pct), hypothetical_copay_non_network_pct: numeric(v.hypothetical_copay_non_network_pct) };
  });
  const payload = new FormData(); payload.append("policy", data.get("policy")); payload.append("base", JSON.stringify(base)); payload.append("scenarios", JSON.stringify(scenarios));
  try {
    const response = await apiFetch("/api/what-if", { method: "POST", body: payload }); const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check the policy and scenario values.");
    renderWhatIf(body);
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
  finally { button.disabled = false; button.textContent = "Compare scenarios"; }
}

function renderWhatIf(data) {
  const result = $("#what-if-result");
  const format = value => value == null ? "Unavailable" : inr(value);
  const cards = data.scenarios.map((scenario, index) => `<article class="scenario-card"><h3>${esc(scenario.name)} ${scenario.hypothetical ? '<span class="pill review">Hypothetical</span>' : '<span class="pill high">Extracted policy</span>'}</h3>
    <div class="payment-pair"><div><small>Insurer estimate</small><b>${format(scenario.estimated_insurer_payment)}</b></div><div><small>Patient estimate</small><b>${format(scenario.estimated_patient_payment)}</b></div></div>
    ${scenario.hypothetical ? `<p class="small">Change vs current: insurer ${format(scenario.differences.insurer_payment)} · patient ${format(scenario.differences.patient_payment)}</p>` : ""}
    <details><summary>Itemized estimate</summary><dl>${Object.entries(scenario.breakdown).map(([k,v]) => `<dt>${esc(k.replaceAll("_", " "))}</dt><dd>${format(v)}</dd>`).join("")}</dl>${scenario.overridden_policy_rules?.length ? `<p>Hypothetical rules: ${scenario.overridden_policy_rules.map(esc).join(", ")}</p>` : ""}</details></article>`).join("");
  result.innerHTML = `<p class="note">${esc(data.notice)}</p><div class="scenario-cards">${cards}</div>
    <div class="preclaim-list"><div class="panel-head"><h3>Payment breakdown comparison</h3><div class="tabs"><button type="button" class="active" data-metric="estimated_insurer_payment">Insurer</button><button type="button" data-metric="estimated_patient_payment">Patient</button></div></div><div id="what-if-chart" class="scenario-chart" role="img" aria-label="Scenario payment comparison"></div></div>
    <div class="preclaim-list"><h3>Real policy rules kept fixed</h3><ul>${Object.entries(data.fixed_policy_values).map(([k,v]) => `<li>${esc(k.replaceAll("_", " "))}: ${esc(v ?? "Not found in supplied policy")}</li>`).join("")}</ul><p class="muted small">Scenario overrides are limited to sublimit and co-payment. Room limits and the other listed rules remain tied to the uploaded policy.</p><pre class="letter">${esc(JSON.stringify(data.real_policy_terms, null, 2))}</pre></div>`;
  const draw = metric => {
    const vals = data.scenarios.map(x => x[metric]); const max = Math.max(1, ...vals.filter(v => v != null));
    $("#what-if-chart").innerHTML = data.scenarios.map(x => `<div class="chart-row"><span>${esc(x.name)}</span><div class="bar-track"><div class="bar-fill" style="width:${x[metric] == null ? 0 : Math.max(2, x[metric] / max * 100)}%"></div></div><b>${format(x[metric])}</b></div>`).join("");
    result.querySelectorAll("[data-metric]").forEach(b => b.classList.toggle("active", b.dataset.metric === metric));
  };
  result.querySelectorAll("[data-metric]").forEach(b => b.addEventListener("click", () => draw(b.dataset.metric)));
  draw("estimated_insurer_payment"); result.classList.remove("hidden");
}

function addPreclaimCostLine() {
  const row = document.createElement("div");
  row.className = "cost-line";
  row.innerHTML = `<input aria-label="Expense description" placeholder="Expense description" maxlength="160">
    <select aria-label="Expense category"><option value="OTHER">Other</option><option value="ROOM">Room</option><option value="ICU">ICU</option><option value="PROCEDURE">Procedure</option><option value="PHARMACY">Pharmacy</option><option value="CONSUMABLE">Consumable</option><option value="NON_MEDICAL">Non-medical</option><option value="PERSONAL">Personal item</option><option value="LAB">Laboratory</option><option value="RADIOLOGY">Radiology</option><option value="THERAPY">Therapy</option></select>
    <input aria-label="Estimated expense amount" type="number" min="0.01" step="0.01" placeholder="Amount (₹)">
    <button type="button" class="secondary remove-cost-line" aria-label="Remove expense">Remove</button>`;
  row.querySelector(".remove-cost-line").addEventListener("click", () => row.remove());
  $("#preclaim-cost-lines").appendChild(row);
}

async function submitPreclaim(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const costItems = [...document.querySelectorAll("#preclaim-cost-lines .cost-line")].map((row) => {
    const [description, category, amount] = row.querySelectorAll("input, select");
    return { description: description.value.trim(), category: category.value, amount: Number(amount.value) };
  }).filter((item) => item.description && item.amount > 0);
  if (costItems.length) form.append("cost_items", JSON.stringify(costItems));
  const network = form.get("network_hospital");
  if (network === "") form.delete("network_hospital");
  for (const key of ["patient_age", "room_rate_per_day", "deductible"]) if (form.get(key) === "") form.delete(key);
  const button = $("#preclaim-submit");
  const error = $("#preclaim-error");
  const result = $("#preclaim-result");
  button.disabled = true;
  button.textContent = "Calculating…";
  error.classList.add("hidden");
  result.classList.add("hidden");
  try {
    const response = await apiFetch("/api/preclaim", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check the policy PDF and assessment inputs.");
    renderPreclaim(body);
  } catch (e) {
    error.textContent = e.message || "The assessment could not be completed.";
    error.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Estimate coverage";
  }
}

function renderPreclaim(data) {
  const result = $("#preclaim-result");
  const b = data.breakdown;
  const rows = [
    ["Estimated treatment and hospital costs", b.estimated_total],
    ["Estimated non-payable costs", b.excluded_estimate],
    ["Room-rent excess", b.room_excess],
    ["Eligible estimate before co-payment", b.after_sublimit],
    ["Co-payment", b.copay],
    ["Deductible", b.deductible],
    ["Estimated insurer payment", data.estimated_insurer_payment],
    ["Estimated patient payment", data.estimated_patient_payment],
  ].filter(([, value]) => value !== null && value !== undefined);
  const items = (title, values) => values.length ? `<div class="preclaim-list"><h3>${title}</h3><ul>${values.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  const evidence = data.policy_evidence.length ? `<div class="preclaim-list"><h3>Relevant policy evidence</h3>${data.policy_evidence.map((e) => `<blockquote><b>Clause ${esc(e.clause_id)} · ${esc(e.title)}</b><p>${esc(e.quote)}</p><small>${esc(e.source)}${e.page ? ` · page ${esc(e.page)}` : ""}</small></blockquote>`).join("")}</div>` : '<p class="muted">No relevant policy clauses were found in the uploaded policy.</p>';
  result.innerHTML = `<div class="verdict money">Estimated insurer payment: <b>${inr(data.estimated_insurer_payment)}</b> · Estimated patient payment: <b>${inr(data.estimated_patient_payment)}</b></div>
    <p class="note">Estimate only. This does not guarantee actual claim approval or settlement.</p>
    <div class="scroll"><table class="calc"><tbody>${rows.map(([label, value]) => `<tr><td>${esc(label)}</td><td class="num">${inr(value)}</td></tr>`).join("")}</tbody></table></div>
    ${items("Rules applied", data.applied_rules.map((r) => `${r.rule.replaceAll("_", " ")}: ${Object.entries(r).filter(([k]) => k !== "rule").map(([k, v]) => `${k.replaceAll("_", " ")} ${typeof v === "number" ? inr(v) : v}`).join(", ")}`))}
    ${items("Inputs needing confirmation", data.missing_inputs)}${items("Uncertainty", data.uncertainty)}${evidence}`;
  result.classList.remove("hidden");
}

async function submitRejectionAnalysis(event) {
  event.preventDefault();
  const button = $("#rejection-submit");
  const error = $("#rejection-error");
  const result = $("#rejection-result");
  button.disabled = true;
  button.textContent = "Analyzing…";
  error.classList.add("hidden");
  result.classList.add("hidden");
  try {
    const response = await apiFetch("/api/rejection-analysis", { method: "POST", body: new FormData(event.currentTarget) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check both PDF files and try again.");
    renderRejectionAnalysis(body);
  } catch (e) {
    error.textContent = e.message || "The rejection analysis could not be completed.";
    error.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Analyze rejection";
  }
}

function renderRejectionAnalysis(data) {
  const cards = data.reasons.map((r) => {
    const tone = r.classification.startsWith("Potentially") ? "review" : r.classification.startsWith("Supported") ? "high" : "unknown";
    const evidence = r.policy_evidence.length ? r.policy_evidence.map((e) => `<blockquote><b>Clause ${esc(e.clause_id)} · ${esc(e.title)}</b><p>${esc(e.quote)}</p><small>${esc(e.source)}${e.page ? ` · page ${esc(e.page)}` : ""}</small></blockquote>`).join("") : '<p class="muted small">No relevant policy passage was found.</p>';
    return `<article class="reason-card"><div class="reason-head"><h3>${r.reason ? `Reason ${esc(r.id)}: ${esc(r.reason)}` : "No rejection reason found"}</h3><span class="pill ${tone}">${esc(r.classification)}</span></div><p>${esc(r.explanation)}</p><div class="preclaim-list"><h3>Policy evidence</h3>${evidence}</div></article>`;
  }).join("");
  const list = (title, values) => values.length ? `<div class="preclaim-list"><h3>${title}</h3><ul>${values.map((v) => `<li>${esc(v)}</li>`).join("")}</ul></div>` : "";
  const node = `
    <p class="note">${esc(data.disclaimer)}</p>
    ${cards}
    ${list("Document references in the notice", data.document_references || [])}
    ${list("Documents the rejection notice asks you to provide", data.requested_documents)}
    ${list("Documents or clarification to confirm", data.additional_documents_to_confirm)}
    <div class="preclaim-list"><div class="panel-head"><h3>Appeal or clarification draft · review before sending</h3><button type="button" class="secondary" id="copy-appeal">Copy draft</button></div><textarea id="appeal-draft" class="appeal-draft" rows="18">${esc(data.appeal_draft)}</textarea></div>`;
  $("#rejection-result").innerHTML = node;
  $("#copy-appeal").addEventListener("click", () => navigator.clipboard.writeText($("#appeal-draft").value).then(() => {
    const button = $("#copy-appeal"); button.textContent = "Copied"; setTimeout(() => (button.textContent = "Copy draft"), 1200);
  }));
  $("#rejection-result").classList.remove("hidden");
}

async function submitDocumentCheck(event) {
  event.preventDefault();
  const button = $("#document-check-submit");
  const error = $("#document-check-error");
  const result = $("#document-check-result");
  button.disabled = true;
  button.textContent = "Checking…";
  error.classList.add("hidden");
  result.classList.add("hidden");
  try {
    const response = await apiFetch("/api/document-check", { method: "POST", body: new FormData(event.currentTarget) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check the uploaded PDFs and try again.");
    renderDocumentCheck(body);
  } catch (e) {
    error.textContent = e.message || "The document checklist could not be completed.";
    error.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Check documents";
  }
}

function renderDocumentCheck(data) {
  const statusLabel = (status) => ({ complete: "Complete", missing: "Missing", uncertain: "Uncertain", detected: "Detected", duplicate: "Duplicate" }[status] || status);
  const checklistRows = data.checklist.map((item) => `<tr><td>${esc(item.label)}<span class="type">${esc(item.requirement_kind)}</span></td><td><span class="check-status ${esc(item.status)}">${statusLabel(item.status)}</span></td><td>${esc(item.source)}</td><td>${esc(item.note)}${item.fields_not_detected?.length ? `<br><b>Not detected:</b> ${item.fields_not_detected.map(esc).join(", ")}` : ""}</td></tr>`).join("");
  const docs = data.documents.map((doc) => `<article class="reason-card"><div class="reason-head"><div><h3>${esc(doc.type_label)}</h3><p class="muted small">${esc(doc.filename)} · ${esc(doc.text_characters)} readable characters</p></div><span class="check-status ${esc(doc.status)}">${statusLabel(doc.status)}</span></div><p>${esc(doc.reason)}</p>${doc.fields_detected.length ? `<p class="small"><b>Detected fields:</b> ${doc.fields_detected.map(esc).join(", ")}</p>` : ""}${doc.fields_not_detected.length ? `<p class="small"><b>Not detected; verify the original:</b> ${doc.fields_not_detected.map(esc).join(", ")}</p>` : ""}${doc.evidence_excerpt ? `<blockquote>${esc(doc.evidence_excerpt)}</blockquote>` : ""}</article>`).join("");
  $("#document-check-result").innerHTML = `<p class="note">${esc(data.generic_recommendations_notice)}</p>
    <div class="summary">${Object.entries(data.summary).map(([k, v]) => `<div class="card"><div class="label">${esc(k)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    <div class="preclaim-list"><div class="panel-head"><h3>Checklist</h3><button type="button" class="secondary" id="export-checklist">Export checklist (CSV)</button></div><div class="scroll"><table><thead><tr><th>Requirement</th><th>Status</th><th>Source</th><th>Notes</th></tr></thead><tbody>${checklistRows}</tbody></table></div></div>
    <div class="preclaim-list"><h3>Documents detected</h3>${docs || '<p class="muted">No documents uploaded.</p>'}</div>`;
  $("#export-checklist").addEventListener("click", () => {
    const blob = new Blob([data.export_csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "claimshield-document-checklist.csv"; link.click(); URL.revokeObjectURL(link.href);
  });
  $("#document-check-result").classList.remove("hidden");
}

function renderStack(stack) {
  const cloud = /Document AI|BigQuery|Vertex|Gemini|Cloud Storage/;
  $("#stack").innerHTML = Object.entries(stack)
    .map(([k, v]) => `<span class="${cloud.test(v) && !/local/.test(v) ? "cloud" : ""}" title="${esc(k)}">${esc(v)}</span>`)
    .join("");
}

function renderSamples(cat) {
  const box = $("#samples");
  box.innerHTML = "";
  for (const b of Object.values(cat.bills)) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "sample";
    card.dataset.id = b.id;
    card.innerHTML = `
      <b>${esc(b.hospital)}</b>
      <div class="meta">${esc(b.procedure)}</div>
      <div class="meta">${esc(b.patient)} · ${esc(b.policy_plan)} · billed ${inr(b.gross)}</div>
      <div class="links">${b.clean ? '<span class="tag">clean bill</span>' : ""}
        <a href="${esc(b.url)}" target="_blank" rel="noopener">view bill</a>
        <a href="${esc(cat.policies[b.policy].url)}" target="_blank" rel="noopener">view policy</a></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.tagName === "A") return;
      selectSample(b.id);
    });
    box.appendChild(card);
  }
}

function selectSample(id) {
  state.sample = state.sample === id ? null : id;
  state.billFile = state.policyFile = null;
  state.billFiles = []; state.policyFiles = [];
  for (const k of ["bill", "policy"]) {
    $(`#file-${k}`).value = "";
    $(`#drop-${k}`).classList.remove("ready");
  }
  $("#hint-bill").textContent = "Itemised final bill with the insurer settlement";
  $("#hint-policy").textContent = "Policy wording with schedule and clauses";
  document.querySelectorAll(".sample").forEach((el) => el.classList.toggle("selected", el.dataset.id === state.sample));
  updateReady();
}

function wireDrop(kind) {
  const drop = $(`#drop-${kind}`);
  const input = $(`#file-${kind}`);
  const set = (incoming) => {
    const files = [...incoming];
    const error = $('#upload-error');
    error.classList.add('hidden');
    if (files.length > 10 || files.some(f => f.size > 15 * 1024 * 1024 || !/\.pdf$/i.test(f.name))) {
      error.textContent = 'Choose up to 10 PDF files per group, each no larger than 15 MB.';
      error.classList.remove('hidden');
      input.value = '';
      state[`${kind}Files`] = []; state[`${kind}File`] = null;
      drop.classList.remove('ready');
      $(`#hint-${kind}`).textContent = 'Please choose your PDFs again.';
      $(`#main-${kind}`).innerHTML = '<option value="0">Choose files first</option>';
      updateReady(); return;
    }
    state[`${kind}Files`] = files;
    state[`${kind}File`] = files[0] || null;
    state.sample = null;
    document.querySelectorAll(".sample").forEach((el) => el.classList.remove("selected"));
    drop.classList.toggle("ready", files.length > 0);
    $(`#hint-${kind}`).textContent = files.map(file => `${file.name} (${Math.ceil(file.size / 1024)} KB)`).join(', ') || 'Choose your PDFs';
    $(`#main-${kind}`).innerHTML = files.map((file, index) => `<option value="${index}">${esc(file.name)}</option>`).join('');
    updateReady();
  };
  input.addEventListener("change", () => set(input.files));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("drag"); set(e.dataTransfer.files); });
}

function updateReady() {
  const ready = Boolean(state.sample || (state.billFile && state.policyFile));
  $("#audit").disabled = !ready;
  const s = state.sample && state.samples.bills[state.sample];
  $("#selection").textContent = s
    ? `Sample: ${s.hospital} - ${s.procedure} with ${s.policy_plan}`
    : state.billFile && state.policyFile ? 'Ready. Check your main bill and policy selections before continuing.' : 'Add both document groups to begin.';
}

// ---------------------------------------------------------------- run

async function startAudit() {
  const form = new FormData();
  if (state.sample) form.append("sample_bill", state.sample);
  else {
    if ([...state.billFiles, ...state.policyFiles].reduce((sum, file) => sum + file.size, 0) > 50 * 1024 * 1024) {
      $('#upload-error').textContent = 'Choose at most 50 MB of documents in total.';
      $('#upload-error').classList.remove('hidden'); return;
    }
    state.billFiles.forEach(file => form.append('medical_documents', file));
    state.policyFiles.forEach(file => form.append('insurance_documents', file));
    form.append('main_bill', $('#main-bill').value);
    form.append('main_policy', $('#main-policy').value);
    form.append('claim_type', $('#journey-claim-type').value);
  }
  $("#audit").disabled = true;
  $("#audit").textContent = "Starting…";
  let res;
  try {
    res = await apiFetch(state.sample ? '/api/audit' : '/api/claim-review', { method: "POST", body: form });
  } catch (error) {
    alert(error.message || "Could not reach the audit service. Please retry.");
    updateReady();
    return;
  } finally {
    $("#audit").textContent = "Check my claim";
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "Could not start the audit.");
    updateReady();
    return;
  }
  const { case_id } = await res.json();
  history.replaceState(null, "", `?case=${case_id}`);
  showRun(case_id);
}

function showRun(case_id) {
  state.source?.close(); clearTimeout(state.poll);
  state.caseId = case_id;
  $("#start").classList.add("hidden");
  $("#results").classList.add("hidden");
  $("#run").classList.remove("hidden");
  $("#case-id").textContent = case_id;
  $("#log").innerHTML = "";
  document.querySelectorAll(".agent").forEach((el) => (el.className = "agent"));
  streamTrace(case_id);
  const poll = async () => {
    if (state.caseId !== case_id) return;
    try {
      const response = await apiFetch(`/api/cases/${case_id}`);
      if (!response.ok) throw new Error('This claim is unavailable.');
      const current = await response.json();
      if (current.status !== 'running') { state.source?.close(); await loadResults(case_id); return; }
      $('#review-progress').textContent = current.processing_stage === 'queued' ? 'Your review is safely queued. Keep this page link to return later.' : 'Your documents are being reviewed. Results will appear here automatically.';
      state.poll = setTimeout(poll, 2500);
    } catch (error) { $('#review-progress').textContent = error.message; }
  };
  state.poll = setTimeout(poll, 1500);
}

function streamTrace(caseId) {
  const src = new EventSource(`/api/cases/${caseId}/events`);
  state.source = src;
  src.onmessage = (msg) => onEvent(JSON.parse(msg.data));
  src.addEventListener("end", async () => {
    src.close();
    try { await loadResults(caseId); } catch (error) { alert(error.message); }
  });
  src.addEventListener("auth_expired", () => {
    src.close();
    alert("Your session has expired. Reload this page to sign in through Google.");
  });
  src.onerror = () => {
    // EventSource retries on its own. If the case has finished meanwhile, just load it.
    apiFetch(`/api/cases/${caseId}`).then(async (r) => {
      if (!r.ok) throw new Error("This case is unavailable or you do not have access.");
      const c = await r.json();
      if (c.status && c.status !== "running") { src.close(); await loadResults(caseId); }
    }).catch((error) => { src.close(); alert(error.message); });
  };
}

function onEvent(e) {
  const node = document.querySelector(`.agent[data-agent="${e.agent}"]`);
  if (node) {
    if (e.kind === "start") node.classList.add("running");
    if (e.kind === "result") { node.classList.remove("running"); node.classList.add("done"); }
    if (e.kind === "error") node.classList.add("error");
  }
  if (e.agent === "orchestrator" && e.kind === "error") document.querySelectorAll(".agent.running").forEach((n) => n.classList.replace("running", "error"));
  const li = document.createElement("li");
  li.className = `k-${e.kind}`;
  li.innerHTML = `<span class="t">${e.t.toFixed(2)}s</span><span class="a">${esc(e.agent)}</span><span class="m">${esc(e.message)}</span>`;
  const log = $("#log");
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- results

async function loadResults(caseId) {
  clearTimeout(state.poll);
  const c = await apiFetch(`/api/cases/${caseId}`).then((r) => r.json());
  $("#results").classList.remove("hidden");
  renderClaimReview(c.claim_review);
  renderHealthcareAudit(c.healthcare_audit);
  $('#review-progress').textContent = c.status === 'complete' ? 'Review complete. Your results are below.' : 'Review could not finish. See details below.';
  if (c.status !== "complete") {
    $("#verdict").className = "verdict fail";
    $("#verdict").textContent = `The audit could not finish: ${c.error || "unknown error"}. Check that both files are an itemised hospital bill and a policy wording.`;
    for (const id of ["#summary", "#notes"]) $(id).innerHTML = "";
    for (const id of ["#findings-panel", "#letters-panel"]) $(id).classList.add("hidden");
    document.querySelectorAll("#results .grid2").forEach((g) => g.classList.add("hidden"));
    return;
  }
  for (const id of ["#findings-panel", "#letters-panel"]) $(id).classList.remove("hidden");
  document.querySelectorAll("#results .grid2").forEach((g) => g.classList.remove("hidden"));
  renderSummary(c);
  renderFindings(c);
  renderBenchmarks(c);
  renderBill(c);
  renderCalc(c);
  renderLetters(c);
  await loadClaimTimeline(caseId);
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderHealthcareAudit(report) {
  const panel = $('#healthcare-audit-panel');
  const available = Boolean(report && report.finding_counts);
  panel.classList.toggle('hidden', !available);
  if (!available) {
    $('#healthcare-audit-summary').innerHTML = '';
    $('#healthcare-audit-warnings').innerHTML = '';
    $('#healthcare-audit-findings').innerHTML = '';
    return;
  }
  const amount = value => typeof value === 'number' && Number.isFinite(value) ? inr(value) : 'Not determined';
  const counts = report.finding_counts;
  $('#healthcare-audit-summary').innerHTML = [
    ['Billed total', amount(report.total_billed)], ['Potential adjustment scenario', amount(report.potential_discrepancy)],
    ['Benchmark variance · separate', amount(report.benchmark_variance)],
    ['Clear checks', counts.clear ?? 0], ['Flagged checks', counts.flagged ?? 0], ['Needs review', counts.needs_review ?? 0],
  ].map(([label, value]) => `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
  $('#healthcare-audit-warnings').innerHTML = (report.warnings || []).map(warning => `<p class="note">${esc(warning)}</p>`).join('');
  $('#healthcare-audit-findings').innerHTML = (report.findings || []).map(finding => {
    const source = finding.evidence?.source;
    const status = String(finding.status || 'needs_review').replaceAll('_', ' ');
    const sourceText = source ? `<b>${esc(source.source_name)}</b><br>Source status: ${esc(source.verification_status || 'unverified')}<br>Page: ${esc(source.page_number ?? 'Unavailable')} · Effective: ${esc(source.effective_date ?? 'Unavailable')}<br>Version: ${esc(source.version ?? 'Unavailable')}${source.source_url ? `<br>${esc(source.source_url)}` : ''}${source.source_quote || finding.policy?.quote ? `<blockquote>${esc(source.source_quote || finding.policy.quote)}</blockquote>` : ''}` : 'Source unavailable; no verified reference is inferred.';
    return `<tr><td><b>${esc(finding.title || finding.rule_name)}</b><br>${esc(finding.type)}<br><span class="pill ${finding.status === 'clear' ? 'high' : 'review'}">${esc(status)}</span></td><td>${esc(finding.severity || 'warning')}</td><td class="num">${esc(amount(finding.billed_amount))}</td><td class="num">${esc(amount(finding.reference_amount))}</td><td class="num">${esc(amount(finding.status === 'needs_review' ? null : finding.potential_difference))}</td><td>${esc(finding.confidence)}${typeof finding.confidence_score === 'number' ? `<br>${esc(finding.confidence_score.toFixed(2))} · check confidence` : ''}</td><td>${esc(finding.explanation)}<details><summary>Source and calculation</summary><div>${sourceText}</div><pre class="small">${esc(JSON.stringify(finding.evidence || {}, null, 2))}</pre></details></td></tr>`;
  }).join('') || '<tr><td colspan="7">No item-level checks are available. Review the warnings above.</td></tr>';
}

function openClaimTool(id) {
  const target = document.getElementById(id);
  if (!target) return;
  // Reuse the files already selected in this tab; do not make the user upload twice.
  const mainPolicy = state.policyFiles[Number($('#main-policy').value)];
  if (typeof DataTransfer !== 'undefined') {
    const setFiles = (input, files) => {
      if (!input || input.files.length || !files.length) return;
      const transfer = new DataTransfer();
      files.forEach(file => transfer.items.add(file));
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    };
    if (mainPolicy) setFiles(target.querySelector('input[type=file][name=policy]'), [mainPolicy]);
    if (id === 'document-check-panel') setFiles(target.querySelector('input[type=file]'), [...state.billFiles, ...state.policyFiles]);
    if (id === 'policy-history-panel') setFiles(target.querySelector('input[type=file]'), state.policyFiles);
  }
  const disclosure = target.closest('details');
  if (disclosure) disclosure.open = true;
  target.scrollIntoView({behavior: 'smooth', block: 'start'});
}

function renderClaimReview(review) {
  const target = $('#claim-review-result');
  target.classList.toggle('hidden', !review);
  if (!review) { target.innerHTML = ''; return; }
  const check = review.document_check;
  const ai = review.ai_clause_review || {mode: 'unavailable', proposals: [], notice: 'AI clause comparison unavailable.'};
  const aiLabels = {potentially_eligible: 'Potentially eligible under this clause', potentially_ineligible: 'Potentially excluded under this clause', needs_verification: 'More evidence needed'};
  target.innerHTML = `<p class="eyebrow">YOUR CLAIM REVIEW</p><h2>${esc(review.headline)}</h2>
    <p class="note">${esc(review.notice)}</p>
    <div class="summary"><div class="card"><div class="label">Eligibility</div><b>Needs verification</b><p>Unresolved checks are not a rejection.</p></div><div class="card"><div class="label">Policy clauses extracted</div><b>${review.clause_count}</b><p>Each clause is listed below.</p></div><div class="card"><div class="label">Document checklist</div><b>${check.summary.missing} missing · ${check.summary.uncertain} to check</b><p>Generic recommendations, unless a verified insurer source is shown.</p></div></div>
    <h3>Start with these actions</h3><ol class="next-actions">${review.next_steps.slice(0, 3).map(action => `<li><span class="pill review">${esc(action.priority)}</span><h4>${esc(action.title)}</h4><p>${esc(action.detail)}</p><small>${esc(action.basis)}</small> <button type="button" class="secondary" data-open-tool="${esc(action.tool)}">Open related tool</button></li>`).join('')}</ol><details><summary>See the full action plan</summary><ol class="next-actions" start="4">${review.next_steps.slice(3).map(action => `<li><span class="pill review">${esc(action.priority)}</span><h4>${esc(action.title)}</h4><p>${esc(action.detail)}</p><small>${esc(action.basis)}</small> <button type="button" class="secondary" data-open-tool="${esc(action.tool)}">Open related tool</button></li>`).join('')}</ol></details>
    <details><summary>Documents received and missing evidence</summary>${check.documents.map(doc => `<article class="review-case"><b>${esc(doc.filename)}</b> · ${esc(doc.type_label)}<p>${esc(doc.reason)}</p>${doc.fields_not_detected.length ? `<small>Check these fields: ${esc(doc.fields_not_detected.join(', '))}</small>` : ''}</article>`).join('')}<ul>${check.checklist.map(item => `<li><b>${esc(item.label)}</b>: ${esc(item.status)} · ${esc(item.requirement_kind)}</li>`).join('')}</ul></details>
    <details><summary>Review every extracted policy clause (${review.clause_count})</summary><p class="muted">Extraction may miss unnumbered clauses, schedules or scans. Check the original wording. A clause listed here is not automatically satisfied.</p>${review.clauses.map(clause => `<article class="review-case"><h4>${esc(clause.clause_id)} · ${esc(clause.title)}</h4><span class="pill review">${clause.status === 'review_finding' ? 'Finding to review' : 'Needs verification'}</span><p>${esc(clause.explanation)}</p><blockquote>${esc(clause.text)}</blockquote><small>${esc(clause.source)} · Page ${esc(clause.page)}</small><p>${esc(clause.next_step)}</p></article>`).join('')}</details>
    <details><summary>Medical evidence compared with policy clauses · ${ai.proposals.length} AI drafts</summary><p class="note">${esc(ai.notice)}</p>${ai.proposals.map(draft => `<article class="review-case"><h4>Clause ${esc(draft.clause_id)} · ${esc(aiLabels[draft.assessment] || 'Needs verification')}</h4><span class="pill review">Unconfirmed AI draft</span><p>${esc(draft.explanation)}</p><b>Policy evidence</b><blockquote>${esc(draft.policy_quote)}</blockquote>${draft.evidence.map(citation => `<b>${esc(citation.filename)} · Page ${esc(citation.page)}</b><blockquote>${esc(citation.quote)}</blockquote>`).join('')}${draft.missing_information.length ? `<p>Still needed: ${draft.missing_information.map(esc).join('; ')}</p>` : ''}</article>`).join('')}</details>
    <details><summary>Expense coverage checks</summary><ul>${review.expenses.map(line => `<li>Line ${line.line_no}: ${esc(line.description)} — ${line.status === 'potentially_non_payable' ? 'Potentially non-payable under the extracted terms; verify exclusions' : 'Eligibility needs verification'}</li>`).join('')}</ul></details>`;
  target.querySelectorAll('[data-open-tool]').forEach(button => button.addEventListener('click', () => openClaimTool(button.dataset.openTool)));
}

function renderBenchmarks(c) {
  const rows = c.benchmark_comparisons || [];
  $("#benchmark-rows").innerHTML = rows.length ? rows.map(r => {
    const range = r.comparison_range;
    const sourceType = r.price_type === "contracted_tariff" ? "Contracted tariff" : "Informational benchmark";
    return `<tr><td><b>${esc(r.description)}</b><div class="muted small">${esc(r.code || "No reference code")}</div></td>
      <td>${esc(r.region || "Unknown region")}<div class="muted small">${esc(r.unit)} · ${esc(r.region_basis)}</div></td>
      <td class="num"><b>${inr(r.billed_unit_rate)}</b></td>
      <td>${range ? `${inr(range.p50)} – ${inr(range.p90)}` : `<span class="muted">${esc(r.missing_reason || "No comparable pricing data")}</span>`}</td>
      <td>${r.price_source ? esc(r.price_source) : "No source"}<div class="muted small">${esc(sourceType)}${r.provider_id ? ` · provider ${esc(r.provider_id)}` : ""} · ${esc(r.reference_date || "date unavailable")} · ${esc(r.dataset_version || "unversioned")}</div></td>
      <td>${r.confidence_label ? '<span class="pill review">Worth reviewing</span>' : r.comparable ? '<span class="pill high">Within comparison threshold</span>' : '<span class="muted">Missing comparison</span>'}</td></tr>`;
  }).join("") : '<tr><td colspan="6" class="empty">No charge comparisons were returned.</td></tr>';
}

const EVENT_STATUS = { submission: "submitted", insurer_query: "query_pending", document_response: "documents_responded", settlement: "settled", rejection: "rejected", grievance: "grievance_open", closure: "closed" };
async function loadClaimTimeline(caseId) {
  const [timeline, rules] = await Promise.all([
    apiFetch(`/api/cases/${caseId}/timeline`).then(r => r.json()),
    apiFetch(`/api/cases/${caseId}/escalation-rules`).then(r => r.json()),
  ]);
  const status = timeline.claim_status || "No claim status recorded";
  $("#timeline-status").innerHTML = `<div class="card"><div class="label">Current claim status</div><div class="value">${esc(status.replaceAll("_", " "))}</div></div><div class="card"><div class="label">Escalation rules</div><div class="value">${rules.available ? `Version ${esc(rules.version)}` : "Unavailable"}</div></div>`;
  $("#claim-timeline").innerHTML = timeline.events.length ? timeline.events.map(e => `<li><time>${esc(e.date)}</time><b>${esc(e.event_type.replaceAll("_", " "))}</b><span>${esc(e.claim_status.replaceAll("_", " "))} · ${esc(e.source)}</span>${e.notes ? `<p>${esc(e.notes)}</p>` : ""}${e.required_documents?.length ? `<small>Required: ${e.required_documents.map(esc).join(", ")}</small>` : ""}${e.attachment_names?.length ? `<small>Attached: ${e.attachment_names.map(esc).join(", ")}</small>` : ""}</li>`).join("") : '<li class="muted">No claim events recorded yet.</li>';
  $("#pending-actions").innerHTML = timeline.pending_actions.map(a => `<div class="note"><b>${esc(a.action)}</b>${a.required_documents.length ? ` · Required documents: ${a.required_documents.map(esc).join(", ")}` : ""}</div>`).join("");
}

$("#claim-event-form")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const error = $("#timeline-error");
  error.classList.add("hidden");
  const payload = { event_type: data.get("event_type"), date: data.get("date"), claim_status: data.get("claim_status"), source: data.get("source"), notes: data.get("notes"), required_documents: String(data.get("required_documents") || "").split(",").map(x => x.trim()).filter(Boolean) };
  try {
    const response = await apiFetch(`/api/cases/${state.caseId}/timeline/events`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json(); if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "The event could not be saved.");
    const file = data.get("attachment"); if (file?.size) {
      const added = result.events.find(e => e.event_type === payload.event_type && e.date === payload.date && e.source === payload.source);
      const upload = new FormData(); upload.append("file", file);
      const attached = await apiFetch(`/api/cases/${state.caseId}/timeline/events/${added.id}/documents`, { method: "POST", body: upload });
      if (!attached.ok) throw new Error((await attached.json()).detail || "The event was saved, but the document attachment failed.");
    }
    form.reset(); await loadClaimTimeline(state.caseId);
  } catch (e) { error.textContent = e.message; error.classList.remove("hidden"); }
});

$("#claim-event-form select[name=event_type]")?.addEventListener("change", e => {
  const status = EVENT_STATUS[e.target.value]; if (status) $("#claim-event-form select[name=claim_status]").value = status;
});
async function requestTimelineDraft(grievance) {
  const box = $("#timeline-error"); box.classList.add("hidden");
  try {
    const response = await apiFetch(`/api/cases/${state.caseId}/timeline/draft`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ grievance }) });
    const draft = await response.json(); if (!response.ok) throw new Error(draft.detail || "Could not draft letter.");
    $("#timeline-draft-subject").textContent = draft.subject; $("#timeline-draft-body").textContent = `To: ${draft.recipient}\n\n${draft.body}\n\nStatus: Draft only; not submitted.`; $("#timeline-draft").classList.remove("hidden");
  } catch (e) { box.textContent = e.message; box.classList.remove("hidden"); }
}
$("#draft-follow-up")?.addEventListener("click", () => requestTimelineDraft(false));
$("#draft-grievance")?.addEventListener("click", () => requestTimelineDraft(true));

function renderSummary(c) {
  const r = c.reconciliation;
  const v = $("#verdict");
  if (!c.findings.length) {
    v.className = "verdict good";
    v.innerHTML = `<b>No discrepancies found by the supported bill checks.</b> This does not confirm eligibility or rule out issues in clauses the system could not evaluate. Recorded patient amount: ${inr(r.patient_paid)}.`;
  } else {
    const high = c.findings.filter((f) => f.confidence === "High").length;
    v.className = "verdict money";
    v.innerHTML = `<b>${c.findings.length} item${c.findings.length > 1 ? "s" : ""} worth disputing</b> (${high} high-confidence). ` +
      `Recorded patient amount: ${inr(r.patient_paid)}. Under the extracted terms and audit assumptions, the patient share would be <b>${inr(r.fair_patient_payable_high)}</b>. Confirm unresolved eligibility checks before relying on this scenario.`;
  }
  const cards = [
    ["Billed by hospital", inr(r.billed_gross), ""],
    ["Recorded insurer amount", inr(r.insurer_paid), "Check against the settlement statement"],
    ["Recorded patient amount", inr(r.patient_paid), "Not proof of payment"],
    ["Estimated patient share", inr(r.fair_patient_payable_high), "subject to coverage verification"],
    ["Potential recovery", inr(r.recovery_likely), r.recovery_upper > r.recovery_likely + 0.5 ? `scenario up to ${inr(r.recovery_upper)} including review items; not guaranteed` : "audit scenario, not guaranteed", "hero"],
    ["Hospital overcharges", inr(r.hospital_overcharge_high), r.hospital_overcharge_all > r.hospital_overcharge_high + 0.5 ? `up to ${inr(r.hospital_overcharge_all)}` : "high confidence"],
    ["Insurer shortfall", inr(r.insurer_shortfall), "vs your policy"],
  ];
  $("#summary").innerHTML = cards
    .map(([l, val, sub, cls]) => `<div class="card ${cls || ""}"><div class="label">${esc(l)}</div><div class="value">${esc(val)}</div><div class="sub">${esc(sub)}</div></div>`)
    .join("");
  $("#notes").innerHTML = (r.notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join("");
}

function renderFindings(c) {
  const tbody = $("#findings");
  tbody.innerHTML = "";
  const filter = $("#evidence-filter")?.value || "all";
  const shown = c.findings.filter((f) => filter === "all" || (f.evidence?.status || "insufficient") === filter);
  if (!shown.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">${c.findings.length ? "No findings match this evidence filter." : "No discrepancies found on this bill."}</td></tr>`;
    return;
  }
  for (const f of shown) {
    const row = document.createElement("tr");
    row.className = "row";
    const evidence = f.evidence || { status: "insufficient", bill_lines: [], policy: { status: "unavailable" }, calculation: { status: "partial", steps: [] } };
    const pill = evidence.status === "verified" ? "high" : "review";
    row.innerHTML = `
      <td class="mono">${esc(f.finding_id)}</td>
      <td><span class="type">${esc(TYPE_LABEL[f.type] || f.type)}</span>${esc(f.title)}</td>
      <td>${f.bill_lines.length ? f.bill_lines.map((n) => `<button class="link" data-line="${n}">line ${n}</button>`).join(" ") : '<span class="muted">settlement</span>'}</td>
      <td class="clause">${f.policy ? `§${esc(f.policy.clause_id)} ${esc(f.policy.title)}` : "-"}</td>
      <td><span class="pill ${pill}">${esc(evidence.status === "insufficient" ? "Insufficient evidence" : evidence.status === "verified" ? "Verified" : "Partial evidence")}</span></td>
      <td><span class="pill ${f.confidence === "High" ? "high" : "review"}">${esc(f.confidence)}</span></td>
      <td class="num"><b>${inr(f.impact)}</b></td>`;
    const detail = document.createElement("tr");
    detail.className = "detail hidden";
    detail.innerHTML = `<td colspan="7">
      <div class="k">Evidence on the bill</div><div>${esc(f.bill_evidence)}</div>
      <div class="k">In plain language</div><div>${esc(f.explanation)}</div>
      ${(evidence.bill_lines || []).map((line) => `<div class="evidence-item"><b>Bill line ${esc(line.line_no)}</b> · ${esc(line.document_id || "source ID unavailable")} · ${line.page ? `p.${esc(line.page)}` : "page unavailable"}<div>${line.source_text ? esc(line.source_text) : `<span class="muted">Exact extracted source row unavailable; normalized line: ${esc(line.description)} · ${inr(line.amount)}</span>`}</div></div>`).join("") || (f.bill_lines.length ? `<div class="muted">Bill line source evidence unavailable.</div>` : "")}
      ${evidence.policy?.status === "verified" ? `<div class="k">Verified policy clause ${esc(evidence.policy.clause_id)} · ${esc(evidence.policy.title)} · p.${esc(evidence.policy.page)}</div><blockquote>${esc(evidence.policy.text)}</blockquote>` : `<div class="k">Policy evidence unavailable</div><div class="muted">${esc(evidence.policy?.reason || "No exact matching clause could be verified in the parsed policy.")}</div>`}
      <div class="k">Financial calculation · ${esc(evidence.calculation?.status || "partial")}</div>${(evidence.calculation?.steps || []).length ? `<ol>${evidence.calculation.steps.map((step) => `<li>${esc(step.label)}: ${esc(JSON.stringify(step.inputs))} → ${inr(step.result)}</li>`).join("")}</ol>` : `<div class="muted">Intermediate inputs are unavailable; recorded impact is ${inr(f.impact)}.</div>`}
      ${evidence.status === "insufficient" ? `<div class="note">Insufficient source evidence to verify this finding fully.</div>` : ""}
      <div class="by">Detected by ${esc(f.detected_by)} via ${esc(f.method)} · raise with the ${esc(f.counterparty)}</div></td>`;
    row.addEventListener("click", (e) => {
      if (e.target.dataset.line) { flashLine(e.target.dataset.line); return; }
      detail.classList.toggle("hidden");
      row.classList.toggle("open");
    });
    tbody.append(row, detail);
  }
}

$("#evidence-filter")?.addEventListener("change", () => { if (currentCase) renderFindings(currentCase); });

function renderBill(c) {
  const b = c.bill;
  $("#bill-meta").textContent = `${b.hospital} · bill ${b.bill_number} · ${b.patient_name} (${b.patient_age ?? "?"}) · ${b.admission_date} to ${b.discharge_date} · ${b.procedure}`;
  const flags = {};
  for (const f of c.findings) for (const n of f.bill_lines) {
    if (!flags[n] || f.confidence === "High") flags[n] = { ids: [...(flags[n]?.ids || []), f.finding_id], high: f.confidence === "High" || flags[n]?.high };
    else flags[n].ids.push(f.finding_id);
  }
  $("#bill-lines").innerHTML = b.line_items.map((li) => {
    const fl = flags[li.line_no];
    const cls = fl ? (fl.high ? "flag-high" : "flag-review") : "";
    return `<tr id="line-${li.line_no}" class="${cls}"><td>${li.line_no}</td><td class="mono">${esc(li.service_date)}</td>
      <td>${esc(li.description)}${fl ? `<span class="fl">${esc(fl.ids.join(" "))}</span>` : ""}</td>
      <td class="num">${li.qty}</td><td class="num">${inr(li.unit_price)}</td><td class="num">${inr(li.amount)}</td></tr>`;
  }).join("") + `<tr><td></td><td></td><td><b>Gross total (as printed)</b></td><td></td><td></td><td class="num"><b>${inr(b.stated_gross_total)}</b></td></tr>` +
    b.deductions.map((d) => `<tr><td></td><td></td><td class="muted">Insurer deduction: ${esc(d.label)} - ${esc(d.basis)}</td><td></td><td></td><td class="num muted">${inr(d.amount)}</td></tr>`).join("");
}

function flashLine(n) {
  const row = document.getElementById(`line-${n}`);
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
}

function renderCalc(c) {
  const e = c.reconciliation.entitlement_fair;
  const rows = [
    ["Corrected bill", e.gross],
    ["Less: items the policy doesn't pay for", -e.non_payable],
    ["Less: room rent above the limit", -e.room_excess],
    ["Admissible amount", e.admissible],
    [e.sub_limit_applied != null ? `After sub-limit (cap ${inr(e.sub_limit_applied)})` : "After sub-limit (none applies)", e.after_sub_limit],
    [`Less: co-payment at ${e.copay_pct}%`, -e.copay],
    ["Insurer should pay", e.insurer_payable, "total"],
    ["You should pay", e.patient_payable, "total"],
  ];
  $("#calc").innerHTML = rows.map(([l, v, cls]) => `<tr class="${cls || ""}"><td>${esc(l)}</td><td class="num">${v < 0 ? "−" + inr(-v) : inr(v)}</td></tr>`).join("");
  const t = c.policy_terms;
  const refs = t.clause_refs || {};
  const ref = (k) => (refs[k] ? ` <span class="muted">(§${esc(refs[k])})</span>` : "");
  const items = [];
  if (t.sum_insured) items.push(`Sum insured ${inr(t.sum_insured)}${ref("sum_insured")}`);
  if (t.room_rent_limit) items.push(`Room rent up to ${inr(t.room_rent_limit)}/day${t.icu_rent_limit ? `, ICU ${inr(t.icu_rent_limit)}/day` : ""}${ref("room_rent")}`);
  items.push(`Co-payment ${t.copay_network_pct}% at network, ${t.copay_non_network_pct}% at non-network hospitals${t.copay_senior_pct ? `, +${t.copay_senior_pct}% if aged ${t.senior_age}+` : ""}${ref("copay")}`);
  if (t.sub_limits.length) items.push(`Sub-limits: ${t.sub_limits.map((s) => `${esc(s.procedure)} ${inr(s.amount)}/${esc(s.basis)}`).join("; ")}${ref("sub_limit")}`);
  items.push(`Non-medical items ${t.non_medical_covered ? "covered" : "not covered"}${ref("non_medical")}`);
  items.push(`Extracted by ${esc(t.extraction)}`);
  $("#terms").innerHTML = items.map((i) => `<li>${i}</li>`).join("");
}

function renderLetters(c) {
  const dl = Object.entries(c.files || {}).map(([name, url]) => {
    const label = name.startsWith("reconciliation") ? "Reconciliation report (PDF)" : name.includes("insurer") ? "Letter to insurer (PDF)" : "Letter to hospital (PDF)";
    return `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
  });
  for (const l of c.letters) dl.push(`<a href="/api/cases/${esc(c.case_id)}/letters/${esc(l.recipient)}.txt">Letter to ${esc(l.recipient)} (text)</a>`);
  $("#downloads").innerHTML = dl.join("");
  const tabs = $("#letter-tabs");
  tabs.innerHTML = "";
  if (!c.letters.length) {
    $("#letter").textContent = "No dispute letter needed: nothing on this bill is worth disputing. The reconciliation report is still available above.";
    return;
  }
  const show = (l, btn) => {
    tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $("#letter").textContent = `To,\n${l.to}\n\nSubject: ${l.subject}\n\n${l.body}`;
  };
  c.letters.forEach((l, i) => {
    const b = document.createElement("button");
    b.textContent = l.recipient === "insurer" ? "To the insurer" : "To the hospital";
    b.addEventListener("click", () => show(l, b));
    tabs.appendChild(b);
    if (i === 0) show(l, b);
  });
  const copy = document.createElement("button");
  copy.textContent = "Copy";
  copy.addEventListener("click", () => navigator.clipboard.writeText($("#letter").textContent).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1200); }));
  tabs.appendChild(copy);
}

function reset() {
  state.source?.close();
  clearTimeout(state.poll); state.caseId = null;
  history.replaceState(null, "", location.pathname);
  $("#results").classList.add("hidden");
  $("#run").classList.add("hidden");
  $("#start").classList.remove("hidden");
  selectSample(null);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

init().catch((error) => {
  $("#selection").textContent = error.message || "Could not load the application. Please reload.";
  $("#audit").disabled = true;
});
