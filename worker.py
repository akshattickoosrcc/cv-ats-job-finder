"""
Worker process — pulls analysis jobs off the queue and runs them.

Run as its own process (systemd service on the VPS, or `python worker.py`):
    QUEUE_BACKEND=redis REDIS_URL=... python worker.py

Responsibilities:
  • heartbeat every 30s (so /health can tell if the worker is alive)
  • dequeue job -> extract text in a MEMORY-CAPPED subprocess -> run pipeline
  • store result (24h TTL) ; delete the uploaded file (success OR failure)
  • one automatic retry, then mark failed so the UI stops spinning
  • log every failed job
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time

from taskqueue import get_queue, MAX_RETRIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("worker")

PARSE_TIMEOUT = int(os.environ.get("PARSE_TIMEOUT", "25"))   # seconds
HERE = os.path.dirname(os.path.abspath(__file__))


def _heartbeat_loop(q):
    while True:
        try:
            q.heartbeat()
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
        time.sleep(30)


def _scrape_loop():
    """Keep the job DB warm so /api/search-jobs (DB-only) always has results.
    Runs a scrape on startup, then every SCRAPE_INTERVAL_HOURS. This lives in
    the WORKER so it never touches the web server's latency."""
    import scrapers
    interval = int(os.environ.get("SCRAPE_INTERVAL_HOURS", "6")) * 3600
    mode = os.environ.get("SCRAPE_MODE", "full")   # "full" or "light"
    time.sleep(int(os.environ.get("SCRAPE_STARTUP_DELAY", "20")))
    while True:
        try:
            log.info("periodic scrape starting (mode=%s)", mode)
            (scrapers.run_render_scrape if mode == "light" else scrapers.run_full_scrape)()
            log.info("periodic scrape done")
        except Exception as e:
            log.warning("periodic scrape failed: %s", e)
        time.sleep(interval)


def _extract_text_capped(pdf_path: str) -> str:
    """Extract text in a separate, memory-limited process. Raises on failure."""
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "parse_subprocess.py"), pdf_path],
        capture_output=True, text=True, timeout=PARSE_TIMEOUT, cwd=HERE,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError(f"parser produced no output (exit {proc.returncode})")
    data = json.loads(out)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["text"]


def _process(q, job_id: str, payload: dict) -> None:
    pdf_path = payload.get("pdf_path")
    country  = payload.get("country", "in")
    try:
        text = _extract_text_capped(pdf_path)
        if not text.strip():
            raise RuntimeError("Could not extract text — is the PDF a scanned image?")

        import pipeline
        result = pipeline.run_analysis(text, do_scrape=True, country=country)
        q.set_done(job_id, result)
        log.info("job %s done (score=%s, %d jobs)", job_id,
                 result.get("score"), len(result.get("jobs", [])))
    except subprocess.TimeoutExpired:
        _fail_or_retry(q, job_id, "parsing timed out")
    except Exception as e:
        _fail_or_retry(q, job_id, str(e))
    finally:
        # Delete the uploaded file immediately, success OR failure.
        try:
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception:
            pass


def _fail_or_retry(q, job_id: str, error: str) -> None:
    if q.retries(job_id) < MAX_RETRIES:
        log.warning("job %s error (%s) — retrying", job_id, error)
        q.requeue(job_id)
    else:
        log.error("job %s FAILED after retry: %s", job_id, error)
        q.set_failed(job_id, "Something went wrong — please re-upload")


def _recover_stale_running(q) -> None:
    """On startup, requeue any jobs left in 'running' state by a previously
    crashed worker. Without this, those jobs spin forever as 'running' and
    the user sees the loading spinner until the 5-minute frontend timeout."""
    try:
        if hasattr(q, "r"):
            # Redis: SCAN all job:* hashes for status=running
            recovered = 0
            for key in q.r.scan_iter("job:*"):
                if q.r.hget(key, "status") == "running":
                    job_id = key.split(":", 1)[1]
                    log.warning("recovering stale running job %s", job_id)
                    q.requeue(job_id)
                    recovered += 1
            if recovered:
                log.info("recovered %d stale job(s) from previous crash", recovered)
        else:
            # SQLite fallback
            import sqlite3
            conn = sqlite3.connect(q.path)
            rows = conn.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            if rows:
                conn.execute("UPDATE jobs SET status='queued', retries=retries+1 WHERE status='running'")
                conn.commit()
                log.info("recovered %d stale job(s) from previous crash (SQLite)", len(rows))
            conn.close()
    except Exception as exc:
        log.warning("stale-job recovery failed (non-fatal): %s", exc)


def main():
    q = get_queue()
    concurrency = max(1, int(os.environ.get("WORKER_CONCURRENCY", "3")))
    log.info("worker started (queue=%s, concurrency=%d)", type(q).__name__, concurrency)
    _recover_stale_running(q)
    threading.Thread(target=_heartbeat_loop, args=(q,), daemon=True).start()
    if os.environ.get("WORKER_SCRAPE", "1") == "1":
        threading.Thread(target=_scrape_loop, daemon=True).start()

    # Run N consumer threads so many uploads process IN PARALLEL instead of
    # queueing behind one. A single job is ~1s (cache) to ~9s (niche live
    # scrape) and is I/O-bound (subprocess parse + network scrape), so threads
    # give real parallelism here. This is what stops a burst of 20 evening
    # users from waiting single-file behind each other.
    consumers = [
        threading.Thread(target=_consume_loop, args=(q,), daemon=True)
        for _ in range(concurrency)
    ]
    for t in consumers:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("worker stopping")


def _consume_loop(q) -> None:
    """One consumer: pull a job, process it, repeat. Multiple of these run
    concurrently (WORKER_CONCURRENCY). Redis BLPOP / the SQLite claim-update
    are atomic, so no two consumers ever grab the same job."""
    while True:
        try:
            item = q.dequeue(timeout=5)
            if item is None:
                if hasattr(q, "purge_expired"):
                    q.purge_expired()
                continue
            job_id, payload = item
            log.info("picked up job %s", job_id)
            _process(q, job_id, payload)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("consumer loop error: %s", e)
            time.sleep(1)


if __name__ == "__main__":
    main()
