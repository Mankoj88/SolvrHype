"""
SpotStrategy — spot long-only di Hyperliquid Perp leverage 1x.

ARCHITECTURE (rate-limit safe):
  Stage 1 — ctx-only pre-filter (0 API call per asset):
    funding window + dayNtlVlm + drop = markPx/prevDayPx-1
  Stage 2 — top-N survivors only:
    sort by drop_pct ASC → cap at MAX_CANDIDATES_PER_CYCLE
    fetch 5m candles → indicators → signals

Sebelum refactor, pipeline lama melakukan ~365 API calls/cycle dan kena HTTP 429.
Sekarang ≤21 calls/cycle (1 universe meta + ≤20 candles).
"""
import time
from typing import Optional
import pandas as pd
from loguru import logger
from hyperliquid.info import Info

from config import (
    SPOT, get_api_url, SCAN_MIN_24H_VOLUME_USD,
    MAX_ACCEPTABLE_FUNDING_RATE_HOURLY, NEVER_TRADE_FUNDING_WINDOW_MINUTES,
    MAX_CANDIDATES_PER_CYCLE, CANDLE_FETCH_INTER_CALL_SLEEP_SEC,
)
from strategy.base_strategy import BaseStrategy, TradeSignal
from strategy.indicators import compute_spot_indicators, evaluate_spot_conditions
from strategy.universe import UniverseFetcher


TIMEFRAME_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


