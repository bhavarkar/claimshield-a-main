"""Normalised data shapes shared by every agent. These are plain JSON-able pydantic models so
they can travel through ADK session state, BigQuery rows and the HTTP API unchanged."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal[
    "ROOM", "ICU", "CONSULT", "LAB", "RADIOLOGY", "PROCEDURE", "IMPLANT", "ANAESTHESIA",
    "PHARMACY", "CONSUMABLE", "NON_MEDICAL", "PERSONAL", "THERAPY", "OTHER",
]

Confidence = Literal["High", "Worth reviewing"]


class LineItem(BaseModel):
    line_no: int
    service_date: str
    code: str = ""
    description: str
    dept: str = ""
    category: Category = "OTHER"
    unit: str = "per_item"
    qty: float
    unit_price: float
    amount: float
    source_page: Optional[int] = None
    source_text: str = ""
    normalized_description: str = ""
    strength: str = ""
    dosage_form: str = ""
    pack_size: str = ""
    manufacturer: str = ""
    price_basis: Literal['unknown', 'net_excluding_tax', 'tax_inclusive'] = 'unknown'


class Deduction(BaseModel):
    label: str
    basis: str
    amount: float


class Bill(BaseModel):
    bill_number: str = ""
    hospital: str = ""
    hospital_address: str = ""
    region: str = ""
    network_hospital: bool = True
    patient_name: str = ""
    patient_age: Optional[int] = None
    policy_number: str = ""
    insurer_tpa: str = ""
    admission_date: str = ""
    discharge_date: str = ""
    diagnosis: str = ""
    procedure: str = ""
    line_items: list[LineItem] = Field(default_factory=list)
    stated_gross_total: float = 0.0
    deductions: list[Deduction] = Field(default_factory=list)
    insurer_approved: float = 0.0
    patient_payable: float = 0.0
    parser: str = ""
    facility_category: str = ""
    room_category: str = ""
    admission_days: Optional[float] = None
    actual_room_rent_per_day: Optional[float] = None

    @property
    def nights(self) -> int:
        try:
            return max((date.fromisoformat(self.discharge_date) - date.fromisoformat(self.admission_date)).days, 0)
        except ValueError:
            return 0


class Clause(BaseModel):
    clause_id: str
    title: str
    text: str
    page: int = 1
    source: str = ""
    score: float = 0.0
    document_id: str | None = None
    version_id: str | None = None

    def cite(self) -> str:
        return f"§{self.clause_id} {self.title}"


class PolicyDoc(BaseModel):
    insurer: str = ""
    plan_name: str = ""
    policy_number: str = ""
    source: str = ""
    clauses: list[Clause] = Field(default_factory=list)
    schedule: dict[str, str] = Field(default_factory=dict)
    document_id: str | None = None
    version_id: str | None = None


class SubLimit(BaseModel):
    procedure: str
    amount: float
    basis: str = "claim"


class PolicyTerms(BaseModel):
    """The handful of numbers the audit needs, each tied back to the clause it came from."""

    sum_insured: Optional[float] = None
    room_rent_limit: Optional[float] = None
    icu_rent_limit: Optional[float] = None
    copay_network_pct: float = 0.0
    copay_non_network_pct: float = 0.0
    copay_senior_pct: float = 0.0
    senior_age: Optional[int] = None
    sub_limits: list[SubLimit] = Field(default_factory=list)
    non_medical_covered: bool = False
    personal_items_excluded: bool = True
    clause_refs: dict[str, str] = Field(default_factory=dict)
    extraction: str = "rules"
    uncertain_topics: list[str] = Field(default_factory=list)
    room_rent_limit_type: Optional[Literal['per_day', 'percent_sum_insured_per_day', 'category', 'unknown']] = None
    room_rent_limit_pct: Optional[float] = None


class FindingType(str, Enum):
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    ARITHMETIC_ERROR = "ARITHMETIC_ERROR"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    UPCODING_SIGNAL = "UPCODING_SIGNAL"
    PRICE_OUTLIER = "PRICE_OUTLIER"
    COVERAGE_MISMATCH = "COVERAGE_MISMATCH"
    CLINICAL_REVIEW = "CLINICAL_REVIEW"
    NON_PAYABLE_ITEM = 'NON_PAYABLE_ITEM'
    ROOM_RENT_LIMIT = 'ROOM_RENT_LIMIT'
    ROOM_RENT_PROPORTIONATE = 'ROOM_RENT_PROPORTIONATE'
    HEALTHCARE_BENCHMARK = 'HEALTHCARE_BENCHMARK'
    NPPA_REFERENCE = 'NPPA_REFERENCE'


FINDING_LABELS = {
    FindingType.DUPLICATE_CHARGE: "Duplicate charge",
    FindingType.ARITHMETIC_ERROR: "Quantity x rate error",
    FindingType.TOTAL_MISMATCH: "Bill total doesn't add up",
    FindingType.QUANTITY_MISMATCH: "Quantity exceeds stay",
    FindingType.UPCODING_SIGNAL: "Possible upcoding",
    FindingType.PRICE_OUTLIER: "Priced above benchmark",
    FindingType.COVERAGE_MISMATCH: "Settled differently from policy",
    FindingType.CLINICAL_REVIEW: "Clinical consistency question",
    FindingType.NON_PAYABLE_ITEM: 'Non-payable item reference check',
    FindingType.ROOM_RENT_LIMIT: 'Room-rent daily limit',
    FindingType.ROOM_RENT_PROPORTIONATE: 'Policy-configured proportionate adjustment',
    FindingType.HEALTHCARE_BENCHMARK: 'Healthcare benchmark comparison',
    FindingType.NPPA_REFERENCE: 'NPPA reference comparison',
}


class PolicyCitation(BaseModel):
    clause_id: str
    title: str
    quote: str
    source: str = ""
    page: int = 1
    document_id: str | None = None
    version_id: str | None = None


class Finding(BaseModel):
    finding_id: str
    type: FindingType
    title: str
    bill_lines: list[int] = Field(default_factory=list)
    bill_evidence: str
    policy: Optional[PolicyCitation] = None
    explanation: str
    confidence: Confidence
    impact: float
    counterparty: Literal["hospital", "insurer"]
    bill_correction: float = 0.0
    correction_line: Optional[int] = None
    detected_by: str
    method: str
    topic: str = ""
    evidence: Optional[dict] = None
    benchmark: Optional[dict] = None


class Entitlement(BaseModel):
    gross: float
    non_payable: float
    non_payable_lines: list[int] = Field(default_factory=list)
    room_excess: float
    admissible: float
    sub_limit_applied: Optional[float] = None
    after_sub_limit: float
    copay_pct: float
    copay: float
    insurer_payable: float
    patient_payable: float


class Reconciliation(BaseModel):
    billed_gross: float
    insurer_paid: float
    patient_paid: float
    hospital_overcharge_high: float
    hospital_overcharge_all: float
    insurer_shortfall: float
    fair_bill_high: float
    fair_insurer_payable_high: float
    fair_patient_payable_high: float
    fair_patient_payable_all: float
    recovery_likely: float
    recovery_upper: float
    entitlement_as_billed: Entitlement
    entitlement_fair: Entitlement
    notes: list[str] = Field(default_factory=list)


class Letter(BaseModel):
    recipient: Literal["insurer", "hospital"]
    to: str
    subject: str
    body: str
    finding_ids: list[str] = Field(default_factory=list)
