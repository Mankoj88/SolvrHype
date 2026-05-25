"""
DerivativeStrategy — perp leverage di Hyperliquid, long + short simetris.

Pipeline (per spec user):
  universe → record OI snapshot → cek funding rate sign →
  cek OI flush ≥15% → fetch 5m candles → compute CVD →
  detect S/R + swing → cek harga di area S/R (±1%) →
  entry condition: Early (CVD divergence) atau Confirmed (breakout + volume) →
  SL = swing low/high terbaru →
  leverage = min(5, floor(0.015 / sl_distance_pct)) →
  emit TradeSignal(strategy_type="derivative", sl_mode="swing_low|swing_high")

Long setup (entry di Support): funding < -0.05% + OI flush + CVD bullish divergence
Short setup (entry di Resistance): funding > +0.05% + OI flush + CVD bearish divergence
Return max 2 signal per scan.
"""
import math
import time
from typing import Optional
import pandas as pd
from loguru import logger
from hyperliquid.info import Info

from config import DERIVATIVE, get_api_url
from strategy.base_strategy import BaseStrategy, TradeSignal
from strategy.universe import UniverseFetcher
from strategy.indicators import detect_volume_spike
from strategy.indicators_derivative import (
    OITracker, compute_cvd,
    is_cvd_bullish_divergence, is_cvd_bearish_divergence,
    detect_swing_low, detect_swing_high, detect_support_resistance,
    liquidation_proxy,
)


TIMEFRAME_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


