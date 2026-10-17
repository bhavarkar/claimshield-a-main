"""Analytical store for line items and reference pricing.

The SQL in claimshield/sql/*.sql is written once in the subset both engines accept (window
functions, `||`, CAST AS INTEGER) and run unchanged against:
  * BigQueryStore - `project.dataset.table`, @named parameters
  * SqliteStore   - in-process database loaded from data/reference/*.csv
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Bill, Finding

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
TABLES = ("line_items", "reference_pricing", "procedure_benchmarks")


def _benchmark_manifest(reference_dir: Path) -> dict[str, Any]:
    path = reference_dir / "manifest.json"
    if not path.exists():
        return {"version": "legacy-unversioned", "default_region": "", "price_type": "informational",
                "description": "Legacy reference prices without dataset version metadata."}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("version") or not manifest.get("effective_date"):
        raise ValueError("Benchmark manifest must declare a version and effective_date")
    return manifest


def load_sql(name: str) -> str:
    return (SQL_DIR / f"{name}.sql").read_text()


def _line_rows(case_id: str, bill: Bill) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "case_id": case_id, "line_no": li.line_no, "service_date": li.service_date, "code": li.code,
            "description": li.description, "dept": li.dept, "category": li.category, "unit": li.unit,
            "region": bill.region, "qty": li.qty,
            "unit_price": li.unit_price, "amount": li.amount, "hospital": bill.hospital,
            "procedure": bill.procedure, "ingested_at": now,
        }
        for li in bill.line_items
    ]


class SqliteStore:
    name = "SQLite (local stand-in for BigQuery)"

    def __init__(self, reference_dir: Path) -> None:
        self.reference_dir = reference_dir
        self.benchmark_metadata = _benchmark_manifest(reference_dir)
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(
            """
            CREATE TABLE line_items (case_id TEXT, line_no INTEGER, service_date TEXT, code TEXT, description TEXT,
              dept TEXT, category TEXT, unit TEXT, region TEXT, qty REAL, unit_price REAL, amount REAL, hospital TEXT, procedure TEXT, ingested_at TEXT);
            CREATE TABLE reference_pricing (code TEXT, description TEXT, category TEXT, family TEXT, tier INTEGER,
              p50_unit_price REAL, p90_unit_price REAL, city_tier INTEGER, source TEXT, region TEXT, unit TEXT,
              price_source TEXT, price_type TEXT, provider_id TEXT, reference_date TEXT, dataset_version TEXT);
            CREATE TABLE procedure_benchmarks (procedure_key TEXT, family TEXT, max_expected_tier INTEGER, expected_code TEXT,
              rationale TEXT);
            CREATE TABLE findings (case_id TEXT, finding_id TEXT, type TEXT, title TEXT, bill_lines TEXT, confidence TEXT,
              impact REAL, counterparty TEXT, clause_id TEXT, created_at TEXT);
            CREATE INDEX idx_li_case ON line_items(case_id);
            """
        )
        for table in ("reference_pricing", "procedure_benchmarks"):
            with open(reference_dir / f"{table}.csv", newline="") as fh:
                rows = list(csv.DictReader(fh))
            if table == "reference_pricing":
                for row in rows:
                    if row.get("price_type") not in {None, "", "informational", "contracted_tariff"}:
                        raise ValueError(f"Unsupported benchmark price_type: {row['price_type']}")
                    if row.get("price_type") == "contracted_tariff" and not row.get("provider_id"):
                        raise ValueError("Contracted tariffs require a provider_id for an exact hospital match")
                    if self.benchmark_metadata.get("version") != "legacy-unversioned":
                        if row.get("dataset_version") != self.benchmark_metadata["version"]:
                            raise ValueError("Reference row dataset_version does not match manifest version")
                        for field in ("region", "unit", "price_source", "reference_date"):
                            if not row.get(field):
                                raise ValueError(f"Versioned benchmark row is missing {field}")
            cols = list(rows[0].keys())
            self._db.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        self._db.commit()

    def _render(self, sql: str) -> str:
        sql = sql.format(**{t: t for t in TABLES})
        return re.sub(r"@(\w+)", r":\1", sql)

    def query(self, name: str, **params: Any) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(self._render(load_sql(name)), params)
            return [dict(r) for r in cur.fetchall()]

    def load_case(self, case_id: str, bill: Bill) -> int:
        rows = _line_rows(case_id, bill)
        with self._lock:
            self._db.execute("DELETE FROM line_items WHERE case_id = ?", (case_id,))
            if rows:
                cols = list(rows[0].keys())
                self._db.executemany(
                    f"INSERT INTO line_items ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", rows
                )
            self._db.commit()
        return len(rows)

    def save_findings(self, case_id: str, findings: list[Finding]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute("DELETE FROM findings WHERE case_id = ?", (case_id,))
            self._db.executemany(
                "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(case_id, f.finding_id, f.type.value, f.title, ",".join(map(str, f.bill_lines)), f.confidence, f.impact,
                  f.counterparty, f.policy.clause_id if f.policy else None, now) for f in findings],
            )
            self._db.commit()

    def categories(self) -> dict[str, str]:
        return {r["code"]: r["category"] for r in self.query("categories")}

    def release_case(self, case_id: str) -> None:
        """Discard transient analytical rows after the HTTP result is persisted."""
        with self._lock:
            self._db.execute('DELETE FROM line_items WHERE case_id=?', (case_id,))
            self._db.execute('DELETE FROM findings WHERE case_id=?', (case_id,))
            self._db.commit()


class BigQueryStore:
    name = "BigQuery"

    def __init__(self, project: str, dataset: str, location: str | None = None, reference_dir: Path | None = None) -> None:
        from google.cloud import bigquery

        self._bq = bigquery
        self._client = bigquery.Client(project=project, location=location)
        self._fq = {t: f"`{project}.{dataset}.{t}`" for t in TABLES}
        self._findings = f"{project}.{dataset}.findings"
        self._line_items = f"{project}.{dataset}.line_items"
        self.benchmark_metadata = _benchmark_manifest(reference_dir) if reference_dir else {
            "version": "legacy-unversioned", "default_region": "", "price_type": "informational"}

    def query(self, name: str, **params: Any) -> list[dict[str, Any]]:
        bq = self._bq

        def param(k: str, v: Any):
            kind = "FLOAT64" if isinstance(v, float) else "INT64" if isinstance(v, int) else "STRING"
            return bq.ScalarQueryParameter(k, kind, v)

        job = self._client.query(
            load_sql(name).format(**self._fq),
            job_config=bq.QueryJobConfig(query_parameters=[param(k, v) for k, v in params.items()]),
        )
        return [dict(r.items()) for r in job.result()]

    def load_case(self, case_id: str, bill: Bill) -> int:
        rows = _line_rows(case_id, bill)
        errors = self._client.insert_rows_json(self._line_items, rows)
        if errors:
            raise RuntimeError(f"BigQuery insert failed: {errors[:3]}")
        return len(rows)

    def save_findings(self, case_id: str, findings: list[Finding]) -> None:
        if not findings:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {"case_id": case_id, "finding_id": f.finding_id, "type": f.type.value, "title": f.title, "bill_lines": f.bill_lines,
             "confidence": f.confidence, "impact": f.impact, "counterparty": f.counterparty,
             "clause_id": f.policy.clause_id if f.policy else None, "created_at": now}
            for f in findings
        ]
        errors = self._client.insert_rows_json(self._findings, rows)
        if errors:
            raise RuntimeError(f"BigQuery insert failed: {errors[:3]}")

    def categories(self) -> dict[str, str]:
        return {r["code"]: r["category"] for r in self.query("categories")}
