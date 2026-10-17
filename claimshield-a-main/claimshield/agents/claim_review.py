"""Evidence-first claim journey, composed with the existing six audit agents."""
from __future__ import annotations

from ..services.document_check import check_documents
from ..services.llm import amounts_in
import re


def document_agent(manifest, svc, trace, claim_type):
    trace.emit('document_agent', 'start', 'Checking your medical and insurance documents')
    documents = []
    warnings = []
    pages, remaining = [], 120000
    for entry in manifest:
        content = svc.storage.get(entry['key'])
        try:
            try:
                raw = svc.parser.parse(content)
            except Exception:
                if svc.parser is svc.local_parser:
                    raise
                raw = svc.local_parser.parse(content)
        except Exception:
            raw = None
            warnings.append(f"Could not read {entry['name']}. Upload a readable PDF or ask for a manual check.")
        documents.append({'filename': entry['name'], 'content': content, 'raw': raw})
        if raw and entry['group'] == 'medical':
            for page_number, text in enumerate(raw.pages, 1):
                if remaining <= 0 or len(pages) >= 60:
                    break
                excerpt = text[:min(6000, remaining)]
                if excerpt.strip():
                    pages.append({'document_id': f'D{len(documents)}', 'filename': entry['name'],
                                  'page': page_number, 'text': excerpt})
                    remaining -= len(excerpt)
    result = check_documents(documents, claim_type=claim_type, data_dir=svc.settings.data_dir)
    for doc, entry in zip(result['documents'], manifest):
        doc.update(group=entry['group'], source_key=entry['key'])
    result['warnings'] = warnings
    result['_medical_pages'] = pages  # Internal, bounded context; removed before persistence.
    trace.emit('document_agent', 'result', 'Document checklist ready', summary=result['summary'])
    return result


def clause_review_agent(state, pages, svc, trace):
    """Model proposals stay drafts; citations are checked against the supplied pages."""
    trace.emit('clause_review_agent', 'start', 'Comparing medical evidence with policy clauses')
    clauses = (state.get('policy_doc') or {}).get('clauses', [])
    result = {'mode': 'unavailable', 'proposals': [], 'notice':
              'AI medical-to-clause review is unavailable. No eligibility conclusion was inferred.'}
    if not svc.llm.available or not pages or not clauses:
        trace.emit('clause_review_agent', 'warn', result['notice'])
        return result
    result.update(mode='ai_draft', notice='AI draft assessments use selected text excerpts. Exact quotations are checked; clinical reasoning and eligibility are not independently verified. A qualified reviewer and insurer must confirm applicability. Some clauses or pages may be omitted by context limits.')
    valid_pages = {(p['document_id'], p['page']): p for p in pages}
    # Bound cost per queued job: at most eight sequential model calls, never unbounded fan-out.
    for offset in range(0, min(len(clauses), 48), 6):
        batch = clauses[offset:offset + 6]
        words = set(re.findall(r'[a-z]{4,}', ' '.join(c['title'] + ' ' + c['text'][:1000] for c in batch).lower()))
        selected = sorted(pages, key=lambda p: len(words & set(re.findall(r'[a-z]{4,}', p['text'].lower()))), reverse=True)[:8]
        try:
            proposals = svc.llm.review_claim_clauses(batch, selected)
        except Exception:
            trace.emit('clause_review_agent', 'warn', 'AI comparison unavailable for this batch; its clauses remain unverified')
            continue
        if not isinstance(proposals, list):
            continue
        by_id = {c['clause_id']: c for c in batch}
        counts = {key: sum(isinstance(p, dict) and p.get('clause_id') == key for p in proposals) for key in by_id}
        allowed_pages = {(p['document_id'], p['page']) for p in selected}
        for proposal in proposals[:12]:
            if not isinstance(proposal, dict) or not isinstance(proposal.get('clause_id'), str):
                continue
            clause = by_id.get(proposal.get('clause_id'))
            if not clause or counts[clause['clause_id']] != 1:
                continue
            assessment = proposal.get('assessment')
            quote = proposal.get('policy_quote')
            explanation = proposal.get('explanation')
            missing = proposal.get('missing_information')
            evidence = proposal.get('evidence')
            if (assessment not in {'potentially_eligible', 'potentially_ineligible', 'needs_verification'}
                    or not isinstance(quote, str) or len(quote.strip()) < 15 or quote not in clause['text']
                    or not isinstance(explanation, str) or not 1 <= len(explanation) <= 1500
                    or not isinstance(missing, list) or len(missing) > 10
                    or not all(isinstance(item, str) and len(item) <= 400 for item in missing)
                    or not isinstance(evidence, list) or len(evidence) > 8):
                continue
            citations = []
            for citation in evidence:
                if (not isinstance(citation, dict) or type(citation.get('page')) is not int
                        or not isinstance(citation.get('document_id'), str)):
                    break
                page_key = (citation.get('document_id'), citation['page'])
                source = valid_pages.get(page_key) if page_key in allowed_pages else None
                text = citation.get('quote')
                if not source or not isinstance(text, str) or len(text.strip()) < 15 or text not in source['text']:
                    break
                citations.append({**citation, 'filename': source['filename']})
            if len(citations) != len(evidence) or (assessment != 'needs_verification' and not citations):
                continue
            if not amounts_in(explanation + ' '.join(missing)) <= amounts_in(quote + ' '.join(c['quote'] for c in citations)):
                continue
            if missing:
                assessment = 'needs_verification'
            result['proposals'].append({'clause_id': clause['clause_id'], 'assessment': assessment,
                                        'policy_quote': quote, 'explanation': explanation,
                                        'evidence': citations, 'missing_information': missing,
                                        'review_status': 'unconfirmed_ai_draft'})
    trace.emit('clause_review_agent', 'result', f"Prepared {len(result['proposals'])} source-checked AI drafts; remaining clauses need verification")
    if not result['proposals']:
        result.update(mode='unavailable', notice='No AI clause comparison passed the evidence checks. The clauses remain unverified.')
    return result


