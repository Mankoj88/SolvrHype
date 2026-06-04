"""
Technical indicators untuk Solvira.
Pakai library `ta` (industry standard, pure Python, no compatibility issues).

Parameter sekarang dilewatkan via function args supaya bisa dipakai oleh
multiple strategies dengan setting berbeda (spot 10/5/5 vs default 14/3/3).
"""
import numpy as np
import pandas as pd
from ta.momentum import StochRSIIndicator
from ta.trend import MACD


def compute_stoch_rsi(df: pd.DataFrame, length: int = 14, k_smooth: int = 3,
                      d_smooth: int = 3, oversold: float = 20) -> pd.DataFrame:
    """
    Compute Stochastic RSI K & D lines + golden cross flag (<oversold).
    """
    indicator = StochRSIIndicator(
        close=df["close"],
        window=length,
        smooth1=k_smooth,
        smooth2=d_smooth,
    )
    df["stoch_rsi_k"] = indicator.stochrsi_k() * 100
    df["stoch_rsi_d"] = indicator.stochrsi_d() * 100
    return compute_stoch_golden_cross(df, oversold=oversold)


def compute_stoch_golden_cross(df: pd.DataFrame, oversold: float = 20) -> pd.DataFrame:
    """Flag a Stoch-RSI golden cross in oversold territory.

    Operates on existing `stoch_rsi_k`/`stoch_rsi_d` columns so it can be
    exercised with synthetic K/D series (regression tests) as well as from
    compute_stoch_rsi().
    """
    k_curr = df["stoch_rsi_k"]
    k_prev = df["stoch_rsi_k"].shift(1)
    d_curr = df["stoch_rsi_d"]
    d_prev = df["stoch_rsi_d"].shift(1)

    # Golden cross = K crosses above D. Confirm oversold on the bar BEFORE the
    # cross (k_prev/d_prev, where K was still below D) — at the cross bar K may
    # already have risen to/through the threshold (PENGU: K jumped 0 -> 20 on
    # the cross). Use `<=` so a value sitting exactly on the threshold (e.g.
    # k_prev=20) still counts as oversold.
    df["stoch_golden_cross"] = (
        (k_curr > d_curr) &
        (k_prev <= d_prev) &
        (k_prev <= oversold) &
        (d_prev <= oversold)
    )
    return df


def compute_psar(df, step=0.02, max_step=0.2):
    """Parabolic SAR series aligned to df index. Uses ta.trend.PSARIndicator."""
    from ta.trend import PSARIndicator
    ind = PSARIndicator(high=df["high"], low=df["low"], close=df["close"],
                        step=step, max_step=max_step, fillna=False)
    return ind.psar()


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> pd.DataFrame:
    """MACD with histogram reversal detection (negatif → positif)."""
    indicator = MACD(
        close=df["close"],
        window_slow=slow,
        window_fast=fast,
        window_sign=signal,
    )
    df["macd"] = indicator.macd()
    df["macd_signal"] = indicator.macd_signal()
    df["macd_hist"] = indicator.macd_diff()

    # Strict zero-cross reversal — kept as a DIAGNOSTIC field only. The spot
    # entry gate no longer requires it (it was too strict and produced 0 trades).
    df["macd_reversal"] = (
        (df["macd_hist"] > 0) &
        (df["macd_hist"].shift(1) <= 0)
    )
    # Histogram "shortening"/rising: bar-over-bar increase. Negative bars getting
    # shorter OR turning positive both count — no zero-cross required.
    df["macd_hist_rising"] = df["macd_hist"] > df["macd_hist"].shift(1)
    return df


def detect_volume_spike(df: pd.DataFrame, lookback: int = 3,
                        multiplier: float = 1.5) -> pd.DataFrame:
    """Volume spike vs N candle sebelumnya."""
    avg_vol = df["volume"].rolling(lookback).mean().shift(1)
    df["volume_spike"] = df["volume"] > (avg_vol * multiplier)
    return df


