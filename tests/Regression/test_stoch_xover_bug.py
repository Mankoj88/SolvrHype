"""
Regression — Stoch-RSI golden-cross detection (PENGU 2026-06-02 miss).

Evidence (bot log, PENGU, 5m closed candles):
  14:09 -> stoch_k=0.0,  stoch_d=7.9   (K < D, both oversold)
  14:10 -> stoch_k=20.0, stoch_d=9.8   (K > D -> golden cross; D still < 20)
  14:10 verdict: stoch_xover=False     <-- BUG

Root cause: the golden-cross formula confirmed oversold on the CROSS bar with a
STRICT `k_curr < oversold`. At the cross K has already risen to 20.0, so
`20.0 < 20` is False and the cross is dropped. The correct semantic checks
oversold on the bar BEFORE the cross (where K was still below D) using `<=`.

Closed-candle contract: latest CLOSED candle is iloc[-2]; iloc[-1] is the
in-progress candle and must be ignored by entry detection.
"""
import numpy as np
import pandas as pd
import pytest

pytestmark = [pytest.mark.regression]


def _pengu_df() -> pd.DataFrame:
    """Synthetic Stoch-RSI history matching PENGU's exact K/D pattern.

    Row layout (iloc):
      -5 : k=0.0,  d=7.9   (oversold, K below D)
      -4 : k=0.0,  d=7.9   (oversold, K below D)  <- pre-cross bar
      -3 : k=20.0, d=9.8   <- CROSS bar (K crosses above D; D still < 20)
      -2 : k=20.0, d=9.8   <- latest CLOSED candle
      -1 : k=20.0, d=9.8   <- in-progress candle (excluded from the window)

    K/D are injected directly so the test targets the cross/threshold logic,
    not `ta`'s internal Stoch-RSI math.
    """
    n = 8
    df = pd.DataFrame({
        "open": np.full(n, 100.0),
        "high": np.full(n, 101.0),
        "low": np.full(n, 99.0),
        "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })
    df["stoch_rsi_k"] = 0.0
    df["stoch_rsi_d"] = 7.9

    k_loc = df.columns.get_loc("stoch_rsi_k")
    d_loc = df.columns.get_loc("stoch_rsi_d")
    for i in (-3, -2, -1):
        df.iloc[i, k_loc] = 20.0
        df.iloc[i, d_loc] = 9.8

    # EMA trend gate (new spec): set to PASSING (pullback) so the EMA conditions
    # don't interfere — this test isolates the stoch golden-cross detection.
    # close (100) < ema_slow (103) and ema_fast (101) < ema_slow (103).
    df["ema_slow"] = 103.0
    df["ema_fast"] = 101.0
    return df


def test_pengu_golden_cross_flagged_at_cross_bar():
    """The cross bar (iloc[-3]) must be flagged as a Stoch-RSI golden cross."""
    from strategy.indicators import compute_stoch_golden_cross
    df = compute_stoch_golden_cross(_pengu_df(), oversold=20)
    gc = df["stoch_golden_cross"].values
    assert bool(gc[-3]) is True, (
        "golden cross at the cross bar (k=20 over d=9.8, pre-cross both<20) "
        "was not flagged -> windowed stoch_xover will miss it"
    )


def test_pengu_stoch_xover_true_in_window():
    """evaluate_spot_conditions must report stoch_xover=True for PENGU."""
    from strategy.indicators import compute_stoch_golden_cross, evaluate_spot_conditions
    df = compute_stoch_golden_cross(_pengu_df(), oversold=20)
    conds = evaluate_spot_conditions(df, drop_pct=-3.0)
    assert conds["stoch_xover"] is True, (
        "PENGU golden cross within the lookback window was not detected "
        f"(stoch_xover={conds['stoch_xover']})"
    )