class SpotStrategy(BaseStrategy):
    strategy_type = "spot"

    def __init__(self, info: Info = None, universe: UniverseFetcher = None):
        self._info = info or Info(get_api_url(), skip_ws=True)
        self._universe = universe or UniverseFetcher(self._info)
        self._candles_cache: dict[str, tuple] = {}        # asset → (df, ts)

    # =========================================================== STAGE 1
    # Ctx-only pre-filter. Tidak ada API call per asset di stage ini.

    def _passes_funding(self, ctx: dict) -> bool:
        try:
            funding = float(ctx.get("funding", 0) or 0)
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

    def _passes_volume(self, asset: str, ctx: dict) -> bool:
        # Spec gate: "24h volume > $500,000". dayNtlVlm is the 24h notional
        # volume from meta_and_asset_ctxs (0 API call per asset).
        try:
            vol = float(ctx.get("dayNtlVlm", 0) or 0)
            return vol >= SCAN_MIN_24H_VOLUME_USD
        except (ValueError, TypeError):
            return False

    def _ctx_metrics(self, ctx: dict) -> Optional[tuple[float, float, float]]:
        """Extract (mark_px, day_vol_usd, drop_pct) dari ctx. None jika data tidak valid."""
        try:
            mark = float(ctx.get("markPx", 0) or 0)
            prev = float(ctx.get("prevDayPx", 0) or 0)
            day_vol = float(ctx.get("dayNtlVlm", 0) or 0)
        except (TypeError, ValueError):
            return None
        if mark <= 0 or prev <= 0:
            return None
        drop_pct = (mark / prev - 1) * 100
        return mark, day_vol, drop_pct

    # =========================================================== STAGE 2
    # 5m candle fetch (hanya untuk top-N survivors).

    def _fetch_5m_candles(self, asset: str) -> Optional[pd.DataFrame]:
        now = time.time()
        cached = self._candles_cache.get(asset)
        if cached and (now - cached[1]) < 30:
            return cached[0]

        tf = SPOT["timeframe"]
        lookback = SPOT["candle_lookback"]
        end_ms = int(now * 1000)
        start_ms = end_ms - lookback * TIMEFRAME_MS[tf]
        try:
            candles = self._info.candles_snapshot(asset, tf, start_ms, end_ms)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate limit" in msg:
                logger.warning(f"{asset}: rate-limited on 5m fetch, will retry next cycle")
            else:
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

    # =========================================================== SCAN

    def scan(self) -> list[TradeSignal]:
        # ---- Stage 1: ctx-only pre-filter (0 API call per asset) ----
        # Counters only (no per-asset logging in hot path — avoid spam on 100+ assets)
        n_funding_fail = 0
        n_ctx_invalid = 0
        n_vol_fail = 0
        n_drop_fail = 0
        candidates: list[tuple[str, dict, float, float, float]] = []
        # (asset, ctx, mark_px, day_vol, drop_pct)
        for asset, ctx in self._universe.iter_assets():
            if not self._passes_funding(ctx):
                n_funding_fail += 1
                continue
            metrics = self._ctx_metrics(ctx)
            if metrics is None:
                n_ctx_invalid += 1
                continue
            mark, day_vol, drop_pct = metrics
            if not self._passes_volume(asset, ctx):
                n_vol_fail += 1
                continue
            if drop_pct > -SPOT["min_daily_drop_pct"]:
                n_drop_fail += 1
                continue
            candidates.append((asset, ctx, mark, day_vol, drop_pct))

        if not candidates:
            logger.info(
                f"Spot scan: 0 candidates after ctx pre-filter "
                f"(funding={n_funding_fail}, ctx_invalid={n_ctx_invalid}, "
                f"volume={n_vol_fail}, drop={n_drop_fail})"
            )
            return []

        # Sort: paling drop dulu (kondisi oversold paling kuat)
        candidates.sort(key=lambda t: t[4])
        capped = candidates[:MAX_CANDIDATES_PER_CYCLE]
        logger.info(
            f"Spot scan: {len(candidates)} ctx-filter survivors, "
            f"evaluating top {len(capped)} (cap={MAX_CANDIDATES_PER_CYCLE})"
        )

        # ---- Stage 2: 5m candles + indicators (1 API call per survivor) ----
        # Per-asset debug logging is OK here (≤ MAX_CANDIDATES_PER_CYCLE iterations).
        n_candle_fail = 0
        n_insufficient_data = 0
        n_stoch_fail = 0
        n_macd_fail = 0
        n_vol_spike_fail = 0
        signals: list[TradeSignal] = []
        for i, (asset, ctx, mark, day_vol, drop_pct) in enumerate(capped):
            # Throttle untuk smoothing burst (defense in depth)
            if i > 0 and CANDLE_FETCH_INTER_CALL_SLEEP_SEC > 0:
                time.sleep(CANDLE_FETCH_INTER_CALL_SLEEP_SEC)

            df = self._fetch_5m_candles(asset)
            if df is None:
                n_candle_fail += 1
                logger.debug(
                    f"{asset} | drop={drop_pct:+.2f}% | verdict=FAIL candle_fetch"
                )
                continue
            df = compute_spot_indicators(
                df,
                stoch_length=SPOT["stoch_rsi_length"],
                stoch_k=SPOT["stoch_rsi_k_smooth"],
                stoch_d=SPOT["stoch_rsi_d_smooth"],
                stoch_oversold=SPOT["stoch_rsi_oversold"],
                macd_fast=SPOT["macd_fast"],
                macd_slow=SPOT["macd_slow"],
                macd_signal=SPOT["macd_signal"],
                volume_sma_period=SPOT["volume_sma_period"],
                volume_spike_multiplier=SPOT["vol_spike_multiplier"],
            )
            # Need enough closed candles for the widest window (volume_spike_window).
            if len(df) < SPOT["volume_spike_window"] + 2:
                n_insufficient_data += 1
                logger.debug(
                    f"{asset} | drop={drop_pct:+.2f}% | verdict=FAIL insufficient_data "
                    f"(bars={len(df)} < {SPOT['volume_spike_window'] + 2})"
                )
                continue
            latest = df.iloc[-2]   # latest CLOSED candle (never iloc[-1])

            stoch_k = float(latest["stoch_rsi_k"]) if pd.notna(latest["stoch_rsi_k"]) else float("nan")
            stoch_d = float(latest["stoch_rsi_d"]) if pd.notna(latest["stoch_rsi_d"]) else float("nan")
            macd_hist = float(latest["macd_hist"]) if pd.notna(latest["macd_hist"]) else float("nan")
            vol_val = float(latest["volume"])

            # Windowed evaluation of all 4 conditions over recent CLOSED candles.
            conds = evaluate_spot_conditions(df, drop_pct)

            if not all(conds.values()):
                # Structured rejection: log WHICH condition(s) failed so a
                # future "no-trade" diagnosis is one log line away.
                failed = [name for name, ok in conds.items() if not ok]
                if not conds["stoch_xover"]:
                    n_stoch_fail += 1
                if not conds["macd_rising"]:
                    n_macd_fail += 1
                if not conds["volume_spike"]:
                    n_vol_spike_fail += 1
                logger.debug(
                    f"{asset} | drop={drop_pct:+.2f}% | vol={vol_val:.0f} | "
                    f"stoch_k={stoch_k:.1f} | stoch_d={stoch_d:.1f} | "
                    f"macd_hist={macd_hist:+.4f} | "
                    f"conds={conds} | verdict=FAIL {','.join(failed)}"
                )
                continue

            logger.debug(
                f"{asset} | drop={drop_pct:+.2f}% | vol={vol_val:.0f} | "
                f"stoch_k={stoch_k:.1f} | stoch_d={stoch_d:.1f} | "
                f"macd_hist={macd_hist:+.4f} | conds={conds} | verdict=PASS"
            )

            avg_prev = df["volume"].iloc[-5:-2].mean()
            vol_ratio = float(latest["volume"] / avg_prev) if avg_prev > 0 else 0.0
            signal = TradeSignal(
                asset=asset,
                price=float(latest["close"]),
                timestamp_ms=int(latest.name),
                reason="spot:stoch_xover+macd_rising+vol_spike(windowed)",
                indicators_snapshot={
                    "stoch_k": float(latest["stoch_rsi_k"]),
                    "stoch_d": float(latest["stoch_rsi_d"]),
                    "macd_hist": float(latest["macd_hist"]),
                    "volume_ratio": vol_ratio,
                    "drop_pct": drop_pct,
                    "day_ntl_vlm": day_vol,
                    "timeframe": SPOT["timeframe"],
                    "stoch_params": [
                        SPOT["stoch_rsi_length"],
                        SPOT["stoch_rsi_k_smooth"],
                        SPOT["stoch_rsi_d_smooth"],
                    ],
                },
                strategy_type="spot",
                leverage=SPOT["leverage"],
                is_long=True,
                sl_mode="pct",
            )
            signals.append(signal)
            logger.info(
                f"SIGNAL: {asset} | entry=${signal.price:.4f} | "
                f"stoch_k={stoch_k:.1f} | drop={drop_pct:+.2f}% | vol_ratio={vol_ratio:.2f}x"
            )
            if len(signals) >= 3:
                break

        # End-of-scan summary
        logger.info(
            f"Scan complete: {len(capped)} evaluated, {len(signals)} signals | "
            f"stage2_filters: stoch={n_stoch_fail}, macd={n_macd_fail}, "
            f"vol_spike={n_vol_spike_fail}, candle_fail={n_candle_fail}, "
            f"insufficient_data={n_insufficient_data} | "
            f"stage1_filters: drop={n_drop_fail}, volume={n_vol_fail}, "
            f"funding={n_funding_fail}, ctx_invalid={n_ctx_invalid}"
        )

        if not signals:
            rejections = {
                "stoch_xover": n_stoch_fail,
                "macd_rising": n_macd_fail,
                "volume_spike": n_vol_spike_fail,
                "candle_fetch": n_candle_fail,
                "insufficient_data": n_insufficient_data,
            }
            top_name, top_count = max(rejections.items(), key=lambda x: x[1])
            if top_count == 0:
                logger.info("No signals this cycle. No stage2 rejections recorded.")
            else:
                logger.info(
                    f"No signals this cycle. Top rejection reason: "
                    f"{top_name} ({top_count}/{len(capped)})"
                )
        return signals
