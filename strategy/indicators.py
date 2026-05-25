"""
Technical indicators untuk Solvira.
Pakai library `ta` (industry standard, pure Python, no compatibility issues).

Parameter sekarang dilewatkan via function args supaya bisa dipakai oleh
multiple strategies dengan setting berbeda (spot 10/5/5 vs default 14/3/3).
"""
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

    k_curr = df["stoch_rsi_k"]
    k_prev = df["stoch_rsi_k"].shift(1)
    d_curr = df["stoch_rsi_d"]
    d_prev = df["stoch_rsi_d"].shift(1)

    df["stoch_golden_cross"] = (
        (k_curr > d_curr) &
        (k_prev <= d_prev) &
        (k_curr < oversold) &
        (d_curr < oversold)
    )
    return df


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

    df["macd_reversal"] = (
        (df["macd_hist"] > 0) &
        (df["macd_hist"].shift(1) <= 0)
    )
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
    """All conditions must be true for entry."""
    return (
        bool(latest_row["stoch_golden_cross"]) and
        bool(latest_row["macd_reversal"]) and
        bool(latest_row["volume_spike"])
    )