def compute_all_indicators(df: pd.DataFrame, *,
                           stoch_length: int = 14, stoch_k: int = 3, stoch_d: int = 3,
                           stoch_oversold: float = 20,
                           macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9,
                           vol_lookback: int = 3, vol_multiplier: float = 1.5) -> pd.DataFrame:
    """Pipeline: compute all indicators in correct order. Returns NEW DataFrame."""
    df = df.copy()
    df = compute_stoch_rsi(df, length=stoch_length, k_smooth=stoch_k,
                           d_smooth=stoch_d, oversold=stoch_oversold)
    df = compute_macd(df, fast=macd_fast, slow=macd_slow, signal=macd_signal)
    df = detect_volume_spike(df, lookback=vol_lookback, multiplier=vol_multiplier)
    return df


def is_entry_signal(latest_row: pd.Series) -> bool:
    """All conditions must be true for entry.

    LEGACY single-candle gate — used by strategy/scanner.py. Kept unchanged.
    The SPOT path uses the windowed is_spot_entry_signal() below instead.
    """
    return (
        bool(latest_row["stoch_golden_cross"]) and
        bool(latest_row["macd_reversal"]) and
        bool(latest_row["volume_spike"])
    )


# ===========================================================================
# SPOT windowed entry detection (closed candles only).
#
# Background: the legacy gate required Stoch-RSI golden cross, MACD zero-cross,
# and a volume spike to ALL land on the single entry candle (iloc[-2]). That is
# vanishingly rare and produced 0 spot trades for days. The spot path now
# confirms conditions 2-4 over a short window of recent CLOSED candles.
#
# Closed-candle contract: the latest closed candle is iloc[-2]; iloc[-1] is the
# in-progress candle and is EXCLUDED from every window below.
# ===========================================================================

import config


def compute_volume_spike_sma(df: pd.DataFrame, period: int = 20,
                             multiplier: float = 1.5) -> pd.DataFrame:
    """Per-bar volume spike vs a trailing SMA (shifted by 1 to exclude self).

    Separate from detect_volume_spike() (which the derivative path depends on)
    so the spot SMA-window logic never alters derivative behaviour.
    """
    vol_sma = df["volume"].rolling(period).mean().shift(1)
    df["volume_sma"] = vol_sma
    df["volume_spike"] = df["volume"] > (vol_sma * multiplier)
    return df


def compute_spot_indicators(df: pd.DataFrame, *,
                            stoch_length: int, stoch_k: int, stoch_d: int,
                            stoch_oversold: float,
                            macd_fast: int, macd_slow: int, macd_signal: int,
                            volume_sma_period: int,
                            volume_spike_multiplier: float) -> pd.DataFrame:
    """SPOT indicator pipeline. Returns a NEW DataFrame with all spot columns.

    Adds: stoch_rsi_k/d, stoch_golden_cross, macd/_signal/_hist, macd_reversal
    (diagnostic), macd_hist_rising, volume_sma, volume_spike.
    """
    df = df.copy()
    df = compute_stoch_rsi(df, length=stoch_length, k_smooth=stoch_k,
                           d_smooth=stoch_d, oversold=stoch_oversold)
    df = compute_macd(df, fast=macd_fast, slow=macd_slow, signal=macd_signal)
    df = compute_volume_spike_sma(df, period=volume_sma_period,
                                  multiplier=volume_spike_multiplier)
    return df


