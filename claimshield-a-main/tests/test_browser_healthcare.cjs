const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function view() {
  const elements = {};
  for (const id of ['panel', 'summary', 'warnings', 'findings']) {
    const selector = id === 'panel' ? '#healthcare-audit-panel' : `#healthcare-audit-${id}`;
    elements[selector] = {innerHTML: '', classList: {toggle(name, enabled) { this[name] = enabled; }}};
  }
  const context = vm.createContext({document: {querySelector: selector => elements[selector]}});
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  vm.runInContext(source.split('\ninit().catch(')[0] + '\nglobalThis.render = renderHealthcareAudit;', context);
  return {render: context.render, elements};
}

test('stored report displays all financial/status fields and source provenance', () => {
  const report = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/healthcare/source_unavailable_output.json')));
  report.findings.push({title: 'NPPA check', type: 'NPPA_REFERENCE', status: 'flagged', severity: 'warning',
    billed_amount: 30, reference_amount: 20, potential_difference: 10, confidence: 'High', confidence_score: .98,
    explanation: 'Confirm product and tax basis.', evidence: {source: {source_name: 'Synthetic source',
      verification_status: 'approved', page_number: 3, effective_date: '2026-01-01', version: 'fixture'}}});
  const page = view(); page.render(report);
  const summary = page.elements['#healthcare-audit-summary'].innerHTML;
  for (const label of ['Billed total', 'Potential adjustment scenario', 'Benchmark variance', 'Clear checks', 'Flagged checks', 'Needs review']) assert.ok(summary.includes(label));
  const findings = page.elements['#healthcare-audit-findings'].innerHTML;
  for (const field of ['NPPA_REFERENCE', 'warning', '₹30', '₹20', '₹10', '0.98', 'Confirm product', 'Synthetic source', 'approved', 'Page: 3', '2026-01-01']) assert.ok(findings.includes(field), field);
});

test('unknown results remain undetermined, source text is escaped, and legacy reports are hidden', () => {
  const page = view();
  page.render({finding_counts: {clear: 0, flagged: 0, needs_review: 1}, potential_discrepancy: null,
    warnings: ['<script>bad</script>'], findings: [{status: 'needs_review', potential_difference: 9999,
      explanation: '<img src=x onerror=bad>', evidence: {source: {source_name: '<script>source</script>'}}}]});
  const html = page.elements['#healthcare-audit-findings'].innerHTML;
  assert.ok(html.includes('Not determined') && !html.includes('9,999'));
  assert.ok(html.includes('&lt;img') && !html.includes('<img'));
  assert.ok(page.elements['#healthcare-audit-warnings'].innerHTML.includes('&lt;script&gt;'));
  page.render(undefined);
  assert.equal(page.elements['#healthcare-audit-panel'].classList.hidden, true);
  assert.equal(page.elements['#healthcare-audit-findings'].innerHTML, '');
});
