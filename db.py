import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import os
_data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DB_PATH = _data_dir / "jobs.db"
_local = threading.local()


def _retry_locked(fn, retries: int = 5, base_delay: float = 0.2):
    """Retries a write on 'database is locked' — the background job scraper
    holds long write transactions on the same sqlite file."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise


def _conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")   # 64 MB page cache
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456") # 256 MB mmap
        conn.execute("PRAGMA busy_timeout=10000")  # wait 10s on lock
        _local.conn = conn
    return _local.conn


def init_db():
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            company     TEXT    NOT NULL,
            location    TEXT    DEFAULT 'Remote',
            link        TEXT    UNIQUE NOT NULL,
            source      TEXT    NOT NULL,
            scraped_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_source    ON jobs(source);
        CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company);
        CREATE INDEX IF NOT EXISTS idx_jobs_title     ON jobs(title);

        CREATE TABLE IF NOT EXISTS scrape_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT,
            finished_at TEXT,
            total_jobs  INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'running'
        );

        CREATE TABLE IF NOT EXISTS ats_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            score       INTEGER NOT NULL,
            word_count  INTEGER,
            issues      TEXT,
            suggestions TEXT,
            cv_keywords TEXT,
            reviewed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            amount      REAL    NOT NULL,
            direction   TEXT    NOT NULL,
            merchant    TEXT,
            vpa         TEXT,
            bank        TEXT,
            method      TEXT,
            category    TEXT    NOT NULL,
            txn_time    TEXT    NOT NULL,
            raw_sms     TEXT,
            sms_hash    TEXT    UNIQUE,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_txn_time     ON transactions(txn_time);
        CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);
    """)
    c.commit()


def save_jobs(jobs: list[dict]) -> int:
    if not jobs:
        return 0
    c = _conn()
    now = datetime.utcnow().isoformat()
    inserted = 0
    for job in jobs:
        link = (job.get("link") or "").strip()
        title = (job.get("title") or "").strip()
        if not link or not title:
            continue
        try:
            c.execute(
                "INSERT OR IGNORE INTO jobs (title, company, location, link, source, scraped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    title,
                    (job.get("company") or "").strip(),
                    (job.get("location") or "Remote").strip(),
                    link,
                    (job.get("source") or "Unknown").strip(),
                    now,
                ),
            )
            inserted += c.rowcount
        except Exception:
            pass
    c.commit()
    return inserted


_INDIA_CITIES = {
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
    "chennai", "kolkata", "noida", "gurgaon", "gurugram", "ahmedabad", "jaipur",
    "chandigarh", "kochi", "trivandrum", "bhubaneswar", "indore", "coimbatore",
}

def _india_tier(loc: str) -> int:
    l = loc.lower()
    if any(c in l for c in _INDIA_CITIES):
        return 0   # India first
    if "remote" in l:
        return 1   # Remote second
    return 2       # Everything else


def search_jobs(query: str, source: str = None, limit: int = 500) -> list[dict]:
    c = _conn()
    terms = [t.strip().lower() for t in query.split() if t.strip()]
    if not terms:
        sql = "SELECT * FROM jobs"
        params: list = []
    else:
        like_clauses = " OR ".join(
            ["(lower(title) LIKE ? OR lower(company) LIKE ?)" for _ in terms]
        )
        sql = f"SELECT * FROM jobs WHERE ({like_clauses})"
        params = []
        for t in terms:
            params += [f"%{t}%", f"%{t}%"]

    if source:
        sql += " AND source = ?" if "WHERE" in sql else " WHERE source = ?"
        params.append(source)

    sql += " ORDER BY scraped_at DESC LIMIT ?"
    params.append(limit)

    rows = c.execute(sql, params).fetchall()
    jobs = [dict(r) for r in rows]
    # India-first, then remote, then global
    jobs.sort(key=lambda j: _india_tier(j.get("location", "")))
    return jobs


def total_jobs() -> int:
    return _conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def get_last_scrape() -> dict | None:
    row = _conn().execute(
        "SELECT * FROM scrape_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def start_scrape_log() -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO scrape_log (started_at, status) VALUES (?,?)",
        (datetime.utcnow().isoformat(), "running"),
    )
    c.commit()
    return cur.lastrowid


