"""
SQLite trade logger untuk audit trail, performance analytics, tax reporting.
"""
import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from loguru import logger
from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    asset_class TEXT NOT NULL DEFAULT 'crypto',
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    size_coin REAL NOT NULL,
    size_usd REAL NOT NULL,
    entry_time_utc TEXT,
    exit_time_utc TEXT,
    hold_duration_seconds INTEGER,
    pnl_usd REAL,
    pnl_pct REAL,
    fees_usd REAL DEFAULT 0,
    exit_reason TEXT,
    indicators_snapshot TEXT,
    notes TEXT,
    strategy_type TEXT DEFAULT 'spot',
    leverage INTEGER DEFAULT 1,
    entry_swing_price REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time_utc);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    date TEXT PRIMARY KEY,
    capital_usd REAL,
    open_positions_count INTEGER,
    open_positions_value_usd REAL,
    cumulative_usdt_wallet REAL,
    daily_pnl_usd REAL,
    daily_trades_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount_usd REAL NOT NULL,
    tx_hash_hl TEXT,
    tx_hash_swap TEXT,
    tx_hash_send TEXT,
    status TEXT,
    error_message TEXT,
    initiated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)

        # Bug #22: version-based migrations so new columns don't break existing DBs
        cur = conn.execute("PRAGMA user_version").fetchone()
        version = cur[0] if cur else 0

        if version < 1:
            # v1: asset_class column (already in SCHEMA DEFAULT, but add if missing on old DBs)
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN asset_class TEXT DEFAULT 'crypto'")
            except Exception:
                pass  # column already exists
            conn.execute("PRAGMA user_version = 1")

        if version < 2:
            # v2: dual-strategy columns
            for sql in [
                "ALTER TABLE trades ADD COLUMN strategy_type TEXT DEFAULT 'spot'",
                "ALTER TABLE trades ADD COLUMN leverage INTEGER DEFAULT 1",
                "ALTER TABLE trades ADD COLUMN entry_swing_price REAL",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.execute("PRAGMA user_version = 2")

        if version < 3:
            # v3: trade observability — max favorable/adverse excursion (%)
            for sql in [
                "ALTER TABLE trades ADD COLUMN mfe_pct REAL",
                "ALTER TABLE trades ADD COLUMN mae_pct REAL",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.execute("PRAGMA user_version = 3")

        # Add future migrations here as version < N blocks
        final_version = conn.execute("PRAGMA user_version").fetchone()[0]

    logger.info(f"Database initialized (schema v{final_version}): {DB_PATH}")


def migrate_schema(db_path: str):
    """Add missing columns to an existing trades table (safe to run on any version)."""
    migrations = [
        "ALTER TABLE trades ADD COLUMN tp1_hit INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN tp2_hit INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN sl_hit INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN close_reason TEXT",
    ]
    conn = sqlite3.connect(db_path)
    try:
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def log_trade(asset, side, size_coin, size_usd, entry_price=None, exit_price=None,
              entry_time_ms=None, exit_time_ms=None, pnl_usd=None, pnl_pct=None,
              fees_usd=0, exit_reason=None, indicators_snapshot=None, notes=None,
              strategy_type="spot", leverage=1, entry_swing_price=None,
              mfe_pct=None, mae_pct=None):
    entry_time = (datetime.fromtimestamp(entry_time_ms/1000, tz=timezone.utc).isoformat()
                  if entry_time_ms else None)
    exit_time = (datetime.fromtimestamp(exit_time_ms/1000, tz=timezone.utc).isoformat()
                 if exit_time_ms else None)
    duration = (int((exit_time_ms - entry_time_ms) / 1000)
                if (exit_time_ms and entry_time_ms) else None)

    with _conn() as conn:
        conn.execute("""
            INSERT INTO trades (
                asset, side, entry_price, exit_price, size_coin, size_usd,
                entry_time_utc, exit_time_utc, hold_duration_seconds,
                pnl_usd, pnl_pct, fees_usd, exit_reason,
                indicators_snapshot, notes,
                strategy_type, leverage, entry_swing_price,
                mfe_pct, mae_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (asset, side, entry_price, exit_price, size_coin, size_usd,
              entry_time, exit_time, duration, pnl_usd, pnl_pct, fees_usd, exit_reason,
              json.dumps(indicators_snapshot) if indicators_snapshot else None, notes,
              strategy_type, leverage, entry_swing_price,
              mfe_pct, mae_pct))


def log_daily_snapshot(capital_usd, open_positions_count, open_positions_value_usd,
                       cumulative_usdt_wallet, daily_pnl_usd, daily_trades_count):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_snapshots
            (date, capital_usd, open_positions_count, open_positions_value_usd,
             cumulative_usdt_wallet, daily_pnl_usd, daily_trades_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (today, capital_usd, open_positions_count, open_positions_value_usd,
              cumulative_usdt_wallet, daily_pnl_usd, daily_trades_count))


def log_withdrawal_initiated(amount_usd: float, tx_hash_hl: str = None) -> int:
    with _conn() as conn:
        cursor = conn.execute("""
            INSERT INTO withdrawals (amount_usd, tx_hash_hl, status)
            VALUES (?, ?, 'pending')
        """, (amount_usd, tx_hash_hl))
        return cursor.lastrowid


def log_withdrawal_complete(withdrawal_id: int, tx_hash_swap: str = None, tx_hash_send: str = None):
    with _conn() as conn:
        conn.execute("""
            UPDATE withdrawals
            SET tx_hash_swap = ?, tx_hash_send = ?, status = 'complete', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tx_hash_swap, tx_hash_send, withdrawal_id))


def log_withdrawal_failed(withdrawal_id: int, error: str):
    with _conn() as conn:
        conn.execute("""
            UPDATE withdrawals
            SET status = 'failed', error_message = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error, withdrawal_id))


def get_recent_trades(days_back: int = 7, strategy_type: str = None) -> list[dict]:
    cutoff = datetime.now(timezone.utc).timestamp() - days_back * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with _conn() as conn:
        if strategy_type:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE entry_time_utc >= ? AND strategy_type = ?
                ORDER BY entry_time_utc DESC
            """, (cutoff_iso, strategy_type)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM trades WHERE entry_time_utc >= ? ORDER BY entry_time_utc DESC
            """, (cutoff_iso,)).fetchall()
        return [dict(r) for r in rows]


def get_daily_stats(date_str: str = None) -> dict:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades WHERE DATE(exit_time_utc) = ? AND exit_time_utc IS NOT NULL
        """, (date_str,)).fetchall()
        
        if not rows:
            return {
                "date": date_str, "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "pnl_usd": 0.0, "pnl_pct": 0.0,
                "top_trade": None, "worst_trade": None,
            }
        
        wins = [r for r in rows if (r["pnl_usd"] or 0) > 0]
        losses = [r for r in rows if (r["pnl_usd"] or 0) <= 0]
        total_pnl = sum(r["pnl_usd"] or 0 for r in rows)
        total_size = sum(r["size_usd"] or 0 for r in rows)
        
        sorted_by_pnl = sorted(rows, key=lambda r: r["pnl_pct"] or 0, reverse=True)
        
        return {
            "date": date_str,
            "total_trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(rows) if rows else 0,
            "pnl_usd": total_pnl,
            "pnl_pct": (total_pnl / total_size * 100) if total_size else 0,
            "top_trade": {
                "asset": sorted_by_pnl[0]["asset"],
                "pnl_pct": sorted_by_pnl[0]["pnl_pct"] or 0,
            } if sorted_by_pnl else None,
            "worst_trade": {
                "asset": sorted_by_pnl[-1]["asset"],
                "pnl_pct": sorted_by_pnl[-1]["pnl_pct"] or 0,
            } if len(sorted_by_pnl) > 1 else None,
        }


def get_total_pnl_since(start_date_iso: str) -> float:
    with _conn() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl_usd), 0) as total FROM trades WHERE entry_time_utc >= ?
        """, (start_date_iso,)).fetchone()
        return row["total"] or 0