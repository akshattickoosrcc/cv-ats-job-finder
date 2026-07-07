"""WSGI entrypoint for the web API (gunicorn wsgi:app).

The web process is pure API now — no scheduler / scraping runs here. All heavy
work lives in worker.py. db.init_db() runs on importing app.
"""
from app import app  # noqa: F401
