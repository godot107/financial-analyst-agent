"""Jobs for the HTTP service, and what they have spent.

A memo outlasts any sensible HTTP request, so the service takes a job, answers
at once, and does the work in the background. This module is the ledger: which
jobs exist, what state they're in, and how much each caller has committed today.

"Committed" is the heart of it. A queued job hasn't spent anything yet, but if
the cap only counted finished jobs, a burst of submissions would all pass the
check and then all spend. So a job counts at its estimate until it finishes,
and at its real cost after.

SQLite, because one process and one worker is the whole of Phase A.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Status = Literal["queued", "running", "done", "failed"]


class Job(BaseModel):
    id: str
    kind: Literal["memo", "batch"]
    key_name: str  # which caller submitted it; only they may read it
    status: Status
    request: dict
    estimate_usd: float
    cost_usd: float = 0.0
    memo: str | None = None
    result: dict | None = None
    error: str | None = None
    created_at: str
    finished_at: str | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def next_reset(moment: datetime | None = None) -> str:
    """When today's caps reset: the next UTC midnight."""
    moment = moment or now()
    return (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class JobStore:
    def __init__(self, path: Path | str):
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # The web handlers and the worker thread share one connection.
        self.lock = threading.Lock()
        with self.lock:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT, key_name TEXT, status TEXT,
                    request TEXT, estimate_usd REAL, cost_usd REAL, memo TEXT,
                    result TEXT, error TEXT, created_at TEXT, finished_at TEXT)"""
            )
            self.connection.commit()

    def _job(self, row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        data = dict(row)
        data["request"] = json.loads(data["request"])
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return Job(**data)

    def add(self, kind: str, key_name: str, request: dict, estimate_usd: float) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            key_name=key_name,
            status="queued",
            request=request,
            estimate_usd=estimate_usd,
            created_at=now().isoformat(),
        )
        with self.lock:
            self.connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.kind, job.key_name, job.status, json.dumps(job.request),
                 job.estimate_usd, 0.0, None, None, None, job.created_at, None),
            )
            self.connection.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row)

    def claim_next(self) -> Job | None:
        """The oldest queued job, marked running in the same step so it runs once."""
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.connection.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
            self.connection.commit()
        job = self._job(row)
        return job.model_copy(update={"status": "running"})

    def finish(
        self,
        job_id: str,
        status: Status,
        cost_usd: float,
        memo: str | None = None,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                """UPDATE jobs SET status = ?, cost_usd = ?, memo = ?, result = ?, error = ?,
                   finished_at = ? WHERE id = ?""",
                (status, cost_usd, memo, json.dumps(result) if result else None, error,
                 now().isoformat(), job_id),
            )
            self.connection.commit()

    def committed_today(self, key_name: str | None = None) -> float:
        """Today's spend: real cost for finished jobs, the estimate for the rest."""
        today = now().date().isoformat()
        query = """SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'failed')
                                            THEN cost_usd ELSE estimate_usd END), 0)
                   FROM jobs WHERE substr(created_at, 1, 10) = ?"""
        params: list = [today]
        if key_name is not None:
            query += " AND key_name = ?"
            params.append(key_name)
        with self.lock:
            return float(self.connection.execute(query, params).fetchone()[0])
