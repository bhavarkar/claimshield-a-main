"""Durable single-host queue. SQLite transactions arbitrate worker leases.

Requires a persistent local disk. This is not a distributed cloud queue.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class QueueFull(Exception):
    pass


class JobQueue:
    def __init__(self, path: Path, capacity: int = 100, per_owner: int = 3, storage: Any = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.capacity, self.per_owner, self.storage = path, capacity, per_owner, storage
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, available REAL NOT NULL,
                lease REAL NOT NULL DEFAULT 0, token TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
            db.execute('CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status, available, created)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, case_id: str, owner: str, payload: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT owner FROM jobs WHERE status IN ('queued','running')").fetchall()
            if len(pending) >= self.capacity or sum(r['owner'] == owner for r in pending) >= self.per_owner:
                raise QueueFull("The review queue is busy. Please try again shortly.")
            now = time.time()
            db.execute("INSERT INTO jobs (id,owner,payload,status,created,available) VALUES (?,?,?,'queued',?,?)",
                       (case_id, owner, json.dumps(payload), now, now))
        if self.storage:
            job_record = {
                "id": case_id, "owner": owner, "payload": payload, "status": "queued",
                "created": now, "available": now, "lease": 0, "token": None, "attempts": 0,
            }
            try:
                self.storage.put(f"cases/{case_id}/job.json", json.dumps(job_record).encode(), "application/json")
            except Exception:
                pass

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("""SELECT * FROM jobs WHERE (status='queued' AND available<=?)
                OR (status='running' AND lease<?) ORDER BY created LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE jobs SET status='running', lease=?, token=?, attempts=attempts+1 WHERE id=?",
                       (now + 120, token, row['id']))
            job = {**dict(row), 'payload': json.loads(row['payload']), 'token': token,
                   'attempts': row['attempts'] + 1}
        if self.storage:
            try:
                key = f"cases/{row['id']}/job.json"
                data = json.loads(self.storage.get(key)) if self.storage.exists(key) else {}
                data.update({"status": "running", "lease": now + 120, "token": token, "attempts": row['attempts'] + 1})
                self.storage.put(key, json.dumps(data).encode(), "application/json")
            except Exception:
                pass
        return job

    def renew(self, job):
        now = time.time()
        with self.connect() as db:
            db.execute("UPDATE jobs SET lease=? WHERE id=? AND token=? AND status='running'",
                       (now + 120, job['id'], job['token']))
        if self.storage:
            try:
                key = f"cases/{job['id']}/job.json"
                if self.storage.exists(key):
                    data = json.loads(self.storage.get(key))
                    data["lease"] = now + 120
                    self.storage.put(key, json.dumps(data).encode(), "application/json")
            except Exception:
                pass

    def finish(self, job, *, retry=False):
        status = 'queued' if retry else 'complete'
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?, available=?, lease=0 WHERE id=? AND token=?",
                       (status, time.time() + min(30, 2 ** job['attempts']),
                        job['id'], job['token']))
        if self.storage:
            try:
                key = f"cases/{job['id']}/job.json"
                if self.storage.exists(key):
                    data = json.loads(self.storage.get(key))
                    data["status"] = status
                    data["lease"] = 0
                    self.storage.put(key, json.dumps(data).encode(), "application/json")
            except Exception:
                pass

    def status(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT status, attempts FROM jobs WHERE id=?", (case_id,)).fetchone()
            if row:
                return dict(row)
        if self.storage:
            job_key = f"cases/{case_id}/job.json"
            if self.storage.exists(job_key):
                try:
                    data = json.loads(self.storage.get(job_key))
                    return {"status": data.get("status", "queued"), "attempts": data.get("attempts", 0)}
                except Exception:
                    pass
            case_key = f"cases/{case_id}/case.json"
            if self.storage.exists(case_key):
                try:
                    case_data = json.loads(self.storage.get(case_key))
                    st = case_data.get("status")
                    if st == "running":
                        return {"status": "queued", "attempts": 0}
                    if st in ("complete", "failed"):
                        return {"status": st, "attempts": 1}
                except Exception:
                    pass
        return None
