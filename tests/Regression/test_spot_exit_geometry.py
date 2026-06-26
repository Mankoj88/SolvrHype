"""
Regression — SPOT exit geometry (SL/TP) resolved via the REAL entry-path resolvers.

Pins the new spot exit rules so a future config drift is caught:
  - SL: a spot long at entry E gets SL ≈ E*0.99 (−1%), NEVER E*1.01 (sign bug).
  - TP1: +1.75%, sells 50% of remaining, post_action "breakeven".
  - TP2: +2.75%, sells 100% of remaining, post_action None.
  - max_hold_hours unchanged at 6.0.

These assert through OrderManager._resolve_sl_price / _resolve_tp_levels /
_max_hold_hours — the exact functions called at entry (order_manager.py ~548-549,
609-610, 477-482) — not against the raw dict, so they prove the wiring, not just
the literals. Uses the established object.__new__(OrderManager) harness pattern.
"""
import time
import types

import pytest

from execution.order_manager import OrderManager
from strategy.base_strategy import TradeSignal

pytestmark = [pytest.mark.regression]


def _spot_long_signal(price=100.0):
    """A spot, pct-mode long — the path that reads SPOT['cutloss_pct']/['take_profits']."""
    return TradeSignal(
        asset="BTC",
        price=price,
        timestamp_ms=int(time.time() * 1000),
        reason="test",
        indicators_snapshot={},
        strategy_type="spot",
        leverage=1,
        is_long=True,
        sl_mode="pct",
    )


@pytest.fixture
def om():
    """Bare OrderManager — the resolvers touch no exchange/info state."""
    return object.__new__(OrderManager)


def test_spot_long_sl_is_minus_one_percent(om):
    entry = 100.0
    sl = om._resolve_sl_price(_spot_long_signal(entry), entry)
    # −1% below entry, NOT +1% above (sign bug guard).
    assert sl == pytest.approx(entry * 0.99)
    assert sl < entry, "spot long SL must sit BELOW entry"


def test_spot_tp_levels_175_and_275(om):
    tps = om._resolve_tp_levels(_spot_long_signal())
    assert len(tps) == 2

    tp1_pct, tp1_frac, tp1_action = tps[0]
    assert tp1_pct == pytest.approx(1.75)
    assert tp1_frac == pytest.approx(0.50)
    assert tp1_action == "breakeven"   # SL → entry after TP1

    tp2_pct, tp2_frac, tp2_action = tps[1]
    assert tp2_pct == pytest.approx(2.75)
    assert tp2_frac == pytest.approx(1.00)
    assert tp2_action is None


def test_spot_max_hold_unchanged(om):
    # _max_hold_hours only reads pos.strategy_type → SimpleNamespace exercises the branch.
    pos = types.SimpleNamespace(strategy_type="spot")
    assert om._max_hold_hours(pos) == 6.0
