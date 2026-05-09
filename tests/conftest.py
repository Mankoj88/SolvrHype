import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import numpy as np
import pandas as pd
import pytest
from freezegun import freeze_time


# ============================================================================
# Environment isolation
# ============================================================================

@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """
    Auto-applied to every test: prevents accidental real network calls
    or filesystem writes outside the temp dir.
    """
    # Force test mode
    monkeypatch.setenv("SOLVIRA_ENV", "test")
    monkeypatch.setenv("DRY_RUN", "true")

    # Fake credentials (will fail real auth — by design)
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("HL_ADDRESS", "0x" + "cd" * 20)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-do-not-use")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000000000:FAKE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "0")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "http://localhost:1/fake")
    monkeypatch.setenv("METAMASK_ADDRESS", "0x" + "ef" * 20)

    # Redirect any state files to tmp
    monkeypatch.setenv("SOLVIRA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SOLVIRA_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setenv("SOLVIRA_LOG_DIR", str(tmp_path / "logs"))

    (tmp_path / "logs").mkdir(exist_ok=True)
    yield


# ============================================================================
# Candle / market data fixtures
# ============================================================================

@pytest.fixture
def sample_candles_df():
    """
    Generic 200-candle 10-minute dataframe with a clear uptrend.
    Use as base; tests can mutate the last few candles to simulate setups.
    """
    n = 200
    base_price = 100.0
    rng = np.random.default_rng(42)

    closes = base_price + np.cumsum(rng.normal(0.05, 0.5, n))
    highs = closes + np.abs(rng.normal(0.3, 0.1, n))
    lows = closes - np.abs(rng.normal(0.3, 0.1, n))
    opens = np.concatenate([[base_price], closes[:-1]])
    volumes = rng.uniform(1000, 5000, n)

    timestamps = pd.date_range(
        end=pd.Timestamp.now("UTC").floor("10min"),
        periods=n,
        freq="10min",
    )

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


@pytest.fixture
def oversold_setup_df(sample_candles_df):
    """
    Modified candles where the last 3 closes form an oversold→reversal pattern
    that should trigger a Stoch RSI golden cross + MACD reversal signal.
    Use this fixture to test scanner positive-path.
    """
    df = sample_candles_df.copy()
    # Force a sharp dip in last 30 candles → oversold
    df.loc[df.index[-30:-3], "close"] *= 0.85
    df.loc[df.index[-30:-3], "low"] *= 0.83
    # Then a bounce in last 3 candles
    df.loc[df.index[-3:], "close"] = df["close"].iloc[-4] * np.array([1.01, 1.02, 1.025])
    df.loc[df.index[-3:], "high"] = df["close"].iloc[-3:] * 1.005
    # Volume spike on the last candle
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:-1].mean() * 3.5
    return df


@pytest.fixture
def flat_market_df(sample_candles_df):
    """Sideways market — should NOT generate signals."""
    df = sample_candles_df.copy()
    df["close"] = 100.0 + np.random.default_rng(1).normal(0, 0.05, len(df))
    df["high"] = df["close"] + 0.1
    df["low"] = df["close"] - 0.1
    df["open"] = df["close"].shift(1).fillna(100.0)
    return df


# ============================================================================
# Hyperliquid SDK mocks
# ============================================================================

@pytest.fixture
def fake_hyperliquid_meta():
    """Minimal metadata response for HL Info.meta()"""
    return {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
            {"name": "ARB", "szDecimals": 1, "maxLeverage": 10},
        ]
    }


@pytest.fixture
def fake_meta_with_ctx(fake_hyperliquid_meta):
    """meta_and_asset_ctxs() response — meta + funding/oi/markPx tuple."""
    ctxs = [
        {"funding": "0.00001", "openInterest": "1000.0", "markPx": "65000.0",
         "premium": "0.0", "midPx": "65000.0", "impactPxs": ["64999", "65001"],
         "dayNtlVlm": "1000000.0", "prevDayPx": "64500.0"},
        {"funding": "0.00002", "openInterest": "500.0", "markPx": "3500.0",
         "premium": "0.0", "midPx": "3500.0", "impactPxs": ["3499", "3501"],
         "dayNtlVlm": "500000.0", "prevDayPx": "3450.0"},
        {"funding": "0.00003", "openInterest": "10000.0", "markPx": "150.0",
         "premium": "0.0", "midPx": "150.0", "impactPxs": ["149.9", "150.1"],
         "dayNtlVlm": "200000.0", "prevDayPx": "148.0"},
        {"funding": "0.00001", "openInterest": "50000.0", "markPx": "1.20",
         "premium": "0.0", "midPx": "1.20", "impactPxs": ["1.199", "1.201"],
         "dayNtlVlm": "50000.0", "prevDayPx": "1.18"},
    ]
    return [fake_hyperliquid_meta, ctxs]


