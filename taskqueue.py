"""
Task queue abstraction for the CV analysis pipeline.

Two interchangeable backends behind ONE interface so the app never binds to a
specific technology:

  • RedisTaskQueue  – production. Redis list (queue) + hashes (job state) +
                      keys with 24h TTL (results). Fast, shared across the web
                      and worker processes, survives web/worker restarts.
  • SqliteTaskQueue – zero-dependency fallback for local dev or a box without
                      Redis. Same semantics via a WAL SQLite file.

Select with the QUEUE_BACKEND env var ("redis" | "sqlite"). To move to RQ or
Celery later, implement the same handful of methods below and swap the factory.

Job lifecycle:  queued -> running -> done | failed
Results auto-expire after RESULT_TTL seconds (24h). One automatic retry.
"""
from __future__ import annotations

import json
import os
import time
import uuid

RESULT_TTL   = 24 * 60 * 60      # results live 24h
JOB_TTL      = 24 * 60 * 60      # job state lives 24h
HEARTBEAT_KEY = "worker:heartbeat"
MAX_RETRIES  = 1                 # one automatic retry per job


def new_job_id() -> str:
    return uuid.uuid4().hex


# ─────────────────────────── Redis backend ───────────────────────────

class RedisTaskQueue:
    """Redis-backed queue. Queue is a list; each job has a hash + a result key."""

    QUEUE_KEY = "jobs:queued"

    def __init__(self, url: str):
        import redis
        self.r = redis.from_url(url, decode_responses=True)
        # fail fast if unreachable
        self.r.ping()

    # -- producer (web) --
    def enqueue(self, payload: dict) -> str:
        job_id = new_job_id()
        now = time.time()
        pipe = self.r.pipeline()
        pipe.hset(f"job:{job_id}", mapping={
            "status": "queued",
            "payload": json.dumps(payload),
            "retries": 0,
            "enqueued_at": now,
        })
        pipe.expire(f"job:{job_id}", JOB_TTL)
        pipe.rpush(self.QUEUE_KEY, job_id)
        pipe.execute()
        return job_id

    # -- consumer (worker) --
    def dequeue(self, timeout: int = 5):
        res = self.r.blpop(self.QUEUE_KEY, timeout=timeout)
        if not res:
            return None
        _, job_id = res
        raw = self.r.hget(f"job:{job_id}", "payload")
        if raw is None:
            return None
        self.r.hset(f"job:{job_id}", "status", "running")
        return job_id, json.loads(raw)

    def set_done(self, job_id: str, result: dict) -> None:
        pipe = self.r.pipeline()
        pipe.hset(f"job:{job_id}", "status", "done")
        pipe.set(f"result:{job_id}", json.dumps(result), ex=RESULT_TTL)
        pipe.execute()

    def set_failed(self, job_id: str, error: str) -> None:
        self.r.hset(f"job:{job_id}", mapping={"status": "failed", "error": error})

    def retries(self, job_id: str) -> int:
        return int(self.r.hget(f"job:{job_id}", "retries") or 0)

    def requeue(self, job_id: str) -> None:
        self.r.hincrby(f"job:{job_id}", "retries", 1)
        self.r.hset(f"job:{job_id}", "status", "queued")
        self.r.rpush(self.QUEUE_KEY, job_id)

    def get_status(self, job_id: str) -> dict:
        h = self.r.hgetall(f"job:{job_id}")
        if not h:
            return {"status": "unknown"}
        out = {"status": h.get("status", "unknown")}
        if out["status"] == "failed":
            out["error"] = h.get("error", "job failed")
        if out["status"] == "queued":
            out["position"] = self.position(job_id)
        return out

    def get_result(self, job_id: str):
        raw = self.r.get(f"result:{job_id}")
        return json.loads(raw) if raw else None

    def depth(self) -> int:
        return self.r.llen(self.QUEUE_KEY)

    def position(self, job_id: str) -> int:
        try:
            queued = self.r.lrange(self.QUEUE_KEY, 0, -1)
            return queued.index(job_id) + 1
        except ValueError:
            return 0

    # -- worker heartbeat --
    def heartbeat(self) -> None:
        self.r.set(HEARTBEAT_KEY, time.time())

    def last_heartbeat(self):
        v = self.r.get(HEARTBEAT_KEY)
        return float(v) if v else None

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False


