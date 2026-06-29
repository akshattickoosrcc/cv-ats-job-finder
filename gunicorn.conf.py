import multiprocessing
import os

workers     = int(os.environ.get("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count() * 2)))
worker_class = "gevent"
worker_connections = 1000
timeout     = 120          # PDF parsing can take a few seconds
keepalive   = 5
bind        = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
preload_app = False        # each worker inits its own DB connection
loglevel    = "info"
accesslog   = "-"
errorlog    = "-"
