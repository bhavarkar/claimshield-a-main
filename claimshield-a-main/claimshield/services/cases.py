"""Immutable server-authored ownership records, checked before any case data access."""

import json
import re

from fastapi import HTTPException

from ..api.auth import Principal

CASE_ID = re.compile(r"[a-z0-9-]{6,64}")


class CaseRepository:
    def __init__(self, storage):
        self.storage = storage

    def create(self, case_id: str, principal: Principal) -> None:
        if not CASE_ID.fullmatch(case_id):
            raise ValueError("Invalid case ID")
        record = {"version": 1, "case_id": case_id, "owner_id": principal.subject,
                  "tenant_id": principal.tenant, "issuer": principal.issuer}
        self.storage.create(f"cases/{case_id}/owner.json", json.dumps(record).encode(), "application/json")

    def authorize(self, case_id: str, principal: Principal) -> None:
        if not CASE_ID.fullmatch(case_id):
            raise HTTPException(404, "Unknown case")
        key = f"cases/{case_id}/owner.json"
        # Missing/legacy metadata is never inferred from case.json or a requester.
        if not self.storage.exists(key):
            raise HTTPException(404, "Unknown case")
        try:
            owner = json.loads(self.storage.get(key))
            allowed = (owner.get("version") == 1 and owner.get("case_id") == case_id
                       and owner.get("owner_id") == principal.subject
                       and owner.get("tenant_id") == principal.tenant
                       and owner.get("issuer") == principal.issuer)
        except (ValueError, AttributeError):
            allowed = False
        if not allowed:
            raise HTTPException(404, "Unknown case")