@pytest.fixture
def mock_hl_info(fake_meta_with_ctx, sample_candles_df):
    """Mocked hyperliquid Info client."""
    info = MagicMock()
    info.meta = MagicMock(return_value=fake_meta_with_ctx[0])
    info.meta_and_asset_ctxs = MagicMock(return_value=fake_meta_with_ctx)
    info.user_state = MagicMock(return_value={
        "marginSummary": {"accountValue": "1000.0", "totalRawUsd": "1000.0",
                          "totalNtlPos": "0.0", "totalMarginUsed": "0.0"},
        "assetPositions": [],
        "withdrawable": "1000.0",
    })

    # candles_snapshot returns list-of-dicts (HL format)
    def _candles_snapshot(coin, interval, startTime, endTime):
        df = sample_candles_df.tail(200)
        return [
            {
                "t": int(ts.timestamp() * 1000),
                "T": int(ts.timestamp() * 1000) + 600_000,
                "s": coin,
                "i": interval,
                "o": str(o), "h": str(h), "l": str(l), "c": str(c),
                "v": str(v), "n": 100,
            }
            for ts, o, h, l, c, v in zip(
                df["timestamp"], df["open"], df["high"], df["low"],
                df["close"], df["volume"]
            )
        ]
    info.candles_snapshot = MagicMock(side_effect=_candles_snapshot)
    info.all_mids = MagicMock(return_value={
        "BTC": "65000.0", "ETH": "3500.0", "SOL": "150.0", "ARB": "1.20"
    })
    return info


@pytest.fixture
def mock_hl_exchange():
    """Mocked hyperliquid Exchange client (order placement)."""
    ex = MagicMock()
    ex.order = MagicMock(return_value={
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"filled": {"totalSz": "0.001", "avgPx": "65000.0", "oid": 12345}}
                ]
            }
        }
    })
    ex.market_close = MagicMock(return_value={"status": "ok"})
    ex.cancel = MagicMock(return_value={"status": "ok"})
    ex.update_leverage = MagicMock(return_value={"status": "ok"})
    return ex


# ============================================================================
# Telegram & Anthropic mocks
# ============================================================================

@pytest.fixture
def mock_telegram(monkeypatch):
    """Capture all sendMessage calls; assert via mock.calls list."""
    calls = []

    async def fake_send(chat_id, text, **kwargs):
        calls.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return {"ok": True, "result": {"message_id": len(calls)}}

    fake = AsyncMock(side_effect=fake_send)
    fake.calls = calls
    return fake


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Mock Anthropic client returning a deterministic review JSON."""
    def fake_create(**kwargs):
        return MagicMock(
            content=[MagicMock(text='{"verdict": "no_change", "reason": "test"}')],
            stop_reason="end_turn",
            usage=MagicMock(input_tokens=100, output_tokens=50),
        )

    client = MagicMock()
    client.messages.create = MagicMock(side_effect=fake_create)
    return client


# ============================================================================
# Database fixtures
# ============================================================================

@pytest.fixture
def isolated_db(tmp_path):
    """Empty SQLite DB at tmp path — schema NOT applied (test will apply it)."""
    db_path = tmp_path / "test_trades.db"
    yield str(db_path)
    # cleanup: file auto-removed with tmp_path


@pytest.fixture
def seeded_db(tmp_path):
    """SQLite DB with the Solvira schema pre-applied. Empty rows."""
    db_path = tmp_path / "seeded_trades.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            entry_size_usd REAL NOT NULL,
            entry_size_coin REAL NOT NULL,
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            sl_hit INTEGER DEFAULT 0,
            pnl_usd REAL,
            opened_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP,
            close_reason TEXT
        );
        CREATE TABLE daily_snapshots (
            date TEXT PRIMARY KEY,
            account_value REAL NOT NULL,
            total_pnl REAL,
            n_trades INTEGER,
            n_wins INTEGER,
            withdrawable REAL
        );
        CREATE TABLE withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_usd REAL NOT NULL,
            hl_tx_hash TEXT,
            arb_swap_tx TEXT,
            mm_send_tx TEXT,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    yield str(db_path)


# ============================================================================
# Time fixtures
# ============================================================================

@pytest.fixture
def freeze_clock():
    """Freeze time at a known UTC instant — useful for funding/schedule tests."""
    with freeze_time("2026-05-07 12:00:00", tz_offset=0) as frozen:
        yield frozen


@pytest.fixture
def funding_window():
    """Freeze time exactly at top of hour (HL funding window)."""
    with freeze_time("2026-05-07 12:00:30", tz_offset=0) as frozen:
        yield frozen


# ============================================================================
# Position factories (use with order_manager tests)
# ============================================================================

@pytest.fixture
def make_position():
    """Factory: build a Position dict with sensible defaults."""
    def _make(**overrides):
        base = {
            # Position dataclass fields
            "asset": "BTC",
            "entry_price": 65000.0,
            "entry_size_coin": 0.003076,
            "entry_size_usd": 200.0,
            "entry_time_ms": 1746612000000,
            "tp_levels_remaining": [[3.0, 0.5], [5.0, 1.0]],
            "tp_hit_count": 0,
            "initial_sl_price": 63700.0,
            "current_sl_price": 63700.0,
            "sl_oid": 12345,
            "remaining_size_coin": 0.003076,
        }
        base.update(overrides)
        return base
    return _make