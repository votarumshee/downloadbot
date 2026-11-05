from __future__ import annotations

import hashlib
import json
import time
import uuid

import psycopg
from psycopg.rows import dict_row

from downloadbot.config import Settings


class QueueFull(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class AccountUnavailable(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, encrypted_cookies TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL, last_used_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_error_code TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL, platform TEXT NOT NULL,
    account_id TEXT REFERENCES accounts(id), max_height INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
      ('queued','running','succeeded','failed','cancelled','expired')),
    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION, finished_at DOUBLE PRECISION, expires_at DOUBLE PRECISION,
    available_at DOUBLE PRECISION NOT NULL, lease_until DOUBLE PRECISION,
    worker_id TEXT, attempt INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    artifact_path TEXT, title TEXT, ext TEXT, size_bytes BIGINT, duration DOUBLE PRECISION,
    error_code TEXT, error_message TEXT, idempotency_key TEXT, fingerprint TEXT NOT NULL,
    UNIQUE (owner, idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs (owner, created_at);
CREATE INDEX IF NOT EXISTS jobs_account_idx ON jobs (account_id, status);
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, heartbeat_at DOUBLE PRECISION NOT NULL);
"""


class Store:
    """Short PostgreSQL transactions; no connection remains open during a download."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self):
        return psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row,
            connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=5000",
        )

    def initialize(self):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(72341900)")
            db.execute(SCHEMA)

    def ready(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT MAX(heartbeat_at) AS last FROM workers").fetchone()
            return bool(row["last"] and row["last"] >= time.time() - self.settings.lease_seconds)

    def worker_ping(self, worker_id: str):
        with self.connect() as db:
            db.execute("INSERT INTO workers VALUES (%s,%s) ON CONFLICT (id) DO UPDATE "
                       "SET heartbeat_at=EXCLUDED.heartbeat_at", (worker_id, time.time()))

    def create_job(self, *, owner: str, url: str, platform: str, account_id: str | None = None,
                   max_height: int = 720, idempotency_key: str | None = None) -> tuple[dict, bool]:
        fingerprint = hashlib.sha256(json.dumps([url, account_id, max_height]).encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            # Serialize only submission, enforcing global and per-client quotas atomically.
            db.execute("SELECT pg_advisory_xact_lock(72341901)")
            if idempotency_key:
                existing = db.execute("SELECT * FROM jobs WHERE owner=%s AND idempotency_key=%s",
                                      (owner, idempotency_key)).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise IdempotencyConflict
                    return existing, False
            if account_id:
                account = db.execute("SELECT * FROM accounts WHERE id=%s FOR SHARE", (account_id,)).fetchone()
                if not account or account["platform"] != platform or account["status"] != "active":
                    raise AccountUnavailable
            counts = db.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE owner=%s) AS own "
                                "FROM jobs WHERE status IN ('queued','running')", (owner,)).fetchone()
            if counts["total"] >= self.settings.max_active_jobs or counts["own"] >= self.settings.max_active_jobs_per_client:
                raise QueueFull
            job = db.execute("INSERT INTO jobs "
                             "(id,owner,url,platform,account_id,max_height,created_at,updated_at,available_at,"
                             "idempotency_key,fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                             (str(uuid.uuid4()), owner, url, platform, account_id, max_height, now, now, now,
                              idempotency_key, fingerprint)).fetchone()
            return job, True

    def get_job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def list_jobs(self, owner: str | None, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE (%s::text IS NULL OR owner=%s) "
                              "ORDER BY created_at DESC LIMIT %s", (owner, owner, limit)).fetchall()

    def request_cancel(self, job_id: str) -> dict | None:
        now = time.time()
        with self.connect() as db:
            db.execute("UPDATE jobs SET cancel_requested=TRUE, updated_at=%s, "
                       "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
                       "finished_at=CASE WHEN status='queued' THEN %s ELSE finished_at END "
                       "WHERE id=%s AND status IN ('queued','running')", (now, now, job_id))
            return db.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def claim(self, worker_id: str) -> dict | None:
        now = time.time()
        with self.connect() as db:
            # Stale owners lose their lease; completion updates are fenced by worker_id.
            db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' "
                       "WHEN attempt >= %s THEN 'failed' ELSE 'queued' END, worker_id=NULL, lease_until=NULL, "
                       "updated_at=%s, available_at=%s, error_code='worker_lost', "
                       "error_message='Worker lease expired', "
                       "finished_at=CASE WHEN cancel_requested OR attempt >= %s THEN %s ELSE NULL END "
                       "WHERE status='running' AND lease_until < %s",
                       (self.settings.max_attempts, now, now, self.settings.max_attempts, now, now))
            db.execute("UPDATE jobs SET status='failed',error_code='account_unavailable',"
                       "error_message='Account is disabled or requires new cookies',finished_at=%s,updated_at=%s "
                       "WHERE status='queued' AND account_id IN (SELECT id FROM accounts WHERE status<>'active')",
                       (now, now))
            jobs = db.execute("SELECT j.* FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
                              "WHERE j.status='queued' AND j.available_at<=%s AND NOT j.cancel_requested "
                              "AND (j.account_id IS NULL OR (a.status='active' AND a.last_used_at<=%s "
                              "AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.account_id=j.account_id "
                              "AND r.status='running'))) ORDER BY j.created_at LIMIT 20 "
                              "FOR UPDATE OF j SKIP LOCKED",
                              (now, now - self.settings.account_cooldown_seconds)).fetchall()
            for job in jobs:
                if job["account_id"]:
                    account = db.execute("SELECT * FROM accounts WHERE id=%s FOR UPDATE SKIP LOCKED",
                                         (job["account_id"],)).fetchone()
                    if not account or account["status"] != "active":
                        continue
                    # A competing claim may have committed since the candidate query's snapshot.
                    busy = db.execute("SELECT 1 FROM jobs WHERE account_id=%s AND status='running' LIMIT 1",
                                      (job["account_id"],)).fetchone()
                    if busy or account["last_used_at"] > now - self.settings.account_cooldown_seconds:
                        continue
                    db.execute("UPDATE accounts SET last_used_at=%s WHERE id=%s", (now, job["account_id"]))
                return db.execute("UPDATE jobs SET status='running',attempt=attempt+1,worker_id=%s,"
                                  "lease_until=%s,started_at=%s,updated_at=%s "
                                  "WHERE id=%s RETURNING *",
                                  (worker_id, now + self.settings.lease_seconds, now, now, job["id"])).fetchone()
        return None

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        now = time.time()
        with self.connect() as db:
            row = db.execute("UPDATE jobs SET lease_until=%s WHERE id=%s AND worker_id=%s "
                             "AND status='running' AND NOT cancel_requested AND lease_until>=%s "
                             "RETURNING id", (now + self.settings.lease_seconds, job_id, worker_id, now)).fetchone()
            return row is not None

    def finish(self, job_id: str, worker_id: str, *, artifact_path: str, title: str, ext: str,
               size_bytes: int, duration: float | None) -> bool:
        now = time.time()
        with self.connect() as db:
            row = db.execute("UPDATE jobs SET status='succeeded',artifact_path=%s,title=%s,ext=%s,"
                             "size_bytes=%s,duration=%s,finished_at=%s,updated_at=%s,expires_at=%s,"
                             "lease_until=NULL,error_code=NULL,error_message=NULL WHERE id=%s AND worker_id=%s "
                             "AND status='running' AND NOT cancel_requested AND lease_until>=%s RETURNING id",
                             (artifact_path, title[:500], ext, size_bytes, duration, now, now,
                              now + self.settings.retention_seconds, job_id, worker_id, now)).fetchone()
            return row is not None

    def fail(self, job_id: str, worker_id: str, code: str, message: str, retryable: bool = False):
        now = time.time()
        with self.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=%s AND worker_id=%s AND status='running' "
                             "AND lease_until>=%s FOR UPDATE", (job_id, worker_id, now)).fetchone()
            if not job:
                return
            if job["cancel_requested"] or code == "cancelled":
                status = "cancelled"
            elif retryable and job["attempt"] < self.settings.max_attempts:
                status = "queued"
            else:
                status = "failed"
            db.execute("UPDATE jobs SET status=%s,error_code=%s,error_message=%s,updated_at=%s,"
                       "finished_at=%s,available_at=%s,lease_until=NULL,worker_id=NULL WHERE id=%s",
                       (status, code, message, now, None if status == "queued" else now,
                        now + min(60, 5 * 2**job["attempt"]), job_id))
            if code == "auth_required" and job["account_id"]:
                # Don't invalidate a session that an operator rotated while this job was running.
                db.execute("UPDATE accounts SET status='needs_refresh',last_error_code=%s,updated_at=%s "
                           "WHERE id=%s AND updated_at<=%s", (code, now, job["account_id"], job["started_at"]))

    def release(self, job_id: str, worker_id: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,"
                       "worker_id=NULL,lease_until=NULL,available_at=%s,updated_at=%s "
                       "WHERE id=%s AND worker_id=%s AND status='running' AND lease_until>=%s",
                       (time.time(), time.time(), job_id, worker_id, time.time()))

    def expire(self) -> list[dict]:
        with self.connect() as db:
            return db.execute("UPDATE jobs SET status='expired',updated_at=%s WHERE status='succeeded' "
                              "AND expires_at<=%s RETURNING *", (time.time(), time.time())).fetchall()

    def save_account(self, account_id: str, platform: str, encrypted_cookies: str) -> dict:
        now = time.time()
        with self.connect() as db:
            # Account ids cannot silently change platform after being referenced by jobs.
            row = db.execute("INSERT INTO accounts (id,platform,encrypted_cookies,created_at,updated_at) "
                             "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
                             "encrypted_cookies=EXCLUDED.encrypted_cookies,status='active',"
                             "updated_at=EXCLUDED.updated_at,last_error_code=NULL "
                             "WHERE accounts.platform=EXCLUDED.platform RETURNING *",
                             (account_id, platform, encrypted_cookies, now, now)).fetchone()
            if row is None:
                raise ValueError("Account platform cannot be changed")
            return row

    def get_account(self, account_id: str) -> dict | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()

    def list_accounts(self) -> list[dict]:
        with self.connect() as db:
            return db.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def disable_account(self, account_id: str):
        with self.connect() as db:
            db.execute("UPDATE accounts SET status='disabled',encrypted_cookies='',updated_at=%s WHERE id=%s",
                       (time.time(), account_id))
            db.execute("UPDATE jobs SET cancel_requested=TRUE WHERE account_id=%s AND status='running'",
                       (account_id,))
