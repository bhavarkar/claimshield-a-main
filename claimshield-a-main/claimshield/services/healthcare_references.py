"""Reviewed healthcare price records and policy-specific room adjustment configurations.

No rates are seeded. Imports are pending; an existing regulatory administrator reviews them.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from threading import RLock
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import Category, PolicyCitation
from .regulatory import ReviewStatus, source_restricted_to_demo


class ReferenceProvenance(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    source_name: str = Field(min_length=2, max_length=300)
    source_type: Literal['CGHS', 'PMJAY', 'NPPA', 'POLICY']
    source_url: str | None = Field(None, max_length=2000)
    document_id: str | None = Field(None, max_length=200)
    page_number: int | None = Field(None, ge=1)
    effective_date: date | None = None
    expiry_date: date | None = None
    version: str = Field(min_length=1, max_length=100)
    verification_status: ReviewStatus = ReviewStatus.PENDING
    enforcement_enabled: bool = False
    imported_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    currentness_checked: bool = False
    notes: str = Field('', max_length=3000)
    reviewer_explanation: str = Field('', max_length=2000)
    synthetic: bool = False

    @model_validator(mode='after')
    def dates_and_activation(self):
        if self.expiry_date and self.effective_date and self.expiry_date < self.effective_date:
            raise ValueError('expiry_date must not precede effective_date')
        if self.enforcement_enabled and self.verification_status != ReviewStatus.APPROVED:
            raise ValueError('unreviewed records cannot be enabled')
        return self


class HealthcareRate(ReferenceProvenance):
    record_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,100}$')
    item_name: str = Field(min_length=2, max_length=300)
    normalized_name: str = Field('', max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    category: Category
    unit: str = Field(min_length=1, max_length=80)
    reference_amount: float = Field(gt=0)
    currency: Literal['INR'] = 'INR'
    geography: str | None = Field(None, max_length=100)
    facility_category: str | None = Field(None, max_length=100)
    strength: str | None = Field(None, max_length=100)
    dosage_form: str | None = Field(None, max_length=100)
    pack_size: str | None = Field(None, max_length=100)
    manufacturer: str | None = Field(None, max_length=200)
    price_basis: Literal['unknown', 'net_excluding_tax', 'tax_inclusive'] = 'unknown'

    @model_validator(mode='after')
    def rate_source(self):
        if self.source_type == 'POLICY':
            raise ValueError('Use a room rule for policy configurations')
        return self


class RoomAdjustmentRule(ReferenceProvenance):
    rule_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,100}$')
    rule_name: str = Field(min_length=2, max_length=300)
    source_type: Literal['POLICY'] = 'POLICY'
    policy_document_id: str = Field(min_length=1, max_length=200)
    policy_version_id: str = Field(min_length=1, max_length=200)
    policy_citation: PolicyCitation
    affected_categories: list[Category] = Field(default_factory=list)
    affected_items: list[str] = Field(default_factory=list, max_length=100)
    excluded_categories: list[Category] = Field(default_factory=list)
    excluded_items: list[str] = Field(default_factory=list, max_length=100)
    calculation: Literal['allowed_over_actual'] = 'allowed_over_actual'


class ReferenceImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rates: list[HealthcareRate] = Field(default_factory=list, max_length=1000)
    room_rules: list[RoomAdjustmentRule] = Field(default_factory=list, max_length=100)


class ReferenceReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verification_status: Literal['approved', 'rejected', 'needs_update']
    enforcement_enabled: bool = False
    currentness_checked: bool = False
    source_checked: bool = False
    interpretation_checked: bool = False
    explanation: str = Field(min_length=3, max_length=2000)


class HealthcareReferenceRegistry:
    KEY = 'healthcare-reference/registry.json'
    _lock = RLock()

    def __init__(self, storage):
        self.storage = storage

    def _load(self):
        if not self.storage.exists(self.KEY):
            return {'schema_version': 1, 'version': 1, 'rates': {}, 'room_rules': {}}, 0
        try:
            if hasattr(self.storage, 'get_with_generation'):
                content, gen = self.storage.get_with_generation(self.KEY)
            else:
                content, gen = self.storage.get(self.KEY), 1
        except FileNotFoundError:
            return {'schema_version': 1, 'version': 1, 'rates': {}, 'room_rules': {}}, 0
        data = json.loads(content)
        if (not isinstance(data, dict) or data.get('schema_version') != 1
                or not isinstance(data.get('rates'), dict) or not isinstance(data.get('room_rules'), dict)):
            raise ValueError('Unsupported healthcare reference schema')
        if 'version' not in data:
            data['version'] = 1
        return data, gen

    def _save(self, data, generation: int = 0):
        new_version = data.get('version', 1) + 1
        data['version'] = new_version
        payload = json.dumps(data, indent=2).encode('utf-8')
        from .storage import StorageConcurrencyError
        try:
            if hasattr(self.storage, 'put_if_generation_match'):
                self.storage.put_if_generation_match(self.KEY, payload, generation, 'application/json')
            else:
                self.storage.put(self.KEY, payload, 'application/json')
        except StorageConcurrencyError as exc:
            raise HTTPException(409, 'Healthcare reference registry was modified concurrently by another administrator. Please reload and retry.') from exc
        try:
            self.storage.put(f'healthcare-reference/history/v{new_version}.json', payload, 'application/json')
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            data, _ = self._load()
            return ([HealthcareRate.model_validate(row) for row in data['rates'].values()],
                    [RoomAdjustmentRule.model_validate(row) for row in data['room_rules'].values()])

    def import_records(self, batch: ReferenceImport):
        with self._lock:
            data, gen = self._load()
            incoming = [('rates', r.record_id, r) for r in batch.rates]
            incoming += [('room_rules', r.rule_id, r) for r in batch.room_rules]
            identities = [(kind, key) for kind, key, _ in incoming]
            if len(set(identities)) != len(identities) or any(key in data[kind] for kind, key, _ in incoming):
                raise HTTPException(409, 'Reference IDs are immutable; import a new version with a new ID')
            for kind, key, record in incoming:
                record = record.model_copy(update={'verification_status': ReviewStatus.PENDING,
                    'enforcement_enabled': False, 'currentness_checked': False,
                    'imported_at': datetime.now(timezone.utc), 'reviewed_at': None, 'reviewed_by': None,
                    'reviewer_explanation': ''})
                data[kind][key] = record.model_dump(mode='json')
            self._save(data, gen)
            return {'imported': len(incoming), 'verification_status': 'pending_human_review', 'enforcement_enabled': False}

    def review(self, kind: str, key: str, review: ReferenceReview, reviewer: str):
        with self._lock:
            data, gen = self._load()
            if kind not in {'rates', 'room_rules'} or key not in data[kind]:
                raise HTTPException(404, 'Unknown healthcare reference')
            cls = HealthcareRate if kind == 'rates' else RoomAdjustmentRule
            record = cls.model_validate(data[kind][key])
            if review.verification_status == 'approved':
                if source_restricted_to_demo(synthetic=record.synthetic, source_url=record.source_url):
                    raise HTTPException(409, 'Synthetic healthcare references may only be reviewed in local demo mode')
                if not (review.currentness_checked and review.source_checked and review.interpretation_checked and reviewer):
                    raise HTTPException(409, 'Source, interpretation, currentness and reviewer must be confirmed')
                if not record.effective_date or not (record.source_url or record.document_id):
                    raise HTTPException(409, 'Verified effective date and source URL/document ID are required')
                if isinstance(record, RoomAdjustmentRule) and not (record.affected_categories or record.affected_items):
                    raise HTTPException(409, 'Explicit affected categories or items are required')
            if review.enforcement_enabled and review.verification_status != 'approved':
                raise HTTPException(409, 'Only approved records can be enabled')
            updated = record.model_copy(update={'verification_status': ReviewStatus(review.verification_status),
                'enforcement_enabled': review.enforcement_enabled, 'currentness_checked': review.currentness_checked,
                'reviewed_by': reviewer, 'reviewed_at': datetime.now(timezone.utc),
                'reviewer_explanation': review.explanation})
            data[kind][key] = updated.model_dump(mode='json')
            self._save(data, gen)
            return updated


def is_active(record: ReferenceProvenance, event_date: date | None) -> bool:
    return bool(not source_restricted_to_demo(synthetic=record.synthetic, source_url=record.source_url, reviewer=record.reviewed_by)
                and record.verification_status == ReviewStatus.APPROVED and record.enforcement_enabled
                and record.currentness_checked and record.reviewed_by and record.reviewed_at
                and (record.source_url or record.document_id) and record.effective_date and event_date
                and record.effective_date <= event_date
                and (record.expiry_date is None or event_date <= record.expiry_date))