def guidance_agent(state, checklist, manifest):
    """Account for every extracted clause; never equate extraction with eligibility."""
    policy = state.get('policy_doc') or {}
    terms = state.get('policy_terms') or {}
    bill = state.get('bill') or {}
    refs = terms.get('clause_refs', {})
    findings = state.get('findings', [])
    clauses = []
    for clause in policy.get('clauses', []):
        topics = [topic for topic, ref in refs.items() if ref == clause['clause_id']]
        related = [f for f in findings if (f.get('policy') or {}).get('clause_id') == clause['clause_id']
                   and (f.get('policy') or {}).get('quote') == clause['text']]
        # A retrieved clause is not proof that all of its conditions were evaluated.
        clauses.append({**clause, 'status': 'review_finding' if related else 'needs_verification',
                        'topics': topics, 'finding_ids': [f['finding_id'] for f in related],
                        'explanation': ' '.join(f['explanation'] for f in related) if related else
                        'This clause was extracted. Its full applicability to this claim still needs confirmation.',
                        'next_step': 'Review the cited evidence with the insurer.' if related else
                        'Ask the insurer to confirm this clause against the treatment and policy schedule.'})

    gaps = [
        'Confirm the patient is insured and the policy was active on the treatment date.',
        'Confirm waiting periods, pre-existing-condition terms and treatment exclusions.',
        'Confirm remaining sum insured, earlier claims, deductible and hospital network status.',
        'Have the insurer confirm medical necessity and any pre-authorization requirements.',
    ]
    if bill.get('policy_number') and policy.get('policy_number') and bill['policy_number'] != policy['policy_number']:
        gaps.insert(0, 'The bill and policy numbers differ. Supply the matching policy before relying on the audit.')
    extras = [d for d in manifest if d['group'] == 'insurance' and not d.get('primary')]
    if extras:
        gaps.insert(0, 'Additional insurance documents need reconciliation with the main wording. Endorsements and renewals may change coverage; use Policy history below.')
    if not clauses:
        gaps.insert(0, 'No numbered policy clauses were extracted. Supply full readable policy wording for clause review.')
    gaps += checklist.get('warnings', [])
    gaps += [f"Resolve unclear policy topic: {t.replace('_', ' ')}." for t in terms.get('uncertain_topics', [])]

    actions = []
    for item in checklist['checklist']:
        if item['status'] != 'complete':
            actions.append({'priority': 'Before submission', 'title': f"Check {item['label'].lower()}",
                            'detail': item['note'], 'basis': item['requirement_kind'], 'tool': 'document-check-panel'})
    for gap in gaps:
        actions.append({'priority': 'Verify coverage', 'title': gap, 'detail': 'This is unresolved, not a claim rejection.',
                        'basis': 'Evidence gap', 'tool': 'policy-history-panel' if 'insurance documents' in gap else 'policy-explainer-panel'})
    for proposal in (state.get('ai_clause_review') or {}).get('proposals', []):
        if proposal['assessment'] == 'potentially_ineligible' or proposal['missing_information']:
            actions.append({'priority': 'Confirm AI draft', 'title': f"Review clause {proposal['clause_id']} with the insurer",
                            'detail': proposal['explanation'] + ' ' + ' '.join(proposal['missing_information']),
                            'basis': 'Unconfirmed AI interpretation; not a rejection', 'tool': 'policy-explainer-panel'})
    for finding in findings:
        actions.append({'priority': 'Review potential recovery', 'title': finding['title'],
                        'detail': finding['explanation'], 'basis': finding['confidence'],
                        'finding_id': finding['finding_id'], 'tool': 'findings-panel'})
    actions.append({'priority': 'Submit and track', 'title': 'Send a complete claim pack to the insurer or hospital insurance desk.',
                    'detail': 'Keep the submission acknowledgement and claim number. Record queries and respond with the requested evidence. Review any draft before sending.',
                    'basis': 'Next step after verification', 'tool': 'timeline-panel'})
    recon = state.get('reconciliation') or {}
    excluded = set((recon.get('entitlement_as_billed') or {}).get('non_payable_lines', []))
    return {'status': 'needs_verification', 'headline': 'Your claim needs a few confirmations',
            'notice': 'This is an evidence review, not insurer approval or rejection. Recovery figures are audit scenarios, not guaranteed coverage. Where available, medical-to-clause comparisons are unconfirmed AI drafts, not clinical or coverage decisions.',
            'ai_clause_review': state.get('ai_clause_review', {'mode': 'unavailable', 'proposals': [], 'notice': 'AI clause comparison unavailable.'}),
            'clauses': clauses, 'clause_count': len(clauses), 'verification_gaps': gaps,
            'document_check': checklist, 'next_steps': actions,
            'expenses': [{'line_no': line['line_no'], 'description': line['description'],
                          'status': 'potentially_non_payable' if line['line_no'] in excluded else 'needs_verification'}
                         for line in bill.get('line_items', [])],
            'recovery_scenario': {'likely': recon.get('recovery_likely'), 'upper': recon.get('recovery_upper'),
                                  'confirmed': False}}
