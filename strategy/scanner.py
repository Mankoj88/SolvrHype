"""
Scanner: filter aset → fetch candles → compute indicators → emit signals.
"""
import time
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from loguru import logger
from hyperliquid.info import Info
from config import (
    CRYPTO_WHITELIST, MIN_DAILY_VOLUME_USD, MIN_DAILY_DROP_PCT,
    CANDLE_LOOKBACK, TIMEFRAME, MAX_ACCEPTABLE_FUNDING_RATE_HOURLY,
    NEVER_TRADE_FUNDING_WINDOW_MINUTES, get_api_url
)
from strategy.indicators import compute_all_indicators, is_entry_signal


@dataclass
class TradeSignal:
    asset: str
    price: float
    timestamp_ms: int
    reason: str
    indicators_snapshot: dict


class MarketScanner:
    def __init__(self):
        self.info = Info(get_api_url(), skip_ws=True)
        self._meta_cache = None
        self._meta_cache_time = 0
        self._daily_candles_cache: dict[str, tuple] = {}  # asset → (data, ts)

    def _get_meta_with_ctx(self):
        """Cache meta dengan TTL 30 detik untuk save API calls."""
        now = time.time()
        if self._meta_cache is None or now - self._meta_cache_time > 30:
            self._meta_cache = self.info.meta_and_asset_ctxs()
            self._meta_cache_time = now
        return self._meta_cache

    def _passes_volume_filter(self, ctx: dict) -> bool:
        try:
            return float(ctx["dayNtlVlm"]) >= MIN_DAILY_VOLUME_USD
        except (KeyError, ValueError, TypeError):
            return False

    def _passes_drop_filter(self, asset: str) -> tuple[bool, float]:
        # Bug #12: cache 1d candles 10 menit (drop% changes slowly)
        now = time.time()
        cached = self._daily_candles_cache.get(asset)
        if cached and (now - cached[1]) < 600:
            candles_1d = cached[0]
        else:
            end_ms = int(now * 1000)
            start_ms = end_ms - 86400 * 3 * 1000
            try:
                candles_1d = self.info.candles_snapshot(asset, "1d", start_ms, end_ms)
                self._daily_candles_cache[asset] = (candles_1d, now)
            except Exception as e:
                logger.debug(f"{asset}: drop filter API error: {e}")
                return False, 0.0

        if len(candles_1d) < 2:
            return False, 0.0
        prev_close = float(candles_1d[-2]["c"])
        curr_close = float(candles_1d[-1]["c"])
        drop_pct = (curr_close / prev_close - 1) * 100
        return drop_pct <= -MIN_DAILY_DROP_PCT, drop_pct

    def _passes_funding_filter(self, ctx: dict) -> bool:
        """Skip kalau funding rate ekstrem atau dekat funding window.

        Bug #4: Hyperliquid funding interval = 1 jam (bukan 4 jam).
        """
        try:
            funding_rate = float(ctx.get("funding", 0))
            if abs(funding_rate) > MAX_ACCEPTABLE_FUNDING_RATE_HOURLY:
                return False

            now_utc = time.gmtime()
            # Hyperliquid funding dibayar setiap jam di top-of-hour (xx:00)
            minutes_into_hour = now_utc.tm_min
            minutes_to_next_funding = 60 - minutes_into_hour

            # Skip kalau dalam window 5 menit sebelum atau sesudah funding payment
            if (minutes_to_next_funding < NEVER_TRADE_FUNDING_WINDOW_MINUTES or
                    minutes_into_hour < NEVER_TRADE_FUNDING_WINDOW_MINUTES):
                logger.debug(f"In funding window (min into hour: {minutes_into_hour})")
                return False
            return True
        except (KeyError, ValueError, TypeError):
            return True  # default allow if data missing

    def _fetch_candles_df(self, asset: str) -> Optional[pd.DataFrame]:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (CANDLE_LOOKBACK * 10 * 60 * 1000)

        try:
            candles = self.info.candles_snapshot(asset, TIMEFRAME, start_ms, end_ms)
            if len(candles) < 50:
                return None

            df = pd.DataFrame([
                {
                    "time": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                }
                for c in candles
            ])
            df.set_index("time", inplace=True)
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch candles for {asset}: {e}")
            return None

    def scan(self) -> list[TradeSignal]:
        """Pipeline: filter → indicator → emit signals."""
        signals = []
        meta_ctx = self._get_meta_with_ctx()
        universe = meta_ctx[0]["universe"]
        contexts = meta_ctx[1]

        for asset_info, ctx in zip(universe, contexts):
            asset = asset_info["name"]

            if asset not in CRYPTO_WHITELIST:
                continue

            if not self._passes_volume_filter(ctx):
                continue

            drop_passed, drop_pct = self._passes_drop_filter(asset)
            if not drop_passed:
                continue

            if not self._passes_funding_filter(ctx):
                logger.debug(f"{asset}: failed funding filter")
                continue

            df = self._fetch_candles_df(asset)
            if df is None:
                continue

            df = compute_all_indicators(df)

            # Bug #1: pakai candle terakhir yang SUDAH CLOSE (kedua-terakhir di df)
            # df.iloc[-1] adalah candle yang sedang form (belum closed)
            if len(df) < 2:
                continue
            latest = df.iloc[-2]

            if is_entry_signal(latest):
                signal = TradeSignal(
                    asset=asset,
                    price=float(latest["close"]),
                    timestamp_ms=int(latest.name),
                    reason="stoch_xover + macd_reversal + vol_spike",
                    indicators_snapshot={
                        "stoch_k": float(latest["stoch_rsi_k"]),
                        "stoch_d": float(latest["stoch_rsi_d"]),
                        "macd_hist": float(latest["macd_hist"]),
                        # Bug #1: compare completed candle volume vs 3 candles before it
                        "volume_ratio": float(latest["volume"] / df["volume"].iloc[-5:-2].mean()),
                        "drop_pct": drop_pct,
                    }
                )
                signals.append(signal)
                logger.info(f"📊 SIGNAL: {asset} @ ${signal.price:.4f}")

        return signals
