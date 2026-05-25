"""
SpotStrategy — spot long-only di Hyperliquid Perp leverage 1x.

Pipeline (per spec user):
  universe (dinamis) →
  filter 7d avg daily volume >$100K →
  filter funding rate safe (reuse rule lama) →
  fetch 5m candles (lookback 120) →
  filter daily drop >-2% vs close kemarin →
  compute Stoch RSI 10/5/5 + MACD + volume spike vs 3 candle →
  entry condition: K>D & both<20 & MACD hist neg→pos & vol spike →
  emit TradeSignal(strategy_type="spot", leverage=1, sl_mode="pct", is_long=True)
Return max 3 signal.
"""
import time
from typing import Optional
import pandas as pd
from loguru import logger
from hyperliquid.info import Info

from config import (
    SPOT, get_api_url,
    MAX_ACCEPTABLE_FUNDING_RATE_HOURLY, NEVER_TRADE_FUNDING_WINDOW_MINUTES,
)
from strategy.base_strategy import BaseStrategy, TradeSignal
from strategy.indicators import compute_all_indicators, is_entry_signal
from strategy.universe import UniverseFetcher


# Hyperliquid timeframe → milliseconds (untuk start_ms calc)
TIMEFRAME_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


class SpotStrategy(BaseStrategy):
    strategy_type = "spot"

    def __init__(self, info: Info = None, universe: UniverseFetcher = None):
        self._info = info or Info(get_api_url(), skip_ws=True)
        self._universe = universe or UniverseFetcher(self._info)
        self._candles_cache: dict[str, tuple] = {}          # asset → (df, ts)
        self._volume_7d_cache: dict[str, tuple] = {}        # asset → (avg, ts)

    # ------------------------------------------------------------------ filters

    def _passes_7d_volume(self, asset: str) -> bool:
        """Avg daily volume 7 hari >$100K. Cache 1 jam."""
        now = time.time()
        cached = self._volume_7d_cache.get(asset)
        if cached and (now - cached[1]) < 3600:
            avg = cached[0]
        else:
            end_ms = int(now * 1000)
            start_ms = end_ms - 8 * 86400 * 1000   # 8 hari buffer
            try:
                candles_1d = self._info.candles_snapshot(asset, "1d", start_ms, end_ms)
            except Exception as e:
                logger.debug(f"{asset}: 7d volume fetch error: {e}")
                return False
            if not candles_1d:
                return False
            recent = candles_1d[-7:] if len(candles_1d) >= 7 else candles_1d
            # candle "v" = volume coin × close (Hyperliquid normalizes to $ notional via field n? no — v is coin)
            # Hyperliquid candles_snapshot returns 'v' as coin volume + 'n' as trade count.
            # For $ notional approx: sum(v_i * close_i). Closer approximation:
            try:
                avg = sum(float(c["v"]) * float(c["c"]) for c in recent) / len(recent)
            except (KeyError, ValueError):
                return False
            self._volume_7d_cache[asset] = (avg, now)
        return avg >= SPOT["min_7d_avg_daily_volume_usd"]

    def _passes_drop(self, asset: str) -> tuple[bool, float]:
        """Daily drop >-2% vs close kemarin."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 3 * 86400 * 1000
        try:
            candles_1d = self._info.candles_snapshot(asset, "1d", start_ms, end_ms)
        except Exception as e:
            logger.debug(f"{asset}: drop fetch error: {e}")
            return False, 0.0
        if len(candles_1d) < 2:
            return False, 0.0
        prev_close = float(candles_1d[-2]["c"])
        curr_close = float(candles_1d[-1]["c"])
        if prev_close <= 0:
            return False, 0.0
        drop_pct = (curr_close / prev_close - 1) * 100
        return drop_pct <= -SPOT["min_daily_drop_pct"], drop_pct

    def _passes_funding(self, ctx: dict) -> bool:
        """Funding window guard (jangan trade dekat funding payment)."""
        try:
            funding = float(ctx.get("funding", 0))
            if abs(funding) > MAX_ACCEPTABLE_FUNDING_RATE_HOURLY:
                return False
            now_utc = time.gmtime()
            mins = now_utc.tm_min
            to_next = 60 - mins
            if to_next < NEVER_TRADE_FUNDING_WINDOW_MINUTES or mins < NEVER_TRADE_FUNDING_WINDOW_MINUTES:
                return False
            return True
        except (KeyError, ValueError, TypeError):
            return True

    # ------------------------------------------------------------------ candles

    def _fetch_5m_candles(self, asset: str) -> Optional[pd.DataFrame]:
        now = time.time()
        cached = self._candles_cache.get(asset)
        # cache 30s — cycle 60s, jadi hampir selalu fresh
        if cached and (now - cached[1]) < 30:
            return cached[0]

        tf = SPOT["timeframe"]
        lookback = SPOT["candle_lookback"]
        end_ms = int(now * 1000)
        start_ms = end_ms - lookback * TIMEFRAME_MS[tf]
        try:
            candles = self._info.candles_snapshot(asset, tf, start_ms, end_ms)
        except Exception as e:
            logger.debug(f"{asset}: 5m fetch error: {e}")
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

    # ------------------------------------------------------------------ scan

    def scan(self) -> list[TradeSignal]:
        signals = []
        for asset, ctx in self._universe.iter_assets():
            if not self._passes_funding(ctx):
                continue
            if not self._passes_7d_volume(asset):
                continue
            drop_ok, drop_pct = self._passes_drop(asset)
            if not drop_ok:
                continue

            df = self._fetch_5m_candles(asset)
            if df is None:
                continue
            df = compute_all_indicators(
                df,
                stoch_length=SPOT["stoch_rsi_length"],
                stoch_k=SPOT["stoch_rsi_k_smooth"],
                stoch_d=SPOT["stoch_rsi_d_smooth"],
                stoch_oversold=SPOT["stoch_rsi_oversold"],
                macd_fast=SPOT["macd_fast"],
                macd_slow=SPOT["macd_slow"],
                macd_signal=SPOT["macd_signal"],
                vol_lookback=SPOT["vol_spike_lookback"],
                vol_multiplier=SPOT["vol_spike_multiplier"],
            )
            if len(df) < 5:
                continue
            latest = df.iloc[-2]   # closed candle
            if not is_entry_signal(latest):
                continue

            vol_ratio = float(latest["volume"] / df["volume"].iloc[-5:-2].mean()) \
                if df["volume"].iloc[-5:-2].mean() > 0 else 0.0
            signal = TradeSignal(
                asset=asset,
                price=float(latest["close"]),
                timestamp_ms=int(latest.name),
                reason="spot:stoch_xover+macd_rev+vol_spike",
                indicators_snapshot={
                    "stoch_k": float(latest["stoch_rsi_k"]),
                    "stoch_d": float(latest["stoch_rsi_d"]),
                    "macd_hist": float(latest["macd_hist"]),
                    "volume_ratio": vol_ratio,
                    "drop_pct": drop_pct,
                    "timeframe": SPOT["timeframe"],
                    "stoch_params": [SPOT["stoch_rsi_length"], SPOT["stoch_rsi_k_smooth"], SPOT["stoch_rsi_d_smooth"]],
                },
                strategy_type="spot",
                leverage=SPOT["leverage"],
                is_long=True,
                sl_mode="pct",
            )
            signals.append(signal)
            logger.info(f"📊 SPOT SIGNAL: {asset} @ ${signal.price:.4f}")
            if len(signals) >= 3:
                break
        return signals