def evaluate_spot_conditions(
    df: pd.DataFrame,
    drop_pct: float,
    *,
    min_drop_pct: float = None,
    stoch_oversold: float = None,
    stoch_cross_lookback: int = None,
    macd_hist_rising_bars: int = None,
    volume_spike_window: int = None,
) -> dict:
    """Evaluate the 4 SPOT buy conditions over a window of CLOSED candles.

    `df` must already have spot indicator columns (see compute_spot_indicators).
    Returns a dict of named booleans so callers can log WHICH condition failed.
    Defaults are pulled from config so callers may pass just (df, drop_pct).

    Note on condition 1 (24h drop): `drop_pct` is computed upstream from the
    exchange ctx (markPx/prevDayPx) — a close-to-close ~24h approximation of the
    chart's "24h Change". A rolling 288-bar (24h of 5m) computation is preferred
    but would require fetching 288 candles/asset; the ctx value is used instead.
    """
    if min_drop_pct is None:
        min_drop_pct = config.SPOT["min_daily_drop_pct"]
    if stoch_oversold is None:
        stoch_oversold = config.SPOT["stoch_rsi_oversold"]
    if stoch_cross_lookback is None:
        stoch_cross_lookback = config.SPOT["stoch_cross_lookback"]
    if macd_hist_rising_bars is None:
        macd_hist_rising_bars = config.SPOT["macd_hist_rising_bars"]
    if volume_spike_window is None:
        volume_spike_window = config.SPOT["volume_spike_window"]

    result = {"drop": False, "stoch_xover": False,
              "macd_rising": False, "volume_spike": False}

    # Closed-candle contract: latest closed == iloc[-2]; the in-progress candle
    # iloc[-1] is EXCLUDED from every window via the `:-1` upper bound. We slice
    # individual columns (no full-frame copy) to keep the per-asset hot path
    # cheap — this runs for every survivor on every scan cycle.
    if len(df) < 3:
        return result

    # Condition 1 — 24h drop.
    result["drop"] = drop_pct <= -min_drop_pct

    cols = df.columns

    # Operate on raw numpy arrays sliced to the in-progress-excluding window
    # ([..:-1]) to avoid per-call pandas Series allocation — this runs for every
    # survivor on every scan cycle.

    # Condition 2 — Stoch-RSI golden cross in oversold within lookback window.
    # compute_stoch_rsi already requires K<oversold AND D<oversold at the cross.
    if "stoch_golden_cross" in cols:
        gc = df["stoch_golden_cross"].values[-(stoch_cross_lookback + 1):-1]
        result["stoch_xover"] = bool(gc.any())

    # Condition 3 — MACD histogram rising (no zero-cross required). Need the last
    # (bars+1) CLOSED values ending at iloc[-2] -> slice [-(bars+2):-1].
    if "macd_hist" in cols:
        seg = df["macd_hist"].values[-(macd_hist_rising_bars + 2):-1]
        if len(seg) >= macd_hist_rising_bars + 1 and not np.isnan(seg).any():
            result["macd_rising"] = bool((np.diff(seg) > 0).all())

    # Condition 4 — volume spike anywhere within the trailing window.
    if "volume_spike" in cols:
        vs = df["volume_spike"].values[-(volume_spike_window + 1):-1]
        result["volume_spike"] = bool(np.nan_to_num(vs).any())

    return result


def is_spot_entry_signal(df: pd.DataFrame, drop_pct: float, **kwargs) -> bool:
    """Pure SPOT entry predicate: TRUE only if ALL FOUR conditions hold.

    Conditions are evaluated over a short window of CLOSED candles (see
    evaluate_spot_conditions). Pure and unit-testable: takes a DataFrame +
    drop_pct, returns bool.
    """
    conds = evaluate_spot_conditions(df, drop_pct, **kwargs)
    return all(conds.values())


def signal_strength(df: pd.DataFrame, drop_pct: float = 0.0, **kwargs) -> float:
    """Coarse 0..1 confidence for a spot setup. Defensive against missing cols.

    Combines how many of the 4 conditions are met with the drop magnitude.
    Never raises on absent indicator columns (returns a partial score).
    """
    try:
        conds = evaluate_spot_conditions(df, drop_pct, **kwargs)
    except Exception:
        return 0.0
    met = sum(1 for v in conds.values() if v)
    base = met / 4.0
    # Small bonus for a deeper dip (capped) — purely diagnostic.
    depth_bonus = min(max(-drop_pct, 0.0) / 100.0, 0.25)
    return round(min(base + base * depth_bonus, 1.0), 4)
