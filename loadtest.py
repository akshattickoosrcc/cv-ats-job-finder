#!/usr/bin/env python3
"""
Load test for the CV-analyzer backend.

Simulates a viral-reel burst:
  • 100 concurrent "visitors" hitting GET /health (the landing-page warm-up)
  • 20 simultaneous CV uploads to POST /api/analyze, then polls them to done

Each virtual user sends a distinct X-Forwarded-For so the per-IP guard treats
them as different people (works when the API trusts one proxy hop via ProxyFix;
behind nginx the real client IP wins, so run this against the gunicorn port or a
staging box without the guard-defeating proxy).

Usage:
    python3 loadtest.py https://api.yourdomain.com sample_cv.pdf
    python3 loadtest.py http://localhost:8000 sample_cv.pdf --visitors 100 --uploads 20

Plain asyncio + the stdlib-friendly `requests` (already a dependency) run in a
thread pool — no extra packages to install.
"""
import argparse
import asyncio
import statistics
import time

import requests


def _pctl(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[k]


def _fake_ip(i):
    return f"203.0.{(i // 256) % 256}.{i % 256}"   # TEST-NET-3 range


def _health(base, i):
    t = time.perf_counter()
    try:
        r = requests.get(f"{base}/health", headers={"X-Forwarded-For": _fake_ip(i)}, timeout=30)
        return r.status_code, (time.perf_counter() - t)
    except Exception:
        return 0, (time.perf_counter() - t)


def _upload(base, pdf, i):
    t = time.perf_counter()
    try:
        with open(pdf, "rb") as f:
            r = requests.post(
                f"{base}/api/analyze",
                files={"cv": ("cv.pdf", f, "application/pdf")},
                data={"country": "in"},
                headers={"X-Forwarded-For": _fake_ip(1000 + i)},
                timeout=30,
            )
        dt = time.perf_counter() - t
        jid = r.json().get("job_id") if r.headers.get("content-type", "").startswith("application/json") else None
        return r.status_code, dt, jid, _fake_ip(1000 + i)
    except Exception as e:
        return 0, (time.perf_counter() - t), None, str(e)[:40]


def _poll(base, jid, ip, timeout=180):
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        try:
            r = requests.get(f"{base}/api/status/{jid}", headers={"X-Forwarded-For": ip}, timeout=15)
            st = r.json().get("status")
            if st in ("done", "failed"):
                return st, time.perf_counter() - t
        except Exception:
            pass
        time.sleep(3)
    return "timeout", time.perf_counter() - t


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("pdf")
    ap.add_argument("--visitors", type=int, default=100)
    ap.add_argument("--uploads", type=int, default=20)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    loop = asyncio.get_event_loop()

    print(f"\n== {args.visitors} concurrent visitors → GET /health ==")
    t0 = time.perf_counter()
    health = await asyncio.gather(*[
        loop.run_in_executor(None, _health, base, i) for i in range(args.visitors)
    ])
    wall = time.perf_counter() - t0
    ok = [d for c, d in health if c == 200]
    codes = {}
    for c, _ in health:
        codes[c] = codes.get(c, 0) + 1
    print(f"  wall time     : {wall:.2f}s for {args.visitors} requests")
    print(f"  status codes  : {codes}")
    if ok:
        print(f"  latency p50   : {_pctl(ok,50)*1000:.0f} ms")
        print(f"  latency p95   : {_pctl(ok,95)*1000:.0f} ms")
        print(f"  latency p99   : {_pctl(ok,99)*1000:.0f} ms")
        print(f"  latency max   : {max(ok)*1000:.0f} ms")

    print(f"\n== {args.uploads} simultaneous uploads → POST /api/analyze ==")
    t0 = time.perf_counter()
    ups = await asyncio.gather(*[
        loop.run_in_executor(None, _upload, base, args.pdf, i) for i in range(args.uploads)
    ])
    wall = time.perf_counter() - t0
    codes = {}
    for c, *_ in ups:
        codes[c] = codes.get(c, 0) + 1
    up_lat = [d for c, d, *_ in ups if c in (202, 429)]
    print(f"  wall time     : {wall:.2f}s")
    print(f"  status codes  : {codes}   (202=queued, 429=busy/guard)")
    if up_lat:
        print(f"  upload p95    : {_pctl(up_lat,95)*1000:.0f} ms   (target < 2000 ms)")

    jobs = [(jid, ip) for c, d, jid, ip in ups if c == 202 and jid]
    print(f"\n== polling {len(jobs)} enqueued jobs to completion ==")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        loop.run_in_executor(None, _poll, base, jid, ip) for jid, ip in jobs
    ])
    done = sum(1 for st, _ in results if st == "done")
    times = [d for st, d in results if st == "done"]
    print(f"  completed     : {done}/{len(jobs)}")
    if times:
        print(f"  job time p50  : {statistics.median(times):.1f}s")
        print(f"  job time p95  : {_pctl(times,95):.1f}s   (target < 45s)")
        print(f"  job time max  : {max(times):.1f}s")
    print(f"  total wall    : {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    asyncio.run(main())
