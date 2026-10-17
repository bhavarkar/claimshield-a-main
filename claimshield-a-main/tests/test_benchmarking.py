from pathlib import Path

from claimshield.agents.steps import benchmark_step
from claimshield.models import Bill, LineItem
from claimshield.services import get_services
from claimshield.trace import Trace


def _bill(code="RM-GEN", *, rate=2000, unit="per_day", region="tier-2", description="General room"):
    return Bill(hospital="Example Hospital", procedure="General treatment", region=region,
        line_items=[LineItem(line_no=1, code=code, service_date="2026-03-01", description=description,
                             category="ROOM", unit=unit, qty=1, unit_price=rate, amount=rate)])


def _comparison(bill):
    svc = get_services(); case_id = "benchmark-feature-test"
    svc.store.load_case(case_id, bill)
    meta = svc.store.benchmark_metadata
    return svc.store.query("benchmark_comparisons", case_id=case_id,
                           region=bill.region or meta["default_region"], dataset_version=meta["version"])[0]


def test_comparable_charge_returns_range_source_date_and_version():
    row = _comparison(_bill())
    assert row["p50_unit_price"] == 1500 and row["p90_unit_price"] == 2200
    assert row["unit"] == "per_day" and row["price_type"] == "informational"
    assert row["reference_date"] == "2026-01-01" and row["dataset_version"] == "synthetic-2026.1"


def test_high_outlier_is_worth_reviewing_with_synthetic_source_metadata():
    svc = get_services(); case_id = "benchmark-high-test"; bill = _bill(rate=5000)
    svc.store.load_case(case_id, bill)
    result = benchmark_step({"bill": bill.model_dump(mode="json"), "case_id": case_id, "clauses": {}}, Trace(case_id), svc)
    finding = result["benchmark_findings"][0]
    assert finding["confidence"] == "Worth reviewing"
    assert finding["benchmark"]["price_type"] == "informational"
    assert finding["benchmark"]["dataset_version"] == "synthetic-2026.1"
    assert "Synthetic" in finding["benchmark"]["dataset_label"]
    assert "fraud" not in finding["explanation"].lower()


def test_unit_and_region_must_match_for_comparison():
    wrong_unit = _comparison(_bill(unit="per_item"))
    wrong_region = _comparison(_bill(region="tier-1"))
    assert wrong_unit["p90_unit_price"] is None
    assert wrong_region["p90_unit_price"] is None


def test_no_comparable_procedure_returns_explicit_missing_row():
    row = _comparison(_bill(code="UNLISTED", description="Specialized service"))
    assert row["p50_unit_price"] is None and row["p90_unit_price"] is None


def test_provider_matched_contracted_tariff_is_separate_from_information_reference():
    svc = get_services(); case_id = "benchmark-contract-test"; bill = _bill(rate=4000)
    svc.store.load_case(case_id, bill)
    with svc.store._lock:
        svc.store._db.execute("""INSERT INTO reference_pricing
            (code,description,category,family,tier,p50_unit_price,p90_unit_price,city_tier,source,
             region,unit,price_source,price_type,provider_id,reference_date,dataset_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("RM-GEN", "Contracted general room", "ROOM", "ROOM", 1, 2400, 2600, 2,
             "hospital-contract", "tier-2", "per_day", "Hospital tariff", "contracted_tariff",
             "Example Hospital", "2026-01-01", "synthetic-2026.1"))
    comparison = svc.store.query("benchmark_comparisons", case_id=case_id, region="tier-2",
                                 dataset_version="synthetic-2026.1")
    assert len(comparison) == 1
    assert comparison[0]["price_type"] == "contracted_tariff"
    findings = benchmark_step({"bill": bill.model_dump(mode="json"), "case_id": case_id, "clauses": {}}, Trace(case_id), svc)
    assert findings["benchmark_findings"][0]["benchmark"]["price_type"] == "contracted_tariff"
