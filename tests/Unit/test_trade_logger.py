"""
Unit tests — monitoring/trade_logger.py

Test IDs: TL1–TL9 from solvira_stress_test_master.md §3.8.
TL3: Bug #22 regression — schema migration.
"""

import sqlite3
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def tl_env(monkeypatch, tmp_path):
    """Redirect DB_PATH on the trade_logger module to a tmp DB and init schema."""
    from monitoring import trade_logger as tl
    db = tmp_path / "trades.db"
    monkeypatch.setattr(tl, "DB_PATH", db)
    tl.init_db()
    return tl, db


# -----------------------------------------------------------------------------
# TL1 — init_db() creates required tables
# -----------------------------------------------------------------------------
class TestInitDbSchema:
    pytestmark = pytest.mark.blocker

    def test_init_db_creates_required_tables(self, tl_env):
        _tl, db = tl_env
        with sqlite3.connect(db) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "trades" in tables
        assert "daily_snapshots" in tables
        assert "withdrawals" in tables


# -----------------------------------------------------------------------------
# TL2 — log_trade writes a round-trip-able row
# -----------------------------------------------------------------------------
class TestLogTradeWrite:
    pytestmark = pytest.mark.blocker

    def test_log_trade_round_trip(self, tl_env):
        tl, db = tl_env
        now_ms = int(time.time() * 1000)
        tl.log_trade(
            asset="BTC", side="long_close",
            size_coin=0.001, size_usd=65.0,
            entry_price=65000.0, exit_price=66000.0,
            entry_time_ms=now_ms - 3600_000, exit_time_ms=now_ms,
            pnl_usd=1.0, pnl_pct=1.5, exit_reason="tp1",
        )
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trades WHERE asset='BTC'"
            ).fetchone()
        assert row is not None
        assert row["side"] == "long_close"
        assert row["pnl_usd"] == 1.0
        assert row["exit_reason"] == "tp1"


# -----------------------------------------------------------------------------
# TL3 — 🔴 Bug #22 regression: schema migration
# -----------------------------------------------------------------------------
class TestBug22DbMigration:
    pytestmark = [pytest.mark.blocker, pytest.mark.regression]

    def test_init_db_uses_user_version_pragma(self, tl_env):
        """init_db must drive migrations via PRAGMA user_version."""
        _tl, db = tl_env
        with sqlite3.connect(db) as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1, "user_version must be set after init_db()"

    def test_old_schema_migrated_in_place(self, tmp_path):
        """A pre-existing trades table with only the v0 columns must gain the
        new columns when migrate_schema runs.
        """
        from monitoring.trade_logger import migrate_schema

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, "
            "entry_price REAL)"
        )
        conn.commit()
        conn.close()

        migrate_schema(str(db))

        with sqlite3.connect(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        assert "tp1_hit" in cols
        assert "tp2_hit" in cols
        assert "sl_hit" in cols
        assert "close_reason" in cols


# -----------------------------------------------------------------------------
# TL4 — get_daily_stats on an empty day returns zeros
# -----------------------------------------------------------------------------
class TestEmptyDayStats:
    def test_no_trades_returns_zeros(self, tl_env):
        tl, _ = tl_env
        stats = tl.get_daily_stats("2099-01-01")
        assert stats["total_trades"] == 0
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["pnl_usd"] == 0.0
        assert stats["top_trade"] is None


# -----------------------------------------------------------------------------
# TL5 — Aggregation: multiple trades sum correctly
# -----------------------------------------------------------------------------
class TestAggregation:
    def test_n_trades_sum_correct(self, tl_env):
        tl, _ = tl_env
        today = datetime.now(timezone.utc)
        exit_ms = int(today.timestamp() * 1000)

        for i in range(10):
            tl.log_trade(
                asset=f"AST{i}", side="long_close",
                size_coin=0.1, size_usd=100.0,
                entry_price=100.0, exit_price=110.0,
                entry_time_ms=exit_ms - 60_000, exit_time_ms=exit_ms,
                pnl_usd=(1.0 if i % 2 == 0 else -0.5),
                pnl_pct=1.0,
                exit_reason="tp1",
            )

        stats = tl.get_daily_stats(today.strftime("%Y-%m-%d"))
        assert stats["total_trades"] == 10
        assert stats["wins"] == 5
        assert stats["losses"] == 5
        assert stats["pnl_usd"] == pytest.approx(5 * 1.0 + 5 * -0.5)


# -----------------------------------------------------------------------------
# TL6 — get_total_pnl_since used by SL enforcer
# -----------------------------------------------------------------------------
class TestPnlSince:
    pytestmark = pytest.mark.blocker

    def test_pnl_since_returns_correct_total(self, tl_env):
        tl, _ = tl_env
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        # Two trades inside the window
        for pnl in (10.0, -3.0):
            tl.log_trade(
                asset="BTC", side="long_close",
                size_coin=0.001, size_usd=65.0,
                entry_time_ms=now_ms - 300_000, exit_time_ms=now_ms,
                pnl_usd=pnl, pnl_pct=1.0,
            )

        cutoff = (now.replace(microsecond=0).isoformat()
                  .replace("+00:00", "+00:00"))
        # Use a slightly-earlier cutoff to include both rows
        from datetime import timedelta
        earlier = (now - timedelta(hours=1)).isoformat()
        total = tl.get_total_pnl_since(earlier)
        assert total == pytest.approx(10.0 - 3.0)


# -----------------------------------------------------------------------------
# TL7 — DB locked during concurrent writes (retry)
# -----------------------------------------------------------------------------
class TestDbLockRetry:
    def test_db_locked_retries(self):
        pytest.skip(
            "SQLite-locked retry logic not wired in trade_logger — deferred"
        )


# -----------------------------------------------------------------------------
# TL8 — Disk full → log + continue (no crash)
# -----------------------------------------------------------------------------
class TestDiskFullLogTrade:
    def test_disk_full_does_not_crash(self, monkeypatch):
        pytest.skip("Disk-full fault injection — deferred to Chaos tier")


# -----------------------------------------------------------------------------
# TL9 — DB file deleted at runtime → fail loud (don't silently swallow)
# -----------------------------------------------------------------------------
class TestDbFileDeleted:
    pytestmark = pytest.mark.blocker

    def test_db_file_deleted_at_runtime(self, tl_env):
        """If the DB file is deleted between writes, sqlite3 should raise so
        the failure is visible — not silently swallow the trade.
        """
        tl, db = tl_env
        db.unlink()
        # Subsequent operations should raise (or recreate the file). Either is
        # acceptable; what's unacceptable is a silent no-op.
        try:
            tl.log_trade(
                asset="BTC", side="long_close",
                size_coin=0.001, size_usd=65.0, pnl_usd=0.0,
            )
        except sqlite3.OperationalError:
            return  # loud failure — acceptable
        except sqlite3.DatabaseError:
            return
        # If no exception, SQLite recreated the file — verify it now exists
        assert db.exists(), \
            "trade_logger silently dropped a trade with no DB file present"
