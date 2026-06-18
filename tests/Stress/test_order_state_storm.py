"""
ST7 — Order/state storm.

Hammer OrderManager with rapid open/close + DRY_RUN sequences. V2 state file
must remain valid JSON, cooldowns must hold, and per-strategy counts must
stay accurate even under interleaved spot+derivative entries.
"""
from __future__ import annotations

import json

import pytest

from strategy.base_strategy import TradeSignal

pytestmark = pytest.mark.stress


def _mk_spot_signal(asset: str, price: float = 100.0) -> TradeSignal:
    return TradeSignal(
        asset=asset, price=price, timestamp_ms=0,
        reason="stress", indicators_snapshot={},
        strategy_type="spot", leverage=1, is_long=True, sl_mode="pct",
    )


def _mk_deriv_signal(asset: str, price: float, sl: float, is_long: bool = True) -> TradeSignal:
    return TradeSignal(
        asset=asset, price=price, timestamp_ms=0,
        reason="stress", indicators_snapshot={},
        strategy_type="derivative", leverage=3, is_long=is_long,
        suggested_sl_price=sl,
        sl_mode="swing_low" if is_long else "swing_high",
    )


def test_state_file_remains_valid_json_under_rapid_writes(patched_sdk, isolate_state_dir):
    from execution.order_manager import OrderManager

    om = OrderManager()
    state_file = isolate_state_dir / "positions_state.json"

    # Open + close 50 positions rapidly across strategies
    for i in range(50):
        sig_spot = _mk_spot_signal(f"S{i}", price=100 + i)
        ok = om.execute_entry(sig_spot, 100.0)
        assert ok, f"spot entry {i} failed"

        sig_deriv = _mk_deriv_signal(f"D{i}", price=200 + i, sl=190 + i)
        ok = om.execute_entry(sig_deriv, 80.0)
        # Deriv may be rejected if hit MAX_OPEN_POSITIONS; that's fine.

        # State file must always parse
        with open(state_file) as f:
            data = json.load(f)
        assert isinstance(data, dict)

        # Close all
        prices = {a: p.entry_price * 1.10 for a, p in om.positions.items()}
        om.manage_open_positions(prices)
        # After full TP loop, eventually positions clear
        prices = {a: p.entry_price * 1.30 for a, p in om.positions.items()}
        om.manage_open_positions(prices)


def test_per_strategy_count_stays_accurate(patched_sdk, isolate_state_dir):
    from execution.order_manager import OrderManager

    om = OrderManager()

    for i in range(3):
        sig = _mk_spot_signal(f"SPOT{i}", price=50.0)
        om.execute_entry(sig, 100.0)

    counts = om.open_position_count_by_strategy()
    assert counts["spot"] == 3 and counts["derivative"] == 0, counts

    sig_d = _mk_deriv_signal("DERIVA", price=100.0, sl=95.0)
    # Max 3 open enforced → 4th entry rejected
    ok = om.execute_entry(sig_d, 80.0)
    assert ok is False, "Should reject when at MAX_OPEN_POSITIONS"

    # Close one spot via SL trigger
    spot_asset = next(a for a, p in om.positions.items() if p.strategy_type == "spot")
    om.manage_open_positions({spot_asset: om.positions[spot_asset].entry_price * 0.5})

    # Cooldown blocks immediate re-entry
    sig_again = _mk_spot_signal(spot_asset, price=50.0)
    ok = om.execute_entry(sig_again, 100.0)
    assert ok is False, "Cooldown should block immediate re-entry"


def test_state_roundtrip_after_storm(patched_sdk, isolate_state_dir):
    """Open 3 positions → instantiate fresh OrderManager → state must reload."""
    from execution.order_manager import OrderManager

    om = OrderManager()
    for asset in ("BTC", "ETH", "SOL"):
        om.execute_entry(_mk_spot_signal(asset, price=100.0), 100.0)

    assert len(om.positions) == 3

    om2 = OrderManager()
    assert len(om2.positions) == 3
    assert set(om2.positions.keys()) == {"BTC", "ETH", "SOL"}
    for pos in om2.positions.values():
        assert pos.strategy_type == "spot"
        assert pos.entry_size_coin > 0
