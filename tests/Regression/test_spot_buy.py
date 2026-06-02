"""
Regression — SPOT BUY path (windowed confirmation + small-pool sizing).

Proves the two blockers behind "0 spot trades for days":

  (a) Windowed entry detection. A Stoch-RSI golden cross 2 candles BEFORE the
      entry candle, a RISING (but still-negative) MACD histogram, and a volume
      spike ~20 candles earlier must yield a BUY signal. The legacy single-
      candle gate (is_entry_signal on iloc[-2]) misses all three -> 0 trades.

  (b) Small-pool sizing. A ~$20 spot pool must size a tradeable order at/above
      the exchange minimum, not return 0.

Both `test_windowed_entry_signal_fires` and `test_small_spot_pool_sizes_*`
FAIL against pre-fix code and PASS after the fix. `test_old_single_candle_*`
documents the buggy legacy behaviour (passes both before and after).

Closed-candle contract: latest CLOSED candle is iloc[-2]; iloc[-1] is the
in-progress candle and must be ignored by entry detection.
"""
import numpy as np
import pandas as pd
import pytest

pytestmark = [pytest.mark.regression, pytest.mark.blocker]


def _windowed_setup_df() -> pd.DataFrame:
    """Synthetic closed-candle history matching the spec scenario.

    Indicator columns are set explicitly so the test targets the WINDOW/gate
    logic (the actual bug), not `ta`'s internal math. Row layout (iloc):
      -1  : in-progress candle (must be ignored by entry detection)
      -2  : latest CLOSED candle == entry candle
      -4  : Stoch-RSI golden cross (2 candles before the entry candle)
      -22 : volume spike (~20 candles before entry, inside the 48-bar window)
    MACD histogram is negative everywhere but strictly rising at the entry
    candle (no zero-cross) -> legacy macd_reversal=False, new rising=True.
    """
    n = 60
    df = pd.DataFrame({
        "open": np.full(n, 100.0),
        "high": np.full(n, 101.0),
        "low": np.full(n, 99.0),
        "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })

    # Stoch RSI: oversold throughout; golden cross flagged only at iloc[-4].
    df["stoch_rsi_k"] = 10.0
    df["stoch_rsi_d"] = 12.0
    df["stoch_golden_cross"] = False
    df.iloc[-4, df.columns.get_loc("stoch_golden_cross")] = True

    # MACD histogram: all negative, strictly rising toward (never crossing) 0.
    hist = np.linspace(-5.0, -1.0, n)   # monotonically increasing, stays < 0
    df["macd"] = hist
    df["macd_signal"] = 0.0
    df["macd_hist"] = hist
    df["macd_reversal"] = False          # never crosses zero -> legacy gate fails

    # Volume spike only at iloc[-22] (~20 bars before the entry candle).
    df["volume_sma"] = 1000.0
    df["volume_spike"] = False
    df.iloc[-22, df.columns.get_loc("volume_spike")] = True
    df.iloc[-22, df.columns.get_loc("volume")] = 5000.0
    return df


# ---------------------------------------------------------------------------
# (a) Windowed entry detection
# ---------------------------------------------------------------------------

def test_old_single_candle_gate_misses_windowed_setup():
    """Legacy iloc[-2] gate sees nothing here -> this is the 0-trades blocker."""
    from strategy.indicators import is_entry_signal
    df = _windowed_setup_df()
    assert is_entry_signal(df.iloc[-2]) is False


def test_windowed_entry_signal_fires():
    """After fix: ALL FOUR conditions satisfied across the window -> signal."""
    try:
        from strategy.indicators import is_spot_entry_signal
    except ImportError:
        is_spot_entry_signal = None
    assert is_spot_entry_signal is not None, (
        "is_spot_entry_signal() not implemented yet (windowed SPOT entry "
        "detection missing) -> SPOT buy path cannot fire"
    )

    df = _windowed_setup_df()
    # Condition 1 (24h drop) is computed upstream from ctx; pass a qualifying
    # drop so the pure function can evaluate conditions 2-4 over the window.
    assert is_spot_entry_signal(df, drop_pct=-3.0) is True


def test_windowed_signal_requires_all_four():
    """AND-logic guard: drop_pct failing condition 1 must veto the signal."""
    try:
        from strategy.indicators import is_spot_entry_signal
    except ImportError:
        pytest.fail("is_spot_entry_signal() not implemented yet")

    df = _windowed_setup_df()
    # drop_pct above the threshold (i.e. not a real dip) -> no entry.
    assert is_spot_entry_signal(df, drop_pct=+1.0) is False


# ---------------------------------------------------------------------------
# (b) Small-pool sizing
# ---------------------------------------------------------------------------

def test_small_spot_pool_sizes_tradeable_order():
    """A ~$20 spot pool must size at/above the $10 exchange minimum, not 0.

    Pre-fix: $50 hard floor -> capacity $20 < $50 -> 0 (the blocker).
    Post-fix: sized at/above the $10 exchange minimum.
    """
    from execution.allocation_manager import AllocationManager
    am = AllocationManager()
    # 50/50 pool split: total equity $40 -> spot pool = $20.
    size = am.calculate_position_size("SOL", total_capital=40.0, strategy_type="spot")
    assert size >= 10.0, (
        f"$20 spot pool returned {size}; expected >= $10 exchange minimum"
    )


def test_small_spot_pool_respects_max_cap():
    """Sizing the small pool must never exceed pool capacity."""
    from execution.allocation_manager import AllocationManager
    am = AllocationManager()
    size = am.calculate_position_size("SOL", total_capital=40.0, strategy_type="spot")
    assert size <= 20.0 + 1e-9, f"size {size} exceeded $20 spot pool capacity"
