import os

# Free tier = 512MB RAM. 2 gevent workers is the safe max.
workers          = int(os.environ.get("WEB_CONCURRENCY", 1))
worker_class     = "gevent"
worker_connections = 100
timeout          = 120
keepalive        = 5
bind             = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
preload_app      = True   # share memory across workers — halves RAM usage
loglevel         = "info"
accesslog        = "-"
errorlog         = "-"
