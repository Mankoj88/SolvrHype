"""
Regression / AUDIT — equivalence between the TWO spot entry-gate implementations.

There are currently two parallel spot entry gates:

  (1) INLINE gate in strategy/spot_strategy.py scan() — the PRODUCTION path.
      Builds: cond_below_ema, cond_ema_cross, green, stoch_xover, macd_turn,
      vol_burst and ANDs all six.
  (2) evaluate_spot_conditions() / is_spot_entry_signal() in
      strategy/indicators.py — currently used ONLY by regression tests.

Goal of the *future* refactor: have scan() call the helper so there is one
source of truth. This file is the SAFETY AUDIT that must run first: it proves
whether the two gates make IDENTICAL pass/fail decisions.

VERDICT (see module-level KNOWN_DIFFERENCES): they are NOT equivalent. This
file pins each divergence so the refactor cannot silently change behaviour —
every KNOWN DIFFERENCE below is asserted, so if a future change accidentally
reconciles (or worsens) a gap, the corresponding test will start failing and
force a conscious decision.

Approach for invoking the inline path:
  scan()'s inline gate is embedded in a method that performs live API candle
  fetches, so it is not callable in isolation. We therefore REPLICATE the exact
  inline expressions (verbatim from spot_strategy.py scan(), lines ~232-258) in
  `inline_gate_decision()` below — same helper calls, same config keys, same
  iloc[-2] / window slicing. The helper path is called directly via
  is_spot_entry_signal().

Closed-candle contract: latest CLOSED candle is iloc[-2]; iloc[-1] is the
in-progress candle and is excluded by both gates.
"""
import numpy as np
import pandas as pd
import pytest

from config import SPOT
from strategy.indicators import (
    is_green_candle, is_stoch_golden_cross_now, is_macd_turning_from_negative,
    has_concentrated_volume_burst, is_spot_entry_signal,
)

pytestmark = [pytest.mark.regression]


# ---------------------------------------------------------------------------
# KNOWN DIFFERENCES catalogued by the Phase-A audit (asserted individually
# below). Kept as documentation so the report and the tests stay in sync.
# ---------------------------------------------------------------------------
KNOWN_DIFFERENCES = {
    "green": "Inline ANDs is_green_candle(); the helper has NO green key at all.",
    "stoch_xover": (
        "Inline = is_stoch_golden_cross_now(): on-the-fly cross AT iloc[-2] only. "
        "Helper = windowed .any() over the precomputed stoch_golden_cross column "
        "across the last stoch_cross_lookback(=2) closed candles (iloc[-3:-1])."
    ),
    "macd": (
        "Inline = is_macd_turning_from_negative(): hist[-2] < 0 AND "
        "|hist[-2]| < |hist[-3]| (must still be negative). "
        "Helper = macd_rising: hist[-2] > hist[-3] only (no sign constraint)."
    ),
    "volume": (
        "Inline = has_concentrated_volume_burst(): 1..max_bars(=10) raw-volume "
        "bars above mult*mean over a 72-bar window EXCLUDING the entry candle "
        "(iloc[-74:-2]); has an UPPER bound. "
        "Helper = volume_spike: .any() over the precomputed volume_spike column "
        "in a 48-bar window INCLUDING the entry candle (iloc[-49:-1]); NO upper "
        "bound."
    ),
    "volume_spike_column": (
        "scan() never calls compute_volume_spike_sma(), so the production df has "
        "NO volume_spike column -> the helper's volume_spike condition is always "
        "False on the real scan df, while the inline path reads raw volume."
    ),
}


# ---------------------------------------------------------------------------
# Gate wrappers
# ---------------------------------------------------------------------------

def inline_gate_decision(df: pd.DataFrame) -> bool:
    """Replicates the EXACT inline gate from spot_strategy.scan() (6-AND)."""
    close_last = float(df["close"].iloc[-2])
    ema_fast_last = float(df["ema_fast"].iloc[-2])
    ema_slow_last = float(df["ema_slow"].iloc[-2])
    cond_below_ema = close_last < ema_slow_last
    cond_ema_cross = ema_fast_last < ema_slow_last
    cond_green = is_green_candle(df)
    cond_stoch = is_stoch_golden_cross_now(df, oversold=SPOT["stoch_rsi_oversold"])
    cond_macd = is_macd_turning_from_negative(df)
    cond_vol = has_concentrated_volume_burst(
        df,
        lookback=SPOT["volume_lookback_candles"],
        multiplier=SPOT["volume_burst_multiplier"],
        min_bars=SPOT["volume_burst_min_bars"],
        max_bars=SPOT["volume_burst_max_bars"],
    )
    return all([cond_below_ema, cond_ema_cross, cond_green,
                cond_stoch, cond_macd, cond_vol])