def finish_scrape_log(log_id: int, total: int, status: str = "done"):
    c = _conn()
    c.execute(
        "UPDATE scrape_log SET finished_at=?, total_jobs=?, status=? WHERE id=?",
        (datetime.utcnow().isoformat(), total, status, log_id),
    )
    c.commit()


def save_ats_review(score: int, word_count: int, issues: list, suggestions: list, cv_keywords: list):
    c = _conn()
    c.execute(
        "INSERT INTO ats_reviews (score, word_count, issues, suggestions, cv_keywords, reviewed_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            score,
            word_count,
            json.dumps(issues),
            json.dumps(suggestions),
            json.dumps(cv_keywords),
            datetime.utcnow().isoformat(),
        ),
    )
    c.commit()


def get_last_ats_review() -> dict | None:
    row = _conn().execute(
        "SELECT * FROM ats_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    r = dict(row)
    r["issues"]      = json.loads(r["issues"] or "[]")
    r["suggestions"] = json.loads(r["suggestions"] or "[]")
    r["cv_keywords"] = json.loads(r["cv_keywords"] or "[]")
    return r


def clear_old_jobs(keep_days: int = 2):
    c = _conn()
    c.execute(
        "DELETE FROM jobs WHERE scraped_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    c.commit()


# ───────────────────────── Payment tracker ───────────────────────

def save_transaction(txn: dict) -> bool:
    """Insert a parsed transaction. Returns False if it's a duplicate (same sms_hash)."""
    c = _conn()

    def _do():
        c.execute(
            "INSERT INTO transactions "
            "(amount, direction, merchant, vpa, bank, method, category, txn_time, raw_sms, sms_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                txn["amount"],
                txn["direction"],
                txn.get("merchant"),
                txn.get("vpa"),
                txn.get("bank"),
                txn.get("method"),
                txn["category"],
                txn["txn_time"],
                txn.get("raw_sms"),
                txn.get("sms_hash"),
                datetime.utcnow().isoformat(),
            ),
        )
        c.commit()

    try:
        _retry_locked(_do)
        return True
    except sqlite3.IntegrityError:
        return False


def get_transactions(month: str = None, category: str = None, limit: int = 1000) -> list[dict]:
    c = _conn()
    sql = "SELECT * FROM transactions"
    clauses, params = [], []
    if month:
        clauses.append("strftime('%Y-%m', txn_time) = ?")
        params.append(month)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY txn_time DESC LIMIT ?"
    params.append(limit)
    rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_monthly_summary(month: str) -> dict:
    c = _conn()
    rows = c.execute(
        "SELECT category, SUM(amount) AS total, COUNT(*) AS cnt "
        "FROM transactions WHERE strftime('%Y-%m', txn_time) = ? AND direction = 'debit' "
        "GROUP BY category ORDER BY total DESC",
        (month,),
    ).fetchall()
    categories = [dict(r) for r in rows]
    total_debit = sum(r["total"] for r in categories)
    total_credit = c.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions "
        "WHERE strftime('%Y-%m', txn_time) = ? AND direction = 'credit'",
        (month,),
    ).fetchone()[0]
    return {
        "month": month,
        "categories": categories,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "txn_count": sum(r["cnt"] for r in categories),
    }


def update_transaction_category(txn_id: int, category: str):
    c = _conn()
    _retry_locked(lambda: (
        c.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, txn_id)),
        c.commit(),
    ))


def delete_transaction(txn_id: int):
    c = _conn()
    _retry_locked(lambda: (
        c.execute("DELETE FROM transactions WHERE id = ?", (txn_id,)),
        c.commit(),
    ))


def available_months() -> list[str]:
    c = _conn()
    rows = c.execute(
        "SELECT DISTINCT strftime('%Y-%m', txn_time) AS m FROM transactions ORDER BY m DESC"
    ).fetchall()
    return [r["m"] for r in rows if r["m"]]
