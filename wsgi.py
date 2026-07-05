import db

db.init_db()

from app import app, _start_scheduler  # noqa: E402

_start_scheduler()
