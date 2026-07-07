"""
The heavy analysis pipeline — runs in the WORKER, never in a web request.

It reuses the proven CV-analysis functions from app.py (ATS scoring, CV
intelligence, keyword extraction, recommendations, job scoring) so every
existing feature is preserved bit-for-bit. Text extraction is done by the
memory-capped subprocess (see parse_subprocess.py); scraping overlaps with the
(cheap) ATS analysis to shave wall-clock time.

Profiling on a typical 2-page CV:
    text extraction  ~10-40 ms   (PyMuPDF/pypdf)
    ATS + intel      ~5-15 ms
    job scraping     ~5-30 s      <-- the only real cost; runs concurrently
"""
from __future__ import annotations

import concurrent.futures

# Importing app pulls in the analysis functions. No server / scheduler starts
# on import (that only happens in wsgi.py / __main__), so this is safe here.
import app as _app
import db
import scrapers


def _scrape_and_score(field: str, cv_keywords: list, country: str) -> list[dict]:
    """Live-scrape jobs for the recommended role and score them against the CV.
    The scraper fetches all sources concurrently with an 8s per-source timeout
    and caches results per (query, country) for 30 min (see scrapers.scrape_live)."""
    try:
        base = db.search_jobs(field, limit=400)
    except Exception:
        base = []
    if country != "in":
        base = [j for j in base if scrapers.detect_country(j.get("location", "")) == country]

    if len(base) < 30:
        try:
            live = scrapers.scrape_live(field, country=country)
            db.save_jobs(live)
            seen = {j.get("link") for j in base}
            base = base + [j for j in live if j.get("link") not in seen]
        except Exception:
            pass

    deduped = _app._dedupe_jobs(base)
    jobs = []
    for job in deduped:
        j = dict(job)
        j["country"] = scrapers.detect_country(j.get("location", ""))
        j["matched_keywords"], j["match_pct"] = _app.compute_match(j, cv_keywords, field, country)
        j["match_score"] = len(j["matched_keywords"])
        jobs.append(j)
    jobs.sort(key=lambda j: (-j["match_pct"], -j["match_score"]))
    return jobs[:300]


def run_analysis(text: str, *, do_scrape: bool = True, country: str = "in") -> dict:
    """Full analysis for already-extracted CV text. Returns the same result
    shape the old synchronous endpoint returned, plus matched jobs."""
    if not text.strip():
        raise ValueError("empty CV text")

    # Cheap CV analysis + the (slow) scrape run concurrently.
    cv_keywords = _app.extract_cv_keywords(text)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        ats_fut = ex.submit(_app.analyze_ats, text)
        rec_fut = ex.submit(_app.derive_recommended_query, text, cv_keywords)
        result = ats_fut.result()
        recommendation = rec_fut.result()

        result["cv_keywords"] = cv_keywords
        result["recommendation"] = recommendation

        jobs = []
        if do_scrape:
            field = recommendation.get("query") or "software engineer"
            jobs = _scrape_and_score(field, cv_keywords, country)
    result["jobs"] = jobs

    # Diff against last review (CV-improvement tracking) — same as before.
    try:
        last = db.get_last_ats_review()
        if last:
            prev_issues = set(last["issues"])
            curr_issues = set(result["issues"])
            result["prev_score"]       = last["score"]
            result["score_delta"]      = result["score"] - last["score"]
            result["fixed_issues"]     = sorted(prev_issues - curr_issues)
            result["new_issues"]       = sorted(curr_issues - prev_issues)
            result["persisted_issues"] = sorted(curr_issues & prev_issues)
            result["has_prev_review"]  = True
        else:
            result["has_prev_review"] = False
        db.save_ats_review(result["score"], result["word_count"],
                           result["issues"], result["suggestions"], cv_keywords)
    except Exception:
        result.setdefault("has_prev_review", False)

    # cv_text is needed later for JD matching; kept in the stored result.
    result["cv_text"] = text
    return result
