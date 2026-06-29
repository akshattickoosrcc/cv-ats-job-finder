import db
import scrapers
import threading

# Initialise DB once when gunicorn loads the app module
db.init_db()

from app import app, _start_scheduler  # noqa: E402

_start_scheduler()
threading.Thread(target=scrapers.run_full_scrape, daemon=True).start()
