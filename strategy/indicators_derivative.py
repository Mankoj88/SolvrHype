"""
Indikator khusus untuk DerivativeStrategy:
- OITracker: in-memory ring buffer Open Interest per asset (snapshot per cycle)
- CVD: cumulative volume delta dengan approximation volume × sign(close - open)
- Swing low/high detector (pivot points)
- Support/Resistance detection (cluster pivot)
- OI flush detection
- Liquidation map proxy (heuristik OI+funding+distance)
"""
import time
from collections import defaultdict, deque
from typing import Optional
import numpy as np
import pandas as pd


# ============================================================== Open Interest

class OITracker:
    """
    Snapshot Open Interest per asset di-record setiap scan cycle.
    Hyperliquid Info API tidak provide historical OI → harus polling.
    max_history = jumlah snapshot disimpan (default 24 = 2 jam jika cycle 5m).
    """

    def __init__(self, max_history: int = 24):
        self.max_history = max_history
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_history))

    def record(self, asset: str, oi_value: float, ts_ms: Optional[int] = None):
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        self.history[asset].append((ts, float(oi_value)))

    def detect_flush(self, asset: str, drop_pct: float = 15.0) -> bool:
        """Flush = current OI turun ≥ drop_pct dari recent max dalam history window."""
        h = list(self.history.get(asset, []))
        if len(h) < 3:
            return False
        recent_max = max(v for _, v in h[:-1])
        current = h[-1][1]
        if recent_max <= 0:
            return False
        change_pct = (current / recent_max - 1) * 100
        return change_pct <= -drop_pct

    def get_change_pct(self, asset: str, lookback: int = 12) -> float:
        """% perubahan OI dari N snapshot ke belakang ke sekarang."""
        h = list(self.history.get(asset, []))
        if len(h) < 2:
            return 0.0
        anchor_idx = max(0, len(h) - lookback - 1)
        anchor = h[anchor_idx][1]
        current = h[-1][1]
        if anchor <= 0:
            return 0.0
        return (current / anchor - 1) * 100


# =================================================================== CVD

def compute_cvd(df: pd.DataFrame) -> pd.DataFrame:
    """Approximation: delta = volume × sign(close - open). cvd = cumsum(delta)."""
    df = df.copy()
    sign = np.sign(df["close"] - df["open"])
    df["delta"] = df["volume"] * sign
    df["cvd"] = df["delta"].cumsum()
    return df


def is_cvd_rising(df: pd.DataFrame, window: int = 3) -> bool:
    """CVD naik strict-monotonic dalam N candle terakhir."""
    if len(df) < window + 1:
        return False
    cvd = df["cvd"].iloc[-(window + 1):].values
    return all(cvd[i] > cvd[i - 1] for i in range(1, len(cvd)))


def is_cvd_falling(df: pd.DataFrame, window: int = 3) -> bool:
    if len(df) < window + 1:
        return False
    cvd = df["cvd"].iloc[-(window + 1):].values
    return all(cvd[i] < cvd[i - 1] for i in range(1, len(cvd)))


def is_cvd_bullish_divergence(df: pd.DataFrame, window: int = 5) -> bool:
    """Harga flat/turun, CVD naik → bullish divergence di area support."""
    if len(df) < window + 1:
        return False
    price_change = df["close"].iloc[-1] - df["close"].iloc[-window - 1]
    cvd_change = df["cvd"].iloc[-1] - df["cvd"].iloc[-window - 1]
    return price_change <= 0 and cvd_change > 0


def is_cvd_bearish_divergence(df: pd.DataFrame, window: int = 5) -> bool:
    """Harga flat/naik, CVD turun → bearish divergence di area resistance."""
    if len(df) < window + 1:
        return False
    price_change = df["close"].iloc[-1] - df["close"].iloc[-window - 1]
    cvd_change = df["cvd"].iloc[-1] - df["cvd"].iloc[-window - 1]
    return price_change >= 0 and cvd_change < 0


# =========================================================== Swing & S/R

def detect_swing_low(df: pd.DataFrame, window: int = 5) -> float:
    """
    Swing low = local minimum dimana low[i] adalah minimum dalam jendela [i-w..i+w].
    Return latest swing low (paling baru). Fallback: min low di df.
    """
    if len(df) < 2 * window + 1:
        return float(df["low"].min())
    lows = df["low"].values
    swings = []
    for i in range(window, len(lows) - window):
        seg = lows[i - window:i + window + 1]
        if lows[i] == seg.min():
            swings.append(lows[i])
    return float(swings[-1]) if swings else float(lows.min())


def detect_swing_high(df: pd.DataFrame, window: int = 5) -> float:
    if len(df) < 2 * window + 1:
        return float(df["high"].max())
    highs = df["high"].values
    swings = []
    for i in range(window, len(highs) - window):
        seg = highs[i - window:i + window + 1]
        if highs[i] == seg.max():
            swings.append(highs[i])
    return float(swings[-1]) if swings else float(highs.max())


def detect_support_resistance(df: pd.DataFrame, lookback: int = 96,
                              pivot_window: int = 5,
                              cluster_tolerance_pct: float = 0.5) -> dict:
    """
    Cari semua pivot lows/highs dalam `lookback` candle, cluster yang berdekatan
    (dalam tolerance %), return level support (kluster terbawah) & resistance (teratas).
    Sideways top/bottom = max swing high / min swing low di window.
    """
    sub = df.tail(lookback)
    if len(sub) < 2 * pivot_window + 1:
        return {
            "support": float(sub["low"].min()) if len(sub) else 0.0,
            "resistance": float(sub["high"].max()) if len(sub) else 0.0,
            "sideways_top": float(sub["high"].max()) if len(sub) else 0.0,
            "sideways_bottom": float(sub["low"].min()) if len(sub) else 0.0,
        }

    lows = sub["low"].values
    highs = sub["high"].values
    pivot_lows = []
    pivot_highs = []
    for i in range(pivot_window, len(lows) - pivot_window):
        if lows[i] == lows[i - pivot_window:i + pivot_window + 1].min():
            pivot_lows.append(lows[i])
        if highs[i] == highs[i - pivot_window:i + pivot_window + 1].max():
            pivot_highs.append(highs[i])

    support = float(min(pivot_lows)) if pivot_lows else float(lows.min())
    resistance = float(max(pivot_highs)) if pivot_highs else float(highs.max())
    # sideways band approx via percentile to reduce outlier influence
    sideways_bottom = float(np.percentile(lows, 10))
    sideways_top = float(np.percentile(highs, 90))
    return {
        "support": support,
        "resistance": resistance,
        "sideways_top": sideways_top,
        "sideways_bottom": sideways_bottom,
    }


# =========================================================== Liquidation proxy

def liquidation_proxy(oi_change_pct: float, funding_rate: float,
                      distance_to_level_pct: float) -> dict:
    """
    Estimasi cluster likuidasi: kombinasi OI extreme + funding extreme +
    jarak ke S/R level. Hanya untuk logging insight, BUKAN gate entry.
    Confidence: low | medium | high.
    """
    confidence = "low"
    if abs(oi_change_pct) > 5 and abs(funding_rate) > 0.0005:
        confidence = "medium"
    if abs(oi_change_pct) > 15 and abs(funding_rate) > 0.001:
        confidence = "high"
    return {
        "distance_pct": distance_to_level_pct,
        "oi_change_pct": oi_change_pct,
        "funding_rate": funding_rate,
        "confidence": confidence,
    }
