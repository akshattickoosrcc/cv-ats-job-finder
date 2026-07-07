"""
Gunicorn config for the WEB process (pure JSON API — no heavy work).

Sizing math
───────────
Every request the web process handles is now cheap: validate a <=2 MB PDF
(pypdf page count, a few ms) + write the file + enqueue (a Redis RPUSH). Real
work happens in the worker. So we optimise for *many cheap concurrent
requests*, using threaded workers.

  concurrency = WORKERS x THREADS   (simultaneous in-flight requests)

Defaults below give 3 x 8 = 24 in-flight. Since each request finishes in
~30-100 ms, that clears ~240-800 req/s — a burst of 100 visitors landing in a
few seconds is absorbed with room to spare.

Memory: each gthread worker is ~70-110 MB (Flask + limiter + pypdf). 3 workers
≈ 250-330 MB, comfortable on a 1-2 GB VPS alongside Redis + the worker process.

Tune per box with env vars:
  512 MB box:  WEB_WORKERS=2 WEB_THREADS=8     (~16 concurrent, ~180 MB)
  1 GB box:    WEB_WORKERS=3 WEB_THREADS=8     (default)
  2 GB box:    WEB_WORKERS=4 WEB_THREADS=12    (~48 concurrent)
"""
import os

bind             = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers          = int(os.environ.get("WEB_WORKERS", "3"))
threads          = int(os.environ.get("WEB_THREADS", "8"))
worker_class     = "gthread"
timeout          = 30              # requests are cheap; 30s is generous
graceful_timeout = 30
keepalive        = 5
max_requests     = 1000           # recycle workers to bound memory drift
max_requests_jitter = 100
preload_app      = False          # keep each worker's Redis client independent
loglevel         = "info"
accesslog        = "-"
errorlog         = "-"
