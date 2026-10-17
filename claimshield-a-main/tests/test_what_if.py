from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from claimshield.models import Clause, PolicyDoc
from claimshield.services.preclaim import WhatIfInput, simulate_what_if


def _policy():
    return PolicyDoc(plan_name="Synthetic", clauses=[
        Clause(clause_id="1", title="Hospitalisation", text="The company will pay medical expenses for covered hospitalisation."),
        Clause(clause_id="2", title="Room rent", text="Room rent for a normal room is payable up to Rs. 5,000 per day."),
        Clause(clause_id="3", title="Co-payment", text="No co-payment applies to Network Hospitals. A co-payment of 20% applies to Non-Network Hospitals."),
        Clause(clause_id="4", title="Sublimit", text="(a) Knee replacement: Rs. 60,000 per claim"),
        Clause(clause_id="5", title="Sum insured", text="Sum Insured under this policy is Rs. 500,000."),
        Clause(clause_id="6", title="Deductible", text="Deductible Rs. 0 per claim."),
    ])


class _Retriever:
    def __init__(self, policy):
        self.policy = policy

    def search(self, policy, query, top_k=1):
        mapping = {"sum insured": 4, "room rent": 1, "co-payment": 2, "sub-limit": 3, "non-medical": 0,
                   "personal items": 0, "deductible": 5}
        index = next((idx for key, idx in mapping.items() if key in query.lower()), 0)
        return [self.policy.clauses[index]]


def _request(scenarios):
    return WhatIfInput(base={"treatment": "Knee replacement", "estimated_total": 100_000,
        "room_category": "General", "room_rate_per_day": 4_000, "hospitalization_date": date(2026, 11, 1),
        "nights": 2, "network_hospital": True}, scenarios=scenarios)


def test_multiple_scenarios_reconcile_and_policy_is_not_mutated():
    policy = _policy(); before = deepcopy(policy.model_dump())
    result = simulate_what_if(_request([
        {"name": "Cheaper room", "estimated_total": 90_000, "room_category": "Shared", "room_rate_per_day": 3_000},
        {"name": "Higher cap and copay", "hypothetical_sub_limit": 80_000,
         "hypothetical_copay_network_pct": 10},
    ]), policy, _Retriever(policy))
    baseline, cheaper, altered = result["scenarios"]
    assert cheaper["breakdown"]["estimated_total"] == 90_000
    assert cheaper["estimated_insurer_payment"] + cheaper["estimated_patient_payment"] == 90_000
    assert altered["hypothetical_policy_values"]["sub_limit"] == 80_000
    assert altered["breakdown"]["copay_percentage"] == 10
    assert altered["differences"]["patient_payment"] is not None
    assert baseline["hypothetical"] is False and altered["hypothetical"] is True
    assert result["fixed_policy_rules"]
    assert policy.model_dump() == before


def test_negative_scenario_amounts_are_rejected():
    with pytest.raises(ValidationError):
        _request([{"name": "Invalid cost", "estimated_total": -5}])


def test_sublimit_and_copay_overrides_change_insurer_and_patient_payment():
    policy = _policy()
    result = simulate_what_if(_request([{"name": "Higher limit", "hypothetical_sub_limit": 80_000},
                                       {"name": "Higher copay", "hypothetical_copay_network_pct": 20}]), policy, _Retriever(policy))
    baseline, higher_limit, higher_copay = result["scenarios"]
    assert higher_limit["estimated_insurer_payment"] > baseline["estimated_insurer_payment"]
    assert higher_limit["estimated_patient_payment"] < baseline["estimated_patient_payment"]
    assert higher_copay["estimated_insurer_payment"] < baseline["estimated_insurer_payment"]
    assert higher_copay["estimated_insurer_payment"] + higher_copay["estimated_patient_payment"] == 100_000