class DerivativeStrategy(BaseStrategy):
    strategy_type = "derivative"

    def __init__(self, info: Info = None, universe: UniverseFetcher = None):
        self._info = info or Info(get_api_url(), skip_ws=True)
        self._universe = universe or UniverseFetcher(self._info)
        self._candles_cache: dict[str, tuple] = {}
        self.oi_tracker = OITracker(max_history=DERIVATIVE["oi_flush_lookback_candles"] * 2)

    # ------------------------------------------------------------------ candles

    def _fetch_5m_candles(self, asset: str) -> Optional[pd.DataFrame]:
        now = time.time()
        cached = self._candles_cache.get(asset)
        if cached and (now - cached[1]) < 30:
            return cached[0]
        tf = DERIVATIVE["timeframe"]
        lookback = DERIVATIVE["candle_lookback"]
        end_ms = int(now * 1000)
        start_ms = end_ms - lookback * TIMEFRAME_MS[tf]
        try:
            candles = self._info.candles_snapshot(asset, tf, start_ms, end_ms)
        except Exception as e:
            logger.debug(f"{asset}: deriv 5m fetch error: {e}")
            return None
        if not candles or len(candles) < 50:
            return None
        df = pd.DataFrame([
            {"time": int(c["t"]), "open": float(c["o"]), "high": float(c["h"]),
             "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"])}
            for c in candles
        ])
        df.set_index("time", inplace=True)
        self._candles_cache[asset] = (df, now)
        return df

    # ------------------------------------------------------------------ helpers

    def _compute_safe_leverage(self, entry_price: float, sl_price: float) -> int:
        """leverage = min(max_lev, floor(risk_per_trade_pct / sl_distance_pct)). Min 1x."""
        if entry_price <= 0 or sl_price <= 0:
            return 1
        sl_distance_pct = abs(entry_price - sl_price) / entry_price
        if sl_distance_pct <= 0:
            return 1
        risk_pct = DERIVATIVE["risk_per_trade_pct"] / 100
        raw = risk_pct / sl_distance_pct
        return max(1, min(DERIVATIVE["max_leverage"], math.floor(raw)))

    def _at_level(self, price: float, level: float) -> bool:
        if level <= 0:
            return False
        return abs(price - level) / price <= DERIVATIVE["support_proximity_pct"]

    # ------------------------------------------------------------------ scan

    def scan(self) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        now_ms = int(time.time() * 1000)

        for asset, ctx in self._universe.iter_assets():
            try:
                oi = float(ctx.get("openInterest", 0))
            except (TypeError, ValueError):
                oi = 0
            if oi > 0:
                self.oi_tracker.record(asset, oi, now_ms)

            try:
                funding = float(ctx.get("funding", 0))
            except (TypeError, ValueError):
                continue

            long_setup = funding < DERIVATIVE["funding_rate_negative_threshold"]
            short_setup = funding > DERIVATIVE["funding_rate_positive_threshold"]
            if not (long_setup or short_setup):
                continue

            if not self.oi_tracker.detect_flush(asset, DERIVATIVE["oi_flush_drop_pct"]):
                continue

            df = self._fetch_5m_candles(asset)
            if df is None:
                continue
            df = compute_cvd(df)
            df = detect_volume_spike(df, lookback=DERIVATIVE["cvd_rising_window"], multiplier=1.5)

            sr = detect_support_resistance(
                df,
                lookback=DERIVATIVE["support_resistance_lookback_candles"],
                pivot_window=DERIVATIVE["support_resistance_pivot_window"],
            )
            current_price = float(df["close"].iloc[-1])
            oi_change = self.oi_tracker.get_change_pct(asset, DERIVATIVE["oi_flush_lookback_candles"])

            signal = None
            if long_setup and self._at_level(current_price, sr["support"]):
                signal = self._try_long_signal(asset, df, sr, current_price, funding, oi_change, now_ms)
            elif short_setup and self._at_level(current_price, sr["resistance"]):
                signal = self._try_short_signal(asset, df, sr, current_price, funding, oi_change, now_ms)

            if signal:
                signals.append(signal)
                logger.info(
                    f"📊 DERIV SIGNAL: {'LONG' if signal.is_long else 'SHORT'} "
                    f"{asset} @ ${signal.price:.4f} SL=${signal.suggested_sl_price:.4f} "
                    f"lev={signal.leverage}x ({signal.reason})"
                )
                if len(signals) >= 2:
                    break
        return signals

    # --------------------------------------------------------------- long path

    def _try_long_signal(self, asset, df, sr, current_price, funding, oi_change, now_ms) -> Optional[TradeSignal]:
        early = is_cvd_bullish_divergence(df, DERIVATIVE["cvd_rising_window"])
        confirmed = (
            bool(df["volume_spike"].iloc[-2])
            and current_price > sr["sideways_top"]
        )
        if not (early or confirmed):
            return None

        swing = detect_swing_low(df, window=DERIVATIVE["swing_pivot_window"])
        if swing <= 0 or swing >= current_price:
            return None  # invalid SL

        leverage = self._compute_safe_leverage(current_price, swing)
        distance_to_level = abs(current_price - sr["support"]) / current_price * 100

        return TradeSignal(
            asset=asset,
            price=current_price,
            timestamp_ms=now_ms,
            reason=f"deriv:{'early' if early else 'confirmed'}_long",
            indicators_snapshot={
                "entry_mode": "early" if early else "confirmed",
                "side": "long",
                "funding": funding,
                "oi_change_pct": oi_change,
                "cvd_last": float(df["cvd"].iloc[-1]),
                "support": sr["support"],
                "resistance": sr["resistance"],
                "sideways_top": sr["sideways_top"],
                "sideways_bottom": sr["sideways_bottom"],
                "swing_low": swing,
                "liquidation_proxy": liquidation_proxy(oi_change, funding, distance_to_level),
                "timeframe": DERIVATIVE["timeframe"],
            },
            strategy_type="derivative",
            leverage=leverage,
            is_long=True,
            suggested_sl_price=swing,
            sl_mode="swing_low",
        )

    # --------------------------------------------------------------- short path

    def _try_short_signal(self, asset, df, sr, current_price, funding, oi_change, now_ms) -> Optional[TradeSignal]:
        early = is_cvd_bearish_divergence(df, DERIVATIVE["cvd_rising_window"])
        confirmed = (
            bool(df["volume_spike"].iloc[-2])
            and current_price < sr["sideways_bottom"]
        )
        if not (early or confirmed):
            return None

        swing = detect_swing_high(df, window=DERIVATIVE["swing_pivot_window"])
        if swing <= 0 or swing <= current_price:
            return None  # invalid SL

        leverage = self._compute_safe_leverage(current_price, swing)
        distance_to_level = abs(current_price - sr["resistance"]) / current_price * 100

        return TradeSignal(
            asset=asset,
            price=current_price,
            timestamp_ms=now_ms,
            reason=f"deriv:{'early' if early else 'confirmed'}_short",
            indicators_snapshot={
                "entry_mode": "early" if early else "confirmed",
                "side": "short",
                "funding": funding,
                "oi_change_pct": oi_change,
                "cvd_last": float(df["cvd"].iloc[-1]),
                "support": sr["support"],
                "resistance": sr["resistance"],
                "sideways_top": sr["sideways_top"],
                "sideways_bottom": sr["sideways_bottom"],
                "swing_high": swing,
                "liquidation_proxy": liquidation_proxy(oi_change, funding, distance_to_level),
                "timeframe": DERIVATIVE["timeframe"],
            },
            strategy_type="derivative",
            leverage=leverage,
            is_long=False,
            suggested_sl_price=swing,
            sl_mode="swing_high",
        )
