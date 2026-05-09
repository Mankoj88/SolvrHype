"""
Technical indicators untuk Solvira.
Pakai library `ta` (industry standard, pure Python, no compatibility issues).
"""
import pandas as pd
from ta.momentum import StochRSIIndicator
from ta.trend import MACD
from config import (
    STOCH_RSI_LENGTH, STOCH_RSI_K_SMOOTH, STOCH_RSI_D_SMOOTH,
    STOCH_RSI_OVERSOLD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    VOLUME_SPIKE_LOOKBACK, VOLUME_SPIKE_MULTIPLIER
)


def compute_stoch_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Stochastic RSI K & D lines.
    Golden cross: K crosses ABOVE D, dengan keduanya <20.
    """
    indicator = StochRSIIndicator(
        close=df["close"],
        window=STOCH_RSI_LENGTH,
        smooth1=STOCH_RSI_K_SMOOTH,
        smooth2=STOCH_RSI_D_SMOOTH,
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
        (k_curr < STOCH_RSI_OVERSOLD) &
        (d_curr < STOCH_RSI_OVERSOLD)
    )
    return df


def compute_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    MACD with histogram reversal detection.
    Reversal: histogram crosses dari negatif ke positif.
    """
    indicator = MACD(
        close=df["close"],
        window_slow=MACD_SLOW,
        window_fast=MACD_FAST,
        window_sign=MACD_SIGNAL,
    )
    df["macd"] = indicator.macd()
    df["macd_signal"] = indicator.macd_signal()
    df["macd_hist"] = indicator.macd_diff()
    
    df["macd_reversal"] = (
        (df["macd_hist"] > 0) &
        (df["macd_hist"].shift(1) <= 0)
    )
    return df


def detect_volume_spike(df: pd.DataFrame) -> pd.DataFrame:
    """Volume spike vs N candle sebelumnya (default 3)."""
    avg_vol = df["volume"].rolling(VOLUME_SPIKE_LOOKBACK).mean().shift(1)
    df["volume_spike"] = df["volume"] > (avg_vol * VOLUME_SPIKE_MULTIPLIER)
    return df


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pipeline: compute all indicators in correct order. Returns NEW DataFrame."""
    df = df.copy()
    df = compute_stoch_rsi(df)
    df = compute_macd(df)
    df = detect_volume_spike(df)
    return df


def is_entry_signal(latest_row: pd.Series) -> bool:
    """All conditions must be true for entry."""
    return (
        bool(latest_row["stoch_golden_cross"]) and
        bool(latest_row["macd_reversal"]) and
        bool(latest_row["volume_spike"])
    )