"""
Trade-observability persistence (no trading-behavior change).

Confirms that on a close the trade_logger row carries the new observability
fields end-to-end:
  * indicators_snapshot  — captured at entry, persisted (NOT NULL)
  * fees_usd             — actual round-trip fees when fills exist, else the
                           taker-rate estimate (the DRY_RUN / no-fills path)
  * mfe_pct / mae_pct    — max favorable / adverse excursion seen while open

DRY_RUN invariant (STEP 7): with no real fills, _realized_fees_for returns None
→ the estimate path MUST run → fees_usd > 0.

Fee-attribution invariant (STEP 5): SUM(fees_usd) over a position's rows
(partial + full) equals the round-trip total (no double counting).

Uses object.__new__(OrderManager) — the established harness pattern in this repo
— a mock info client, and a temp DB pointed at by monitoring.trade_logger.DB_PATH.
"""
import sqlite3
import json
from unittest.mock import MagicMock

import pytest

from execution.order_manager import OrderManager, Position
from config import TAKER_FEE_RATE

pytestmark = [pytest.mark.regression]


@pytest.fixture
def trades_db(tmp_path, monkeypatch):
    """Fresh trades DB with schema migrated to the current version (v3 adds
    mfe_pct/mae_pct). trade_logger resolves DB_PATH from its module globals at
    call time, so monkeypatching it here redirects every log_trade/init_db."""
    import monitoring.trade_logger as tl
    db_path = tmp_path / "obs_trades.db"
    monkeypatch.setattr(tl, "DB_PATH", str(db_path))
    tl.init_db()
    return str(db_path)


@pytest.fixture
def no_side_effects(monkeypatch):
    """Neutralise the close-path side effects (tax log, health, withdraw) so the
    test only exercises the trade_logger write."""
    monkeypatch.setattr("monitoring.tax_logger.log_taxable_event", MagicMock())
    monkeypatch.setattr("monitoring.health.HealthMonitor", MagicMock())
    monkeypatch.setattr("execution.withdraw_manager.WithdrawManager", MagicMock())


@pytest.fixture
def om():
    """Minimally-wired OrderManager with a mock info client that reports NO fills
    (the DRY_RUN / no-real-orders condition → estimate fee path)."""
    o = object.__new__(OrderManager)
    o.info = MagicMock()
    o.info.user_fills = MagicMock(return_value=[])   # no fills → estimate path
    o.positions = {}
    return o


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


def _position(**overrides):
    base = dict(
        asset="SOL",
        entry_price=150.0,
        entry_size_coin=0.27,
        entry_size_usd=40.5,
        entry_time_ms=1_746_612_000_000,
        tp_levels_remaining=[],
        remaining_size_coin=0.27,
        indicators_snapshot={"rsi": 22.5, "macd_hist": -0.13, "ema_fast": 149.0},
        max_favorable_pct=3.4,
        max_adverse_pct=-1.2,
    )
    base.update(overrides)
    return Position(**base)


def test_full_close_persists_indicators_fees_and_mfe_mae(om, trades_db, no_side_effects):
    """A simulated full close writes a row with indicators_snapshot NOT NULL,
    fees_usd > 0 (estimate path, no real fills), and mfe/mae populated."""
    pos = _position()
    om.positions["SOL"] = pos

    # exit above entry → winning long (exercises the record_profit branch too)
    om._on_position_close_full("SOL", exit_price=155.0, exit_reason="tp2")

    rows = _rows(trades_db)
    assert len(rows) == 1
    row = rows[0]

    # indicators_snapshot persisted as JSON, NOT NULL
    assert row["indicators_snapshot"] is not None
    assert json.loads(row["indicators_snapshot"])["rsi"] == 22.5

    # fees: no real fills → taker-rate estimate = entry_size_usd * rate * 2
    expected_fee = pos.entry_size_usd * TAKER_FEE_RATE * 2
    assert row["fees_usd"] > 0
    assert row["fees_usd"] == pytest.approx(expected_fee)

    # MFE/MAE carried through
    assert row["mfe_pct"] == pytest.approx(3.4)
    assert row["mae_pct"] == pytest.approx(-1.2)


def test_fee_attribution_sums_to_roundtrip_across_partial_and_full(om, trades_db, no_side_effects):
    """STEP 5 invariant: a partial close logs its own taker estimate, the full
    close subtracts the partials, so SUM(fees_usd) == round-trip total."""
    pos = _position()
    om.positions["SOL"] = pos

    # partial: sell ~half, then close the rest
    om._on_position_close_partial("SOL", exit_price=153.0, exit_reason="tp1",
                                  size_sold=0.135)
    om._on_position_close_full("SOL", exit_price=155.0, exit_reason="tp2")

    rows = _rows(trades_db)
    assert len(rows) == 2  # one partial + one full

    total_fees = sum(r["fees_usd"] for r in rows)
    expected_roundtrip = pos.entry_size_usd * TAKER_FEE_RATE * 2
    assert total_fees == pytest.approx(expected_roundtrip)
    # both rows still carry the observability fields
    for r in rows:
        assert r["indicators_snapshot"] is not None
        assert r["mfe_pct"] == pytest.approx(3.4)


def test_actual_fees_used_when_fills_present(om, trades_db, no_side_effects):
    """When real fills exist for the asset since entry, the ACTUAL summed fee is
    used (not the estimate)."""
    pos = _position()
    om.positions["SOL"] = pos
    om.info.user_fills = MagicMock(return_value=[
        {"coin": "SOL", "time": pos.entry_time_ms + 10, "fee": "0.0181"},   # entry
        {"coin": "SOL", "time": pos.entry_time_ms + 5_000, "fee": "0.0190"},  # exit
        {"coin": "BTC", "time": pos.entry_time_ms + 5_000, "fee": "9.99"},    # other asset, ignored
        {"coin": "SOL", "time": pos.entry_time_ms - 1, "fee": "5.55"},        # before entry, ignored
    ])

    om._on_position_close_full("SOL", exit_price=155.0, exit_reason="tp2")

    row = _rows(trades_db)[0]
    assert row["fees_usd"] == pytest.approx(0.0181 + 0.0190)
