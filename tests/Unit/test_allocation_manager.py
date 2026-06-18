"""
Unit tests — execution/allocation_manager.py

Test IDs: A1–A7 from solvira_stress_test_master.md §3.5.
Capital tier rules (CRITICAL — hard-coded business logic):
  • <$500   → 1 asset, [1.0]
  • $500    → 2 assets, [0.40, 0.60]
  • $700    → 3 assets, [0.30, 0.30, 0.40]
  • $1500+  → 4 assets, [0.25, 0.25, 0.25, 0.25]
"""

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.blocker]


class TestCapitalTiers:

    @pytest.mark.parametrize("balance,expected", [
        (50,    [1.0]),
        (300,   [1.0]),
        (499,   [1.0]),
        (500,   [0.40, 0.60]),
        (699,   [0.40, 0.60]),
        (700,   [0.30, 0.30, 0.40]),
        (1499,  [0.30, 0.30, 0.40]),
        (1500,  [0.25, 0.25, 0.25, 0.25]),
        (10000, [0.25, 0.25, 0.25, 0.25]),
    ])
    def test_tier_boundaries(self, balance, expected):
        # from config import get_allocation
        # assert get_allocation(balance) == expected
        pytest.skip("Wire get_allocation()")

    def test_allocation_sum_equals_one(self):
        # from config import get_allocation
        # for capital in [100, 500, 700, 1500, 5000]:
        #     assert sum(get_allocation(capital)) == pytest.approx(1.0, abs=1e-9)
        pytest.skip("Wire get_allocation()")

    def test_below_minimum_raises_or_returns_single(self):
        """Balance < $500 — either raise ValueError or return [1.0]."""
        pytest.skip("Wire get_allocation()")


class TestAllocationSizing:

    def test_position_size_respects_max_cap(self):
        """No single position > MAX_POSITION_SIZE_USD."""
        # from execution.allocation_manager import compute_position_sizes
        # sizes = compute_position_sizes(balance=10000, n=4)
        # assert all(s <= MAX_POSITION_SIZE_USD for s in sizes)
        pytest.skip("Wire compute_position_sizes()")

    def test_size_below_min_returns_zero(self):
        """If allocation < MIN_POSITION_SIZE_USD, slot returns 0."""
        pytest.skip("Wire compute_position_sizes()")

    def test_capital_one_position_existing(self):
        """A2: capital $1000, 1 position open → next slot = $400 (0.40)."""
        pytest.skip("Wire allocation manager open-positions awareness")

    def test_capital_all_slots_filled(self):
        """A3: capital $1000, 2 positions open → 0 (no further entries)."""
        pytest.skip("Wire allocation manager open-positions awareness")


class TestAssetWhitelistRespected:

    def test_only_whitelisted_assets_allocated(self, fake_hyperliquid_meta):
        """Allocator must reject non-whitelisted symbols."""
        # from execution.allocation_manager import select_assets
        # selected = select_assets(["BTC", "ETH", "SHITCOIN"], n=2)
        # assert "SHITCOIN" not in selected
        pytest.skip("Wire select_assets()")

    def test_already_reserved_asset_returns_zero(self, make_position):
        """A4: asset already has open position → reserve, return 0."""
        pytest.skip("Wire allocation reserve check")


class _FakeOM:
    """Minimal order_manager: only `.positions` is read by AllocationManager."""
    def __init__(self):
        from execution.order_manager import Position  # noqa: F401
        self.positions = {}


def _add_spot(om, asset, notional_usd, entry_price=100.0):
    from execution.order_manager import Position
    size_coin = notional_usd / entry_price
    om.positions[asset] = Position(
        asset=asset, entry_price=entry_price,
        entry_size_coin=size_coin, remaining_size_coin=size_coin,
        entry_size_usd=notional_usd, entry_time_ms=0,
        tp_levels_remaining=[], strategy_type="spot", leverage=1,
    )


class TestPoolUsageDerivedFromPositions:
    """pool_used/pool_capacity are DERIVED from live open positions, so a
    closed position leaves ZERO residual usage (regression for the -7.67 leak
    where release() was never called and reservations leaked forever)."""

    def test_capacity_full_when_no_positions(self):
        from execution.allocation_manager import AllocationManager
        from config import STRATEGY_POOL_SPLIT
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        capital = 125.74
        assert am.pool_used("spot") == 0.0
        # capacity = capital × 0.50 ≈ 62.87
        assert am.pool_capacity("spot", capital) == pytest.approx(
            capital * STRATEGY_POOL_SPLIT["spot"]
        )
        assert am.pool_capacity("spot", capital) == pytest.approx(62.87)

    def test_one_position_reduces_capacity(self):
        from execution.allocation_manager import AllocationManager
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        capital = 125.74
        _add_spot(om, "SOL", 20.0)
        assert am.pool_used("spot") == pytest.approx(20.0)
        # 62.87 - 20 = 42.87
        assert am.pool_capacity("spot", capital) == pytest.approx(42.87)

    def test_closed_position_frees_pool_no_leak(self):
        from execution.allocation_manager import AllocationManager
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        capital = 125.74
        _add_spot(om, "SOL", 20.0)
        assert am.pool_capacity("spot", capital) == pytest.approx(42.87)
        # Position closes → removed from the live book → usage vanishes.
        om.positions.pop("SOL", None)
        assert am.pool_used("spot") == 0.0
        assert am.pool_capacity("spot", capital) == pytest.approx(62.87)

    def test_partial_tp_frees_proportional_share(self):
        """Selling half (remaining_size_coin halved) frees half the pool."""
        from execution.allocation_manager import AllocationManager
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        _add_spot(om, "SOL", 20.0)
        om.positions["SOL"].remaining_size_coin /= 2  # TP1 sold half
        assert am.pool_used("spot") == pytest.approx(10.0)

    def test_derivative_counts_margin_not_leveraged_notional(self):
        """entry_size_usd is leveraged notional; pool must count margin."""
        from execution.allocation_manager import AllocationManager
        from execution.order_manager import Position
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        # 5x: $10 margin → $50 notional → 0.5 coin @ $100
        om.positions["ETH"] = Position(
            asset="ETH", entry_price=100.0,
            entry_size_coin=0.5, remaining_size_coin=0.5,
            entry_size_usd=50.0, entry_time_ms=0,
            tp_levels_remaining=[], strategy_type="derivative", leverage=5,
        )
        assert am.pool_used("derivative") == pytest.approx(10.0)
        assert am.pool_used("spot") == 0.0  # pool isolation

    def test_held_asset_sizes_to_zero(self):
        """Anti-duplicate: an asset already in the position book → size 0."""
        from execution.allocation_manager import AllocationManager
        om = _FakeOM()
        am = AllocationManager(order_manager=om)
        _add_spot(om, "SOL", 20.0)
        assert am.calculate_position_size("SOL", 2000.0, "spot") == 0
