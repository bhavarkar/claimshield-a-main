"""Connect normalized inputs to existing storage and the reviewed regulatory registry."""
from pydantic import ValidationError
from fastapi import HTTPException

from ..domain.healthcare_audit import HealthcareAuditInput, audit_healthcare
from .healthcare_references import HealthcareReferenceRegistry
from .regulatory import RegulatoryRegistry, ApplicabilityRequest


def run_healthcare_audit(data: HealthcareAuditInput, svc):
    warnings = []
    try:
        rates, room_rules = HealthcareReferenceRegistry(svc.storage).snapshot()
    except (OSError, ValueError, KeyError, HTTPException):
        rates, room_rules = [], []
        warnings.append('Healthcare reference storage could not be verified; rate and proportionate checks remain unavailable.')
    try:
        regulatory = RegulatoryRegistry(svc.storage).item_rules(data.context)
    except (OSError, ValueError, KeyError, HTTPException):
        regulatory = []
        warnings.append('Regulatory source storage could not be verified; no item exclusion was applied.')
    if not rates:
        warnings.append('Healthcare price references are unavailable; no CGHS, PM-JAY or NPPA rate was inferred.')
    if not any(status == 'Applicable' for _, _, status, _ in regulatory):
        warnings.append('No applicable verified non-payable item rules are available; no regulatory exclusion was inferred.')
    result = audit_healthcare(data, rates=rates, room_rules=room_rules, regulatory_rules=regulatory)
    result['warnings'] = warnings
    return result


def audit_from_state(state, svc):
    """Additive report: source checks do not change legacy recovery/settlement math."""
    try:
        data = HealthcareAuditInput(claim_id=state['case_id'], bill=state['bill'],
                                    policy_doc=state.get('policy_doc', {}), policy_terms=state.get('policy_terms', {}),
                                    context=ApplicabilityRequest(insurance_category='health', claim_context=state.get('claim_context', 'hospitalization'),
                                                                 event_date=state['bill'].get('admission_date') or None))
    except ValidationError:
        return {'claim_id': state['case_id'], 'status': 'needs_review', 'findings': [],
                'finding_counts': {'clear': 0, 'flagged': 0, 'needs_review': 1},
                'potential_discrepancy': None, 'total_billed': None, 'benchmark_variance': None,
                'warning_count': 1, 'warnings': ['Normalized dates or amounts need verification before healthcare reference checks can run.'],
                'notice': 'Normalized dates or amounts need verification before healthcare reference checks can run.'}
    return run_healthcare_audit(data, svc)