# ─────────────────────────── SQLite backend ──────────────────────────

class SqliteTaskQueue:
    """SQLite-backed queue (WAL). Same interface as RedisTaskQueue.
    Good for local dev or a single box without Redis."""

    def __init__(self, path: str):
        import sqlite3
        self.path = path
        self._sqlite3 = sqlite3
        self._init()

    def _conn(self):
        c = self._sqlite3.connect(self.path, timeout=15)
        c.row_factory = self._sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=8000")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id          TEXT PRIMARY KEY,
                    status      TEXT NOT NULL,
                    payload     TEXT,
                    result      TEXT,
                    error       TEXT,
                    retries     INTEGER DEFAULT 0,
                    enqueued_at REAL,
                    expires_at  REAL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, enqueued_at)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    def enqueue(self, payload: dict) -> str:
        job_id = new_job_id()
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id,status,payload,retries,enqueued_at,expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, "queued", json.dumps(payload), 0, now, now + JOB_TTL),
            )
        return job_id

    def dequeue(self, timeout: int = 5):
        deadline = time.time() + timeout
        while True:
            with self._conn() as c:
                row = c.execute(
                    "SELECT id,payload FROM jobs WHERE status='queued' "
                    "ORDER BY enqueued_at LIMIT 1"
                ).fetchone()
                if row:
                    # claim it atomically
                    upd = c.execute(
                        "UPDATE jobs SET status='running' WHERE id=? AND status='queued'",
                        (row["id"],),
                    )
                    if upd.rowcount == 1:
                        return row["id"], json.loads(row["payload"])
            if time.time() >= deadline:
                return None
            time.sleep(0.5)

    def set_done(self, job_id: str, result: dict) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='done', result=?, expires_at=? WHERE id=?",
                (json.dumps(result), time.time() + RESULT_TTL, job_id),
            )

    def set_failed(self, job_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='failed', error=? WHERE id=?", (error, job_id))

    def retries(self, job_id: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT retries FROM jobs WHERE id=?", (job_id,)).fetchone()
            return int(row["retries"]) if row else 0

    def requeue(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='queued', retries=retries+1, enqueued_at=? WHERE id=?",
                (time.time(), job_id),
            )

    def get_status(self, job_id: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT status,error FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return {"status": "unknown"}
        out = {"status": row["status"]}
        if row["status"] == "failed":
            out["error"] = row["error"] or "job failed"
        if row["status"] == "queued":
            out["position"] = self.position(job_id)
        return out

    def get_result(self, job_id: str):
        with self._conn() as c:
            row = c.execute("SELECT result,expires_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not row["result"]:
            return None
        if row["expires_at"] and time.time() > row["expires_at"]:
            return None
        return json.loads(row["result"])

    def depth(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def position(self, job_id: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT enqueued_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return 0
            return c.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='queued' AND enqueued_at<=?",
                (row["enqueued_at"],),
            ).fetchone()[0]

    def heartbeat(self) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)",
                      (HEARTBEAT_KEY, str(time.time())))

    def last_heartbeat(self):
        with self._conn() as c:
            row = c.execute("SELECT v FROM meta WHERE k=?", (HEARTBEAT_KEY,)).fetchone()
            return float(row["v"]) if row else None

    def purge_expired(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM jobs WHERE expires_at IS NOT NULL AND expires_at < ?",
                      (time.time(),))

    def ping(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            return False


# ─────────────────────────── factory ─────────────────────────────────

_QUEUE = None

def get_queue():
    """Singleton queue chosen by env. QUEUE_BACKEND=redis|sqlite."""
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    backend = os.environ.get("QUEUE_BACKEND", "sqlite").lower()
    if backend == "redis":
        _QUEUE = RedisTaskQueue(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    else:
        data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
        _QUEUE = SqliteTaskQueue(os.path.join(data_dir, "queue.db"))
    return _QUEUE