def helper_gate_decision(df: pd.DataFrame) -> bool:
    """The helper path. drop_pct is accepted but no longer gates (EMA replaced it)."""
    return bool(is_spot_entry_signal(df, drop_pct=-3.0))


# ---------------------------------------------------------------------------
# Fixture builder — hand-sets every indicator column BOTH gates read so the
# comparison isolates the gate LOGIC (not `ta`'s internal math nor a column
# that one path happens not to compute). >=70 rows for the EMA60 contract.
# Baseline = a fully VALID setup (all six inline conditions True). Individual
# tests then mutate exactly one aspect.
# ---------------------------------------------------------------------------

def _valid_setup_df(n: int = 90) -> pd.DataFrame:
    df = pd.DataFrame({
        "open": np.full(n, 100.0),
        "high": np.full(n, 101.0),
        "low": np.full(n, 99.0),
        "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })
    # green: entry candle (iloc[-2]) close 100 > open 99
    df.iloc[-2, df.columns.get_loc("open")] = 99.0

    # EMA pullback: price below an elevated slow EMA; fast EMA below slow.
    df["ema_slow"] = 103.0
    df["ema_fast"] = 101.0

    # Stoch: an on-the-fly golden cross AT iloc[-2] (for the inline path) AND
    # the precomputed column flagged at iloc[-2] (for the helper path).
    df["stoch_rsi_k"] = 10.0
    df["stoch_rsi_d"] = 12.0
    df["stoch_k"] = 10.0
    df["stoch_d"] = 12.0
    df.iloc[-3, df.columns.get_loc("stoch_k")] = 5.0   # k_prev <= d_prev
    df.iloc[-3, df.columns.get_loc("stoch_d")] = 8.0
    df.iloc[-2, df.columns.get_loc("stoch_k")] = 15.0  # k_now > d_now, both < 20
    df.iloc[-2, df.columns.get_loc("stoch_d")] = 10.0
    df["stoch_golden_cross"] = False
    df.iloc[-2, df.columns.get_loc("stoch_golden_cross")] = True

    # MACD histogram: negative everywhere, strictly rising (never crosses 0).
    df["macd_hist"] = np.linspace(-5.0, -1.0, n)

    # Volume: one concentrated burst ~20 bars before entry (raw + column).
    df["volume_sma"] = 1000.0
    df["volume_spike"] = False
    df.iloc[-22, df.columns.get_loc("volume")] = 5000.0
    df.iloc[-22, df.columns.get_loc("volume_spike")] = True
    return df


# ---------------------------------------------------------------------------
# AGREEMENT cases — both gates must reach the same decision.
# ---------------------------------------------------------------------------

def test_valid_full_setup_both_pass():
    """Carefully aligned valid setup: both gates agree -> True."""
    df = _valid_setup_df()
    assert inline_gate_decision(df) is True
    assert helper_gate_decision(df) is True


def test_flat_neutral_series_both_fail():
    """A flat series (no cross, no burst, price == EMAs) fails both gates."""
    n = 90
    df = pd.DataFrame({
        "open": np.full(n, 100.0), "high": np.full(n, 101.0),
        "low": np.full(n, 99.0), "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })
    df["ema_slow"] = 100.0          # close == ema_slow -> below_ema60 False (both)
    df["ema_fast"] = 100.0
    df["stoch_rsi_k"] = 50.0; df["stoch_rsi_d"] = 50.0
    df["stoch_k"] = 50.0; df["stoch_d"] = 50.0
    df["stoch_golden_cross"] = False
    df["macd_hist"] = np.full(n, -1.0)   # flat -> not rising
    df["volume_sma"] = 1000.0
    df["volume_spike"] = False
    assert inline_gate_decision(df) is False
    assert helper_gate_decision(df) is False


def test_uptrend_no_setup_both_fail():
    """Clean uptrend (price above EMAs, fast above slow): both fail on EMA gate."""
    df = _valid_setup_df()
    df["ema_slow"] = 90.0          # price 100 > slow -> below_ema60 False (both)
    df["ema_fast"] = 95.0          # fast > slow -> ema_fast_below_slow False (both)
    assert inline_gate_decision(df) is False
    assert helper_gate_decision(df) is False


# ---------------------------------------------------------------------------
# KNOWN DIFFERENCES — each asserts the two gates DISAGREE for a documented
# reason. These are NOT bugs being hidden; they pin the audit so the future
# refactor cannot change behaviour unnoticed.
# ---------------------------------------------------------------------------

def test_known_diff_green_only():
    """Red entry candle: inline vetoes on green; helper has no green key -> passes."""
    df = _valid_setup_df()
    df.iloc[-2, df.columns.get_loc("open")] = 101.0   # close 100 < open 101 -> red
    assert inline_gate_decision(df) is False, "inline must veto a red entry candle"
    assert helper_gate_decision(df) is True, (
        "helper has no green condition -> red candle still passes (KNOWN DIFFERENCE)"
    )


def test_known_diff_stoch_windowed_vs_at_entry():
    """Stoch cross 1 candle BEFORE entry: in the helper's window, not at inline's -2."""
    df = _valid_setup_df()
    # No on-the-fly cross at iloc[-2] (keep k below d everywhere)...
    df["stoch_k"] = 10.0; df["stoch_d"] = 12.0
    # ...but the precomputed column flags the cross at iloc[-3] (inside the
    # helper's stoch_cross_lookback=2 window iloc[-3:-1]).
    df["stoch_golden_cross"] = False
    df.iloc[-3, df.columns.get_loc("stoch_golden_cross")] = True
    assert inline_gate_decision(df) is False, "inline only sees a cross AT iloc[-2]"
    assert helper_gate_decision(df) is True, (
        "helper windows the cross over the last 2 closed candles (KNOWN DIFFERENCE)"
    )


def test_known_diff_macd_sign_constraint():
    """MACD rising THROUGH zero: inline requires hist<0; helper only requires rising."""
    df = _valid_setup_df()
    df["macd_hist"] = np.linspace(-5.0, 3.0, len(df))   # rising, ends positive
    assert inline_gate_decision(df) is False, (
        "inline requires hist[-2] < 0 (still-negative momentum)"
    )
    assert helper_gate_decision(df) is True, (
        "helper macd_rising only checks hist[-2] > hist[-3] (KNOWN DIFFERENCE)"
    )


def test_known_diff_volume_upper_bound():
    """Many high-volume bars: inline's concentrated-burst upper bound vetoes; helper .any() passes."""
    df = _valid_setup_df()
    v_loc = df.columns.get_loc("volume")
    s_loc = df.columns.get_loc("volume_spike")
    for j in range(-74, -2, 4):          # ~18 high-volume bars (> max_bars=10)
        df.iloc[j, v_loc] = 5000.0
        df.iloc[j, s_loc] = True
    assert inline_gate_decision(df) is False, (
        "inline has_concentrated_volume_burst caps at max_bars=10"
    )
    assert helper_gate_decision(df) is True, (
        "helper volume_spike is an unbounded .any() over the window (KNOWN DIFFERENCE)"
    )


def test_known_diff_volume_spike_column_absent_in_scan_df():
    """scan() never computes the volume_spike column the helper depends on."""
    df = _valid_setup_df().drop(columns=["volume_spike"])
    assert inline_gate_decision(df) is True, (
        "inline reads RAW volume -> still a valid burst without the column"
    )
    assert helper_gate_decision(df) is False, (
        "helper needs the volume_spike column (never produced by scan) -> False "
        "(KNOWN DIFFERENCE / structural gap)"
    )


# ---------------------------------------------------------------------------
# Catalog guard — make the divergence list explicit in test output.
# ---------------------------------------------------------------------------

def test_audit_reports_differences_exist():
    """The gates are NOT equivalent; this asserts the audit verdict is non-empty."""
    assert len(KNOWN_DIFFERENCES) >= 1, (
        "Phase-A verdict: DIFFERENCES FOUND. Do NOT refactor scan() onto the "
        "helper until these are reconciled: " + ", ".join(KNOWN_DIFFERENCES)
    )
