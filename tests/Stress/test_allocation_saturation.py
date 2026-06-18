"""
ST4 — Allocation pool saturation.

Hammer AllocationManager across both pools at varying capital. Pool usage is
DERIVED from live open positions (order_manager.positions), so this test drives
a fake position book instead of a reservation API. V2 must:
- Respect 50/50 pool split (no spill between spot ↔ derivative).
- Never return size > pool capacity.
- Never return size below MIN unless 0.
- "Closing" a position (removing it from the book) frees its pool usage fully.
- Be deterministic: same inputs → same outputs.
"""
from __future__ import annotations

import random

import pytest

from execution.allocation_manager import AllocationManager
from execution.order_manager import Position
from config import (
    MIN_POSITION_SIZE_USD, MAX_POSITION_SIZE_USD,
    STRATEGY_POOL_SPLIT,
)
from strategy.base_strategy import TradeSignal

pytestmark = pytest.mark.stress


class FakeOrderManager:
    """Minimal stand-in exposing the only attribute AllocationManager reads."""
    def __init__(self):
        self.positions: dict[str, Position] = {}


def _open(om: FakeOrderManager, asset: str, margin_usd: float,
          strategy_type: str, leverage: int = 1, entry_price: float = 100.0):
    """Add a position whose margin-equivalent pool usage == margin_usd.

    pool_used counts remaining_size_coin*entry_price/leverage, so to commit
    `margin_usd` of pool we set notional = margin_usd*leverage.
    """
    notional = margin_usd * leverage
    size_coin = notional / entry_price
    om.positions[asset] = Position(
        asset=asset, entry_price=entry_price,
        entry_size_coin=size_coin, remaining_size_coin=size_coin,
        entry_size_usd=notional, entry_time_ms=0,
        tp_levels_remaining=[], strategy_type=strategy_type, leverage=leverage,
    )


def _mk_deriv_signal(asset: str, entry: float = 100.0, sl: float = 95.0, lev: int = 5) -> TradeSignal:
    return TradeSignal(
        asset=asset, price=entry, timestamp_ms=0,
        reason="stress", indicators_snapshot={},
        strategy_type="derivative", leverage=lev, is_long=True,
        suggested_sl_price=sl, sl_mode="swing_low",
    )


def test_pool_isolation_under_1000_rounds():
    """1000 alternating open/close ops; spot pool used must never exceed spot
    pool capacity, and same for derivative."""
    om = FakeOrderManager()
    am = AllocationManager(order_manager=om)
    rng = random.Random(7)
    capital = 1000.0

    spot_pool_cap = capital * STRATEGY_POOL_SPLIT["spot"]
    deriv_pool_cap = capital * STRATEGY_POOL_SPLIT["derivative"]

    for i in range(1000):
        asset = f"A{i % 30}"
        if rng.random() < 0.5:
            sig = None
            strat = "spot"
            lev = 1
        else:
            lev = 5
            sig = _mk_deriv_signal(asset, entry=rng.uniform(50, 500),
                                   sl=rng.uniform(40, 49), lev=lev)
            strat = "derivative"

        size = am.calculate_position_size(asset, capital, strategy_type=strat, signal=sig)
        if size > 0:
            _open(om, asset, size, strat, leverage=lev,
                  entry_price=(sig.price if sig else 100.0))

        assert am.pool_used("spot") <= spot_pool_cap + 1e-6, (
            f"spot pool overrun: used={am.pool_used('spot')} cap={spot_pool_cap}"
        )
        assert am.pool_used("derivative") <= deriv_pool_cap + 1e-6, (
            f"deriv pool overrun: used={am.pool_used('derivative')} cap={deriv_pool_cap}"
        )

        # Random close
        if rng.random() < 0.3:
            om.positions.pop(asset, None)


def test_calculate_size_never_below_min_or_above_max():
    om = FakeOrderManager()
    am = AllocationManager(order_manager=om)
    rng = random.Random(13)

    for i in range(500):
        asset = f"X{i}"
        capital = rng.uniform(50, 5000)
        sig = _mk_deriv_signal(
            asset, entry=rng.uniform(1, 1000),
            sl=rng.uniform(0.5, 990),
            lev=rng.choice([1, 2, 3, 5]),
        )
        size = am.calculate_position_size(asset, capital, "derivative", sig)
        assert size == 0 or size >= MIN_POSITION_SIZE_USD - 1e-9, size
        assert size <= MAX_POSITION_SIZE_USD + 1e-9, size


def test_close_releases_pool_fully_no_leak():
    """Regression for the -7.67 leak: a closed position must contribute ZERO
    pool usage (no residual reservation)."""
    om = FakeOrderManager()
    am = AllocationManager(order_manager=om)

    _open(om, "A", 20.0, "spot")
    _open(om, "B", 15.0, "spot")
    assert am.pool_used("spot") == pytest.approx(35.0)

    om.positions.pop("A", None)
    assert am.pool_used("spot") == pytest.approx(15.0)
    om.positions.pop("B", None)
    assert am.pool_used("spot") == 0.0, "closed positions must leave zero residual usage"


def test_spot_pool_exhaustion_then_recovery():
    """Fill up to 3 spot slots → next sizing must return 0; close one → can
    size again."""
    om = FakeOrderManager()
    am = AllocationManager(order_manager=om)
    capital = 2000.0

    for asset in ("A", "B", "C"):
        s = am.calculate_position_size(asset, capital, "spot")
        assert s > 0, f"slot for {asset} unexpectedly 0"
        _open(om, asset, s, "spot")

    # 4th attempt: no slot left (only 3 slots in SPOT['allocation_split']).
    s4 = am.calculate_position_size("D", capital, "spot")
    assert s4 == 0, f"expected pool exhaustion → 0, got {s4}"

    om.positions.pop("A", None)
    s_after = am.calculate_position_size("D", capital, "spot")
    assert s_after > 0, "expected recovery after close"
