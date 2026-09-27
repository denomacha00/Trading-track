"""Test-wide setup — most importantly, DATABASE isolation.

pytest imports this file BEFORE it collects any test module, which is the only
moment early enough to point the app at a throwaway database. ``app.database``
binds its SQLAlchemy engine to ``settings.database_url`` at import time (a
module-level ``create_engine``), and ``get_settings()`` is cached — so whichever
test module first imports the app (directly, or transitively via ``app.main`` /
``app.ai`` / ``app.engine``) freezes the DB for the entire run. If that happens
before ``DATABASE_URL`` is redirected, the whole suite runs against — and writes
into — the developer's real ``tranding_track.db``. Stale rows there (e.g. a
legacy admin whose ``username`` is NULL, or signal_logs left by an earlier run)
then make order-dependent tests flap, and every run pollutes the dev DB.

Setting the env here, before the first ``import app.*``, guarantees a private,
per-process SQLite file no matter what order pytest collects the modules in.
"""
from __future__ import annotations

import atexit
import os
import tempfile

# Force paper mode and redirect the DB BEFORE any ``import app.*`` executes.
os.environ.setdefault("TRADING_MODE", "paper")

# A private, throwaway SQLite file keyed to this process — never the developer's
# persistent tranding_track.db. Hard-set (not setdefault): the point of the test
# suite is total DB isolation, so a DATABASE_URL left in the shell (often the dev
# DB, for running the app) must not leak in and re-introduce the pollution bug.
_TEST_DB = os.path.join(tempfile.gettempdir(), f"tt_test_{os.getpid()}.db")
for _p in (_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"):
    try:
        os.remove(_p)
    except OSError:
        pass
os.environ["DATABASE_URL"] = "sqlite:///" + _TEST_DB.replace("\\", "/")


@atexit.register
def _cleanup_test_db() -> None:
    for _p in (_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"):
        try:
            os.remove(_p)
        except OSError:
            pass
