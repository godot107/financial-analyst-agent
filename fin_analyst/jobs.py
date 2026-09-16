"""Jobs for the HTTP service, and what they have spent.

A memo outlasts any sensible HTTP request, so the service takes a job, answers
at once, and does the work in the background. This module is the ledger: which
jobs exist, what state they're in, and how much each caller has committed today.

"Committed" is the heart of it. A queued job hasn't spent anything yet, but if
the cap only counted finished jobs, a burst of submissions would all pass the
check and then all spend. So a job counts at its estimate until it finishes,
and at its real cost after.

Two stores share one interface. `JobStore` is SQLite, for one process on one
machine (Phase A). `S3JobStore` keeps each job as an S3 object, for Lambda
(Phase B), where no process lives long enough to hold a database.
"""

import json
import re
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


# A job id carries its creation date, so the S3 store can find a job without an
# index: "20260916" followed by 24 hex characters.
JOB_ID = re.compile(r"^\d{8}[0-9a-f]{24}$")


def new_job_id() -> str:
    return now().strftime("%Y%m%d") + uuid.uuid4().hex[:24]


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
            id=new_job_id(),
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

    def claim(self, job_id: str) -> Job | None:
        """Mark one specific job running, if it is still queued."""
        with self.lock:
            updated = self.connection.execute(
                "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'", (job_id,)
            ).rowcount
            self.connection.commit()
        return self.get(job_id) if updated else None

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


class S3JobStore:
    """The same ledger, one S3 object per job, for Lambda.

    Objects live at jobs/<YYYY-MM-DD>/<id>.json, so today's spend is one listing
    of today's prefix. Volume is tens of jobs a day, so reading each is fine.

    Two concurrent submissions can both pass a cap check before either is
    written; at this volume, with one caller, that window is accepted rather
    than paid for with a lock service.
    """

    def __init__(self, bucket: str, client, prefix: str = ""):
        self.bucket = bucket
        self.client = client
        self.prefix = prefix

    def _key(self, job_id: str) -> str:
        day = f"{job_id[:4]}-{job_id[4:6]}-{job_id[6:8]}"
        return f"{self.prefix}jobs/{day}/{job_id}.json"

    def _put(self, job: Job) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(job.id),
            Body=job.model_dump_json().encode(),
            ContentType="application/json",
        )

    def add(self, kind: str, key_name: str, request: dict, estimate_usd: float) -> Job:
        job = Job(
            id=new_job_id(),
            kind=kind,
            key_name=key_name,
            status="queued",
            request=request,
            estimate_usd=estimate_usd,
            created_at=now().isoformat(),
        )
        self._put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        # The id becomes part of an object key, so anything that isn't exactly
        # an id is refused before it gets near S3.
        if not JOB_ID.match(job_id or ""):
            return None
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=self._key(job_id))["Body"].read()
        except Exception as missing:
            code = getattr(missing, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        return Job.model_validate_json(body)

    def claim(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None or job.status != "queued":
            return None
        running = job.model_copy(update={"status": "running"})
        self._put(running)
        return running

    def claim_next(self) -> Job | None:
        queued = [job for job in self._today() if job.status == "queued"]
        return self.claim(min(queued, key=lambda j: j.created_at).id) if queued else None

    def finish(self, job_id, status, cost_usd, memo=None, result=None, error=None) -> None:
        job = self.get(job_id)
        if job is None:
            return
        self._put(
            job.model_copy(
                update={
                    "status": status,
                    "cost_usd": cost_usd,
                    "memo": memo,
                    "result": result,
                    "error": error,
                    "finished_at": now().isoformat(),
                }
            )
        )

    def _today(self) -> list[Job]:
        prefix = f"{self.prefix}jobs/{now().date().isoformat()}/"
        jobs, token = [], None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                body = self.client.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()
                jobs.append(Job.model_validate_json(body))
            if not page.get("IsTruncated"):
                return jobs
            token = page.get("NextContinuationToken")

    def committed_today(self, key_name: str | None = None) -> float:
        return sum(
            job.cost_usd if job.status in ("done", "failed") else job.estimate_usd
            for job in self._today()
            if key_name is None or job.key_name == key_name
        )
