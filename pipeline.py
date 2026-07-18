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
import os

# Importing app pulls in the analysis functions. No server / scheduler starts
# on import (that only happens in wsgi.py / __main__), so this is safe here.
import app as _app
import db
import scrapers


def _scrape_and_score(field: str, cv_keywords: list, country: str,
                      user_level: str = "") -> list[dict]:
    """Score cached (+ optionally freshly-scraped) jobs against the CV and rank
    them, factoring in the candidate's EXPERIENCE LEVEL so a fresher isn't shown
    director roles and a senior isn't shown internships.
    Cached per (query, country) for 30 min (see scrapers.scrape_live)."""
    try:
        base = db.search_jobs(field, limit=400)
    except Exception:
        base = []
    if country != "in":
        base = [j for j in base if scrapers.detect_country(j.get("location", "")) == country]

    # For niche roles the cache may not cover, live-scrape the FAST query-
    # specific sources (Internshala/Shine/RemoteOK/WWR) and merge with cache.
    # Greenhouse is deliberately excluded here (its 250-company fan-out is what
    # made uploads take 1-2 min) — that coverage comes from the background
    # full-scrape instead. A hard time budget keeps this bounded so a single
    # slow source can never blow past the target latency.
    if len(base) < 30 and os.environ.get("ANALYSIS_LIVE_SCRAPE", "1") == "1":
        budget = int(os.environ.get("ANALYSIS_SCRAPE_BUDGET", "9"))   # seconds
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(scrapers.scrape_live, field, country)
            live = fut.result(timeout=budget)
            db.save_jobs(live)
            seen = {j.get("link") for j in base}
            base = base + [j for j in live if j.get("link") not in seen]
        except concurrent.futures.TimeoutError:
            pass   # over budget — return the cache now; scrape_live finishes in
                   # the background and caches itself, so the next hit is instant
        except Exception:
            pass
        finally:
            # wait=False: never block the user on a slow source past the budget
            ex.shutdown(wait=False)

    deduped = _app._dedupe_jobs(base)
    jobs = []
    for job in deduped:
        j = dict(job)
        j["country"] = scrapers.detect_country(j.get("location", ""))
        j["matched_keywords"], j["match_pct"] = _app.compute_match(j, cv_keywords, field, country)
        j["match_score"] = len(j["matched_keywords"])
        j["level"] = _app.detect_job_level(j.get("title", ""))
        # Seniority-aware rank score: each level of gap from the candidate costs
        # 18 points, so aligned roles rise to the top without hiding everything.
        pen = _app.level_penalty(user_level, j.get("title", "")) if user_level else 0
        # Source tier: career pages (Greenhouse/Lever) rise; Internshala sinks.
        j["_rank"] = j["match_pct"] - 18 * pen + _app.source_boost(job.get("source"))
        jobs.append(j)
    jobs.sort(key=lambda j: (-j["_rank"], -j["match_score"]))
    for j in jobs:
        j.pop("_rank", None)
    return jobs[:300]


_VALID_LEVELS = {"fresher", "junior", "mid", "senior", "lead"}


def run_analysis(text: str, *, do_scrape: bool = True, country: str = "in",
                 user_level: str = "", desired_role: str = "") -> dict:
    """Full analysis for already-extracted CV text. The USER's manual choices
    take priority: `user_level` (their self-declared experience) drives job
    ranking and `desired_role` drives the search query. Falls back to
    CV-derived values when not provided."""
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
            # User's typed role wins; else the CV-recommended query.
            field = (desired_role or "").strip() or recommendation.get("query") or "software engineer"
            # User's self-declared level wins; else the (fixed) CV estimate.
            lvl = (user_level or "").strip().lower()
            if lvl not in _VALID_LEVELS:
                lvl = (recommendation.get("experience") or {}).get("level", "")
            jobs = _scrape_and_score(field, cv_keywords, country, lvl)
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
