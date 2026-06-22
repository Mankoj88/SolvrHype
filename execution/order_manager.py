"""
OrderManager: handle entry, partial TP, stop loss, max-hold dengan persistent state.

Dual-strategy support (spot + derivative):
- Per-signal leverage (1x spot, up to 5x deriv)
- TP levels format: (pct, sell_fraction_of_remaining, post_action)
  post_action: None | "breakeven"
- SL mode: "pct" (fixed % cutloss) atau "swing_low"/"swing_high" (absolute price)
- Direction-aware (long/short) — entry_long market_buy, entry_short market_sell
- Slippage guard per-signal (compare actual fill vs signal price)
- Per-strategy open position count untuk per-pool capacity tracking
- Structure break check untuk deriv: re-detect swing, jika jebol → close
"""
import time
import json
import pandas as pd
from dataclasses import dataclass, asdict, fields
from typing import Optional
from loguru import logger
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account

from config import (
    HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_ACCOUNT, get_api_url,
    TAKE_PROFITS, CUTLOSS_PCT, USE_BREAKEVEN_AFTER_TP1, MAX_HOLD_HOURS,
    MAX_OPEN_POSITIONS, MIN_POSITION_SIZE_USD, MAX_POSITION_SIZE_USD,
    LEVERAGE, USE_ISOLATED_MARGIN, SLIPPAGE_TOLERANCE, DRY_RUN, DATA_DIR,
    COOLDOWN_AFTER_CLOSE_MINUTES,
    INITIAL_CAPITAL_USD,
    SPOT, DERIVATIVE,
    PSAR_STEP, PSAR_MAX_STEP, PSAR_HOLD_MAX_HOURS,
    TAKER_FEE_RATE,
)
from strategy.base_strategy import TradeSignal


def _normalize_tp_levels(levels) -> list[tuple]:
    """Normalisasi format TP: support (pct, frac) atau (pct, frac, post_action)."""
    normalized = []
    for lvl in levels:
        if len(lvl) == 2:
            normalized.append((float(lvl[0]), float(lvl[1]), None))
        else:
            normalized.append((float(lvl[0]), float(lvl[1]),
                               lvl[2] if len(lvl) > 2 else None))
    return normalized


@dataclass
class Position:
    asset: str
    entry_price: float
    entry_size_coin: float
    entry_size_usd: float
    entry_time_ms: int
    tp_levels_remaining: list
    tp_hit_count: int = 0
    initial_sl_price: float = 0.0
    current_sl_price: float = 0.0
    sl_oid: Optional[int] = None
    remaining_size_coin: float = 0.0
    last_signal_time: Optional[str] = None
    # Dual-strategy fields (default ke spot long supaya backward-compat dengan state lama)
    strategy_type: str = "spot"
    leverage: int = 1
    is_long: bool = True
    entry_swing_price: Optional[float] = None     # swing low (long) atau high (short)
    sl_mode: str = "pct"                          # "pct" | "swing_low" | "swing_high"
    risk_usd: Optional[float] = None              # audit field untuk risk-based sizing
    # Observability (informational only — NO trading decision may read these)
    indicators_snapshot: Optional[dict] = None    # indicator values captured at entry
    max_favorable_pct: float = 0.0                # MFE: best direction-% seen while open
    max_adverse_pct: float = 0.0                  # MAE: worst direction-% seen while open
    fees_partial_usd: float = 0.0                 # accumulated partial-close fee estimates

    def __post_init__(self):
        if self.remaining_size_coin == 0.0 and self.entry_size_coin > 0:
            self.remaining_size_coin = self.entry_size_coin

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        valid = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)

    def to_dict(self):
        return asdict(self)


class OrderManager:
    STATE_FILE = DATA_DIR / "positions_state.json"
    # Bug C fix: a full close must be CONFIRMED flat on the exchange before we
    # record it closed. On an unconfirmed close, retry up to this many attempts
    # total (with a short settle pause between) before keeping the position in
    # state + alerting — never write a phantom "closed" row.
    CLOSE_MAX_ATTEMPTS = 3
    CLOSE_RETRY_SLEEP_SEC = 1.5

    def __init__(self):
        self.info = Info(get_api_url(), skip_ws=True)
        wallet = Account.from_key(HYPERLIQUID_PRIVATE_KEY)
        self.exchange = Exchange(
            wallet,
            base_url=get_api_url(),
            account_address=HYPERLIQUID_ACCOUNT,
        )
        self.positions: dict[str, Position] = self._load_state()
        self._szDecimals_cache: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

        self._reconcile_with_exchange()

    def _load_state(self) -> dict[str, Position]:
        if not self.STATE_FILE.exists():
            return {}
        try:
            with open(self.STATE_FILE) as f:
                data = json.load(f)
            valid_fields = {f.name for f in fields(Position)}
            positions = {}
            for asset, pos_dict in data.items():
                filtered = {k: v for k, v in pos_dict.items() if k in valid_fields}
                try:
                    positions[asset] = Position(**filtered)
                except TypeError as e:
                    logger.error(f"Cannot reconstruct {asset} position: {e}. Dropping.")
                    continue
            return positions
        except Exception as e:
            logger.error(f"Failed to load state: {e}, starting fresh")
            return {}

    def _save_state(self):
        data = {asset: pos.to_dict() for asset, pos in self.positions.items()}
        tmp_path = self.STATE_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(self.STATE_FILE)

    def _live_sl_orders(self) -> Optional[dict[str, int]]:
        """Map asset → oid of a LIVE resting reduce-only trigger (stop-loss) order
        on the exchange, queried via frontend_open_orders (the basic open_orders
        endpoint does NOT carry reduceOnly/isTrigger, so it can't identify an SL).

        Reconcile uses this to ADOPT an already-live hard SL instead of blanking +
        re-placing it (which left a window with no hard SL). The bot only ever
        rests a single reduce-only trigger (the SL) per position, so a reduce-only
        trigger order on an asset IS that asset's live SL.

        Returns:
          dict{asset: oid}  on a successful query (possibly empty → no live SLs).
          None              if no info client or the query failed — the caller then
                            SKIPS the SL sync this cycle (keeps existing sl_oid +
                            the soft SL) rather than risk a duplicate placement.
        """
        info = getattr(self, "info", None)
        if info is None:
            return None
        try:
            orders = info.frontend_open_orders(HYPERLIQUID_ACCOUNT)
        except Exception as e:
            logger.warning(f"frontend_open_orders query failed during reconcile: {e}")
            return None
        live: dict[str, int] = {}
        try:
            for o in orders or []:
                if not o.get("reduceOnly"):
                    continue
                if not o.get("isTrigger"):
                    continue
                coin, oid = o.get("coin"), o.get("oid")
                if coin is None or oid is None:
                    continue
                # One SL per position — keep the first live trigger seen per asset.
                live.setdefault(coin, int(oid))
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(f"Could not parse open orders during reconcile: {e}")
            return None
        return live

    def _reconcile_with_exchange(self, periodic: bool = False):
        """Two-way reconciliation between local state and the exchange.

        Runs at startup (periodic=False) and as a periodic safety net (Bug D,
        periodic=True). Both paths share this logic:
          * orphan adoption — a position on the exchange but absent from state is
            imported as spot long with a pct SL,
          * ghost cleanup — a position in state but NOT on the exchange is removed
            (NO DB close row: the real exit is unknown; an alert is fired instead),
          * SL check-before-replace — for each managed position, ADOPT the oid of a
            live reduce-only trigger (SL) if one already rests on the exchange, and
            place a fresh SL ONLY when none is live. sl_oid is never blanked first,
            so a position keeps its existing hard SL untouched (no blank-then-
            replace gap).
        """
        if DRY_RUN:
            return
        tag = "PERIODIC RECONCILE" if periodic else "STARTUP RECONCILE"
        try:
            user_state = self.info.user_state(HYPERLIQUID_ACCOUNT)
            exchange_positions = {
                p["position"]["coin"]: float(p["position"]["szi"])
                for p in user_state.get("assetPositions", [])
                if float(p["position"]["szi"]) != 0
            }

            # --- positions on both sides: sync size only (SL handled below) ---
            for asset, local_pos in list(self.positions.items()):
                if asset not in exchange_positions:
                    continue  # ghost — handled next
                exchange_size = abs(exchange_positions[asset])
                if abs(exchange_size - local_pos.remaining_size_coin) > 0.01 * local_pos.entry_size_coin:
                    logger.warning(
                        f"{tag}: SIZE MISMATCH {asset}: local={local_pos.remaining_size_coin:.4f}, "
                        f"exchange={exchange_size:.4f}. Updating local."
                    )
                    local_pos.remaining_size_coin = exchange_size
                # NOTE (Bug D): do NOT blank sl_oid here — the SL sync step below
                # adopts the live SL or places one only if missing.

            # --- ghost cleanup: in state but NOT on the exchange ---
            # The position is already gone on the exchange; we do NOT know the real
            # exit price/time, so we must NOT fabricate a DB close row (that is
            # exactly the phantom-close Bug C warned against). Remove + alert only.
            ghost_assets = [a for a in self.positions if a not in exchange_positions]
            for asset in ghost_assets:
                logger.warning(
                    f"{tag}: GHOST {asset} in state but not on exchange — removing "
                    f"from state (real exit unknown, NO DB row written)."
                )
                self.positions.pop(asset, None)
            if ghost_assets:
                self._save_state()
                try:
                    from notifications.telegram import notify_critical_error
                    notify_critical_error(
                        f"{tag}: state had {len(ghost_assets)} ghost position(s) not on "
                        f"exchange, cleaned without a DB close row (real exit unknown): "
                        f"{ghost_assets}",
                        "reconcile_ghost",
                    )
                except Exception:
                    pass

            # --- orphan adoption: on the exchange but NOT in state ---
            imported_assets = []
            for asset, szi in exchange_positions.items():
                if asset in self.positions:
                    continue
                pos_data = next(
                    (p["position"] for p in user_state.get("assetPositions", [])
                     if p["position"]["coin"] == asset
                     and float(p["position"]["szi"]) != 0),
                    None,
                )
                if pos_data is None:
                    continue
                try:
                    entry_price = float(pos_data["entryPx"])
                except (KeyError, ValueError, TypeError):
                    logger.warning(f"{tag}: skipping orphan {asset}: missing entryPx")
                    continue
                size_coin = abs(float(pos_data["szi"]))
                is_long = float(pos_data["szi"]) > 0
                sl_price = entry_price * (1 + SPOT["cutloss_pct"] / 100) if is_long \
                    else entry_price * (1 - SPOT["cutloss_pct"] / 100)
                self.positions[asset] = Position(
                    asset=asset,
                    entry_price=entry_price,
                    entry_size_coin=size_coin,
                    remaining_size_coin=size_coin,
                    entry_size_usd=size_coin * entry_price,
                    entry_time_ms=int(time.time() * 1000),
                    tp_levels_remaining=_normalize_tp_levels(SPOT["take_profits"]),
                    initial_sl_price=sl_price,
                    current_sl_price=sl_price,
                    strategy_type="spot",
                    leverage=1,
                    is_long=is_long,
                    sl_mode="pct",
                )
                imported_assets.append(asset)
                logger.warning(
                    f"{tag}: adopted orphan {asset}: {size_coin} @ "
                    f"${entry_price:.4f}, SL target=${sl_price:.4f}"
                )

            if imported_assets:
                try:
                    from notifications.telegram import notify_critical_error
                    notify_critical_error(
                        f"{tag}: adopted {len(imported_assets)} exchange orphan "
                        f"position(s) into state: {imported_assets}",
                        "reconcile_orphan",
                    )
                except Exception:
                    pass

            # --- SL check-before-replace (Bug D core) ---
            # Adopt a live reduce-only SL's oid if one already rests; place a fresh
            # SL ONLY when none is live. Never blank-then-replace → no gap with no
            # hard SL. On an open-orders query failure (None) skip the sync this
            # cycle: keep the existing sl_oid + the soft SL and retry next reconcile,
            # rather than risk placing a duplicate SL on top of a live one.
            live_sl = self._live_sl_orders()
            if live_sl is None:
                logger.warning(
                    f"{tag}: open-orders query unavailable — skipping SL sync this "
                    f"cycle (existing hard SL + soft SL retained, retry next cycle)."
                )
            else:
                for asset, pos in self.positions.items():
                    if pos.remaining_size_coin <= 0 or pos.current_sl_price <= 0:
                        continue
                    if asset in live_sl:
                        # A hard SL already rests — adopt its oid, do NOT cancel or
                        # re-place. The position is never left without an SL.
                        if pos.sl_oid != live_sl[asset]:
                            logger.info(
                                f"{tag}: {asset} already has a live SL "
                                f"(oid={live_sl[asset]}) — adopting, no re-place."
                            )
                        pos.sl_oid = live_sl[asset]
                    else:
                        logger.info(
                            f"{tag}: no live SL for {asset} — placing one "
                            f"@ ${pos.current_sl_price:.4f}."
                        )
                        pos.sl_oid = self._place_stop_loss(
                            asset, pos.remaining_size_coin, pos.current_sl_price, pos.is_long
                        )
            self._save_state()

        except Exception as e:
            logger.exception(
                f"{tag} failed: {e} — proceeding with local state (RISKY)"
            )
            try:
                from notifications.telegram import notify_critical_error
                notify_critical_error(
                    f"{tag} failed: {e} — proceeding with local state (RISKY)",
                    "reconcile_failed",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ helpers

    def open_position_count(self) -> int:
        return len(self.positions)

    def open_position_count_by_strategy(self) -> dict[str, int]:
        counts = {"spot": 0, "derivative": 0}
        for pos in self.positions.values():
            counts[pos.strategy_type] = counts.get(pos.strategy_type, 0) + 1
        return counts

    def setup_leverage_for_asset(self, asset: str, leverage: int):
        try:
            self.exchange.update_leverage(leverage, asset, is_cross=not USE_ISOLATED_MARGIN)
            logger.info(f"Leverage set: {asset} @ {leverage}x isolated")
        except Exception as e:
            logger.warning(f"Failed to set leverage for {asset}: {e}")

    def _sz_decimals(self, asset: str) -> int:
        """szDecimals for an asset, cached (one meta() round-trip per asset).

        Shared by _round_size and _round_price so the lookup/cache logic lives in
        exactly one place.
        """
        if asset not in self._szDecimals_cache:
            meta = self.info.meta()
            for asset_info in meta["universe"]:
                if asset_info["name"] == asset:
                    self._szDecimals_cache[asset] = asset_info.get("szDecimals", 4)
                    break
            else:
                self._szDecimals_cache[asset] = 4
        return self._szDecimals_cache[asset]

    def _round_size(self, asset: str, size: float) -> float:
        return round(size, self._sz_decimals(asset))

    def _round_price(self, asset: str, price: float) -> float:
        """Round a price to Hyperliquid's wire-precision rule before sending an order.

        Hyperliquid rejects any order price with more than 5 significant figures, OR
        more than (MAX_DECIMALS - szDecimals) decimal places, where MAX_DECIMALS is 6
        for perps and 8 for spot (integer prices are always allowed). The SDK applies
        this for market_open via _slippage_price, but exchange.order (our manual SL
        path) does NOT — so an unrounded SL trigger such as 1663.452 / 67.54062 was
        rejected IN-BAND by the exchange and the SL silently never landed. Mirror the
        SDK rule here so every price we submit is wire-valid.

        These positions are perp-1x (labeled "spot"), so use the PERP cap of
        (6 - szDecimals) decimals. If genuine spot markets are ever added, those
        assets need 8 instead of 6.

        Worked examples (perp):
            1663.452 (ETH, szDecimals=4) → "{:.5g}"→1663.5, round(·, 6-4=2) → 1663.5
            67.54062 (SOL, szDecimals=2) → "{:.5g}"→67.541, round(·, 6-2=4) → 67.541
            63700.0  (already valid)                                        → 63700.0
        """
        price = float(price)
        try:
            return round(float(f"{price:.5g}"), 6 - self._sz_decimals(asset))
        except Exception as e:
            logger.warning(
                f"_round_price: szDecimals lookup failed for {asset} ({e}); "
                f"falling back to 5 sig figs / 6 decimals"
            )
            return round(float(f"{price:.5g}"), 6)

    def _round_price_aggressive(self, asset: str, price: float) -> float:
        """A rounder price for a precision-rejection retry: 4 significant figures and
        one fewer decimal place than the standard rule, to clear a tick edge case
        where the precisely-rounded value is still rejected. Only used on a retry —
        never as the first attempt."""
        price = float(price)
        try:
            decimals = max(0, (6 - self._sz_decimals(asset)) - 1)
        except Exception:
            decimals = 5
        return round(float(f"{price:.4g}"), decimals)

    def _close_side(self, pos: Position) -> str:
        if pos.strategy_type == "derivative":
            return "short_close" if not pos.is_long else "long_close"
        return "long_close"

    def _partial_close_side(self, pos: Position) -> str:
        if pos.strategy_type == "derivative":
            return "short_partial_close" if not pos.is_long else "long_partial_close"
        return "long_partial_close"

    def _compute_pnl_usd(self, pos: Position, exit_price: float, size_coin: float) -> float:
        if pos.is_long:
            return (exit_price - pos.entry_price) * size_coin
        return (pos.entry_price - exit_price) * size_coin

    def _compute_pnl_pct(self, pos: Position, exit_price: float) -> float:
        if pos.entry_price <= 0:
            return 0.0
        raw = (exit_price / pos.entry_price - 1) * 100
        return raw if pos.is_long else -raw

    def _direction_pct_change(self, pos: Position, current_price: float) -> float:
        """% gain dari arah posisi: long positif jika harga naik, short positif jika harga turun."""
        if pos.entry_price <= 0:
            return 0.0
        raw = (current_price - pos.entry_price) / pos.entry_price * 100
        return raw if pos.is_long else -raw

    def _sl_triggered(self, pos: Position, current_price: float) -> bool:
        if pos.is_long:
            return current_price <= pos.current_sl_price
        return current_price >= pos.current_sl_price

    def _resolve_sl_price(self, signal: TradeSignal, actual_price: float) -> float:
        """Hitung SL price berdasarkan sl_mode signal."""
        if signal.sl_mode in ("swing_low", "swing_high") and signal.suggested_sl_price:
            return signal.suggested_sl_price
        # pct mode
        cutloss = SPOT["cutloss_pct"] if signal.strategy_type == "spot" else CUTLOSS_PCT
        if signal.is_long:
            return actual_price * (1 + cutloss / 100)
        return actual_price * (1 - cutloss / 100)

    def _resolve_tp_levels(self, signal: TradeSignal) -> list[tuple]:
        if signal.strategy_type == "spot":
            return _normalize_tp_levels(SPOT["take_profits"])
        if signal.strategy_type == "derivative":
            return _normalize_tp_levels(DERIVATIVE["take_profits"])
        return _normalize_tp_levels(TAKE_PROFITS)

    def _max_hold_hours(self, pos: Position) -> float:
        if pos.strategy_type == "spot":
            return SPOT["max_hold_hours"]
        if pos.strategy_type == "derivative":
            return DERIVATIVE["max_hold_hours"]
        return MAX_HOLD_HOURS

    def _fetch_candles_for_psar(self, asset):
        try:
            now = int(time.time() * 1000)
            start = now - 100 * 300_000  # 100 x 5m
            candles = self.info.candles_snapshot(asset, "5m", start, now)
            if not candles or len(candles) < 10:
                return None
            return pd.DataFrame([
                {"high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"])}
                for c in candles
            ])
        except Exception as e:
            logger.warning(f"PSAR candle fetch failed {asset}: {e}")
            return None

    def _psar_favors_hold(self, pos) -> bool:
        """True if PSAR still favors the trade direction (long: SAR<price, short: SAR>price).
        Conservative: if candles unavailable, return False (i.e. allow close)."""
        df = self._fetch_candles_for_psar(pos.asset)
        if df is None or len(df) < 10:
            return False
        from strategy.indicators import compute_psar
        try:
            sar = compute_psar(df, step=PSAR_STEP, max_step=PSAR_MAX_STEP)
            price = float(df["close"].iloc[-2])   # closed candle
            sar_val = float(sar.iloc[-2])
        except Exception as e:
            logger.warning(f"PSAR compute failed {pos.asset}: {e}")
            return False
        return sar_val < price if pos.is_long else sar_val > price

    # ------------------------------------------------------------------ entry

    def execute_entry(self, signal: TradeSignal, size_usd: float) -> bool:
        """Execute market open + place stop loss order."""
        if size_usd < MIN_POSITION_SIZE_USD:
            logger.warning(f"Skip {signal.asset}: size ${size_usd:.2f} < min")
            return False
        if size_usd > MAX_POSITION_SIZE_USD:
            logger.error(f"REJECTED {signal.asset}: size > MAX")
            return False
        if signal.asset in self.positions:
            return False
        if len(self.positions) >= MAX_OPEN_POSITIONS:
            return False

        cooldown_ts = self._cooldown_until.get(signal.asset, 0)
        if time.time() < cooldown_ts:
            remaining_min = (cooldown_ts - time.time()) / 60
            logger.debug(f"{signal.asset} in cooldown ({remaining_min:.1f}m remaining), skip")
            return False

        leverage = signal.leverage or 1
        self.setup_leverage_for_asset(signal.asset, leverage)

        # Notional = margin × leverage. size_usd di sini adalah margin.
        notional_usd = size_usd * leverage
        size_coin = self._round_size(signal.asset, notional_usd / signal.price)

        if DRY_RUN:
            logger.info(
                f"[DRY_RUN] Would {'BUY' if signal.is_long else 'SELL'} "
                f"{signal.asset}: {size_coin} @ ~{signal.price} ({signal.strategy_type}, {leverage}x)"
            )
            tp_levels = self._resolve_tp_levels(signal)
            sl_price = self._resolve_sl_price(signal, signal.price)
            self.positions[signal.asset] = Position(
                asset=signal.asset,
                entry_price=signal.price,
                entry_size_coin=size_coin,
                remaining_size_coin=size_coin,
                entry_size_usd=size_coin * signal.price,
                entry_time_ms=int(time.time() * 1000),
                tp_levels_remaining=tp_levels,
                initial_sl_price=sl_price,
                current_sl_price=sl_price,
                strategy_type=signal.strategy_type,
                leverage=leverage,
                is_long=signal.is_long,
                entry_swing_price=signal.suggested_sl_price if signal.sl_mode in ("swing_low", "swing_high") else None,
                sl_mode=signal.sl_mode,
                risk_usd=size_usd,
                indicators_snapshot=signal.indicators_snapshot,
            )
            self._save_state()
            return True

        try:
            result = self.exchange.market_open(
                signal.asset, signal.is_long, size_coin, None, SLIPPAGE_TOLERANCE
            )
            if result["status"] != "ok":
                logger.error(f"Entry failed: {result}")
                return False

            statuses = result["response"]["data"]["statuses"]
            filled_data = next((s["filled"] for s in statuses if "filled" in s), None)
            if filled_data is None:
                logger.error(f"No fill in response: {result}")
                return False

            actual_price = float(filled_data["avgPx"])
            actual_size = float(filled_data["totalSz"])

            # Slippage guard per-strategy (spot 0.3%)
            slippage_threshold_pct = SPOT["max_entry_slippage_pct"] \
                if signal.strategy_type == "spot" else 1.0  # 1% default untuk deriv
            slippage_pct = abs(actual_price - signal.price) / signal.price * 100
            if slippage_pct > slippage_threshold_pct:
                logger.warning(
                    f"SLIPPAGE GUARD {signal.asset}: actual fill ${actual_price:.6f} "
                    f"vs signal ${signal.price:.6f} → {slippage_pct:.2f}% > {slippage_threshold_pct}% threshold. "
                    f"Closing immediately to avoid bad entry."
                )
                # Same verify-fill guard as the full-close path (Bug C): don't
                # assume the rollback worked. If it can't be confirmed flat, the
                # bad entry may still be open on the exchange → warn + alert.
                if not self._attempt_close_until_flat(signal.asset):
                    logger.error(
                        f"Slippage-rollback close NOT confirmed for {signal.asset} — "
                        f"bad entry may still be open on exchange"
                    )
                    self._alert_close_failed(signal.asset, "slippage_rollback", retained=False)
                return False

            sl_price = self._resolve_sl_price(signal, actual_price)
            tp_levels = self._resolve_tp_levels(signal)
            position = Position(
                asset=signal.asset,
                entry_price=actual_price,
                entry_size_coin=actual_size,
                remaining_size_coin=actual_size,
                entry_size_usd=actual_size * actual_price,
                entry_time_ms=int(time.time() * 1000),
                tp_levels_remaining=tp_levels,
                initial_sl_price=sl_price,
                current_sl_price=sl_price,
                strategy_type=signal.strategy_type,
                leverage=leverage,
                is_long=signal.is_long,
                entry_swing_price=signal.suggested_sl_price if signal.sl_mode in ("swing_low", "swing_high") else None,
                sl_mode=signal.sl_mode,
                risk_usd=size_usd,
                indicators_snapshot=signal.indicators_snapshot,
            )

            sl_oid = self._place_stop_loss(signal.asset, actual_size, sl_price, signal.is_long)
            position.sl_oid = sl_oid  # real oid on success, None on failure
            # On failure _place_stop_loss has already logged the exact exchange
            # rejection reason and fired the Telegram alert (centralized). The entry
            # is NOT rolled back — the soft SL in manage_open_positions() still
            # protects this position.

            self.positions[signal.asset] = position
            self._save_state()

            side_label = "LONG" if signal.is_long else "SHORT"
            logger.info(
                f"✅ ENTERED {side_label} {signal.asset} ({signal.strategy_type} {leverage}x): "
                f"{actual_size} @ ${actual_price:.4f} SL=${sl_price:.4f}"
            )
            return True

        except Exception as e:
            logger.exception(f"Entry exception for {signal.asset}: {e}")
            return False

    @staticmethod
    def _is_price_error(text: str) -> bool:
        """Heuristic: does an exchange rejection string indicate a price/precision
        problem? If so a retry must use a ROUNDER price — re-sending the same value
        would fail identically. Otherwise the failure is treated as transient."""
        t = (text or "").lower()
        return any(k in t for k in ("price", "tick", "significant", "decimal"))

    def _submit_sl_order(self, asset: str, size: float, px: float, is_long: bool):
        """Submit ONE reduce-only stop-market order. No rounding/retry here.

        Returns (oid, reason, price_error):
          * success      → (int oid, None, False)
          * placed/acted → (None, None, False)   e.g. immediate fill, no resting oid
          * failure      → (None, reason str, price_error bool)
        The reason is ALWAYS surfaced (logged) — a precision rejection comes back as
        status="ok" with an in-band {"error": ...} status, which used to be swallowed.
        """
        try:
            result = self.exchange.order(
                asset,
                not is_long,            # opposite side
                size,
                px,                     # limit_px → float_to_wire (must be float, not str)
                {"trigger": {"isMarket": True, "triggerPx": px, "tpsl": "sl"}},
                reduce_only=True,
            )
        except Exception as e:
            # Transport/signing exception — transient, retry the same value.
            logger.exception(f"SL order exception for {asset} @ {px}: {e}")
            return None, f"exception: {e}", False

        if result.get("status") != "ok":
            logger.error(f"SL order non-ok for {asset} @ {px}: {result}")
            return None, f"status={result.get('status')} {result}", self._is_price_error(str(result))

        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        # In-band rejection (the previously-swallowed case): status ok but the order
        # was rejected, e.g. {"error": "Order has invalid price."}. ALWAYS log it.
        err = next((s["error"] for s in statuses if isinstance(s, dict) and "error" in s), None)
        if err:
            logger.error(f"SL order rejected for {asset} @ {px}: {err}")
            return None, str(err), self._is_price_error(str(err))

        resting = next((s["resting"] for s in statuses if isinstance(s, dict) and "resting" in s), None)
        if resting:
            return int(resting["oid"]), None, False

        # Immediate fill on submit (rare for a stop): the SL acted rather than
        # resting. Not a failure — there's just no resting oid to track.
        filled = next((s["filled"] for s in statuses if isinstance(s, dict) and "filled" in s), None)
        if filled:
            oid = filled.get("oid")
            logger.warning(f"SL for {asset} filled immediately on submit: {filled}")
            return (int(oid) if oid else None), None, False

        logger.error(f"SL order unexpected response for {asset} @ {px}: {result}")
        return None, f"no resting/error/filled in statuses: {statuses}", False

    def _place_stop_loss(self, asset: str, size: float, trigger_price: float,
                         is_long: bool = True) -> Optional[int]:
        """Place a hard (reduce-only stop-market) SL: long → sell when price <=
        trigger; short → buy when price >= trigger. Returns the resting oid, or None.

        The trigger/limit price is rounded to Hyperliquid's wire-precision rule via
        _round_price BEFORE sending — exchange.order does NOT auto-round the way the
        SDK's market_open does (Bug: unrounded triggers like 1663.452 / 67.54062 were
        rejected in-band and the SL silently never landed). Rounding here covers ALL
        callers (entry, reconcile, BE-stop) in one place.

        On failure we retry ONCE, but never re-send the identical request that just
        failed for a price reason: a precision rejection retries with a rounder price;
        a transient error retries the same price after a short pause. If the retry
        also fails we log the reason, fire a Telegram alert, and return None — the
        entry is NOT rolled back (the soft SL in manage_open_positions still covers).
        """
        # Bug B: float_to_wire formats every price with f"{x:.8f}" and raises on a
        # str, so the price MUST be a float. _round_price float()-coerces and rounds.
        px = self._round_price(asset, trigger_price)

        oid, reason, price_error = self._submit_sl_order(asset, size, px, is_long)
        if oid is not None:
            return oid
        if reason is None:
            return None  # placed/acted but no trackable oid — not a failure

        # --- conditional retry: only in a way that can actually succeed ---
        if price_error:
            # Precision/tick rejection — the same value would fail again. Retry with
            # a more aggressively rounded price (4 sig figs / one fewer decimal).
            retry_px = self._round_price_aggressive(asset, px)
            logger.warning(
                f"SL retry (price) {asset}: {px} rejected ({reason}) → retry @ {retry_px}"
            )
        else:
            # Transient (network/timeout/exception/non-price) — same price, brief pause.
            retry_px = px
            logger.warning(
                f"SL retry (transient) {asset}: {reason} → retry @ {retry_px} after 1s"
            )
            time.sleep(1)

        oid, reason, _ = self._submit_sl_order(asset, size, retry_px, is_long)
        if oid is not None:
            return oid
        if reason is None:
            return None

        logger.error(
            f"⚠️ Hard SL placement failed for {asset}: {reason} "
            f"(size={size}, trigger={px}) — relying on soft SL"
        )
        self._alert_sl_placement_failed(asset, reason)
        return None

    def _cancel_order(self, asset: str, oid: int):
        try:
            self.exchange.cancel(asset, oid)
        except Exception as e:
            logger.warning(f"Cancel failed: {e}")

    # ------------------------------------------------------------------ manage

    def manage_open_positions(self, current_prices: dict[str, float]):
        """Check each position untuk TP/SL/max-hold/structure-break."""
        positions_to_remove = []

        for asset, pos in list(self.positions.items()):
            if asset not in current_prices:
                continue
            current_price = current_prices[asset]
            pct_change = self._direction_pct_change(pos, current_price)
            # Observability only (MFE/MAE) — reads already-available current_price,
            # NO decision below may use these. Behavior-neutral.
            pos.max_favorable_pct = max(pos.max_favorable_pct, pct_change)
            pos.max_adverse_pct = min(pos.max_adverse_pct, pct_change)
            time_held_hours = (time.time() * 1000 - pos.entry_time_ms) / 3600_000

            if time_held_hours >= self._max_hold_hours(pos):
                favors_hold = self._psar_favors_hold(pos)
                if favors_hold and time_held_hours < PSAR_HOLD_MAX_HOURS:
                    logger.info(f"⏳ HOLD_EXTEND {asset}: {time_held_hours:.1f}h, PSAR still favors "
                                f"{'long' if pos.is_long else 'short'} — holding (TP/SL still active)")
                    # fall through to SL/TP checks; do NOT continue
                else:
                    reason = "max_hold_ceiling" if favors_hold else "max_hold_psar_flip"
                    logger.info(f"⏰ MAX_HOLD {asset}: {time_held_hours:.1f}h close ({reason})")
                    # Remove from state ONLY on a confirmed close. If unconfirmed,
                    # keep it so the next cycle retries (Bug C: no phantom close).
                    if self._close_full_position(asset, reason, current_price):
                        positions_to_remove.append(asset)
                    continue

            if self._sl_triggered(pos, current_price):
                logger.warning(
                    f"🛑 SL TRIGGERED {asset}: ${current_price:.4f} vs SL ${pos.current_sl_price:.4f}"
                )
                if pos.sl_oid:
                    self._cancel_order(asset, pos.sl_oid)
                    pos.sl_oid = None
                reason = "sl" if pos.tp_hit_count == 0 else "be_stop"
                if pos.sl_mode in ("swing_low", "swing_high"):
                    reason = "structure_break"
                # Remove from state ONLY on a confirmed close. If unconfirmed,
                # keep it so the next cycle retries (Bug C: no phantom close).
                if self._close_full_position(asset, reason, current_price):
                    positions_to_remove.append(asset)
                continue

            # TP iterate. Format normalisasi: (pct, sell_frac, post_action)
            for i, level in enumerate(pos.tp_levels_remaining):
                if len(level) == 2:
                    tp_pct, sell_pct = level
                    post_action = None
                else:
                    tp_pct, sell_pct, post_action = level

                if pct_change >= tp_pct:
                    is_last_tp = (i == len(pos.tp_levels_remaining) - 1)
                    self._execute_partial_tp(asset, sell_pct, tp_pct, current_price, is_last_tp)
                    pos.tp_levels_remaining = pos.tp_levels_remaining[i+1:]

                    if post_action == "breakeven" or (
                        post_action is None and USE_BREAKEVEN_AFTER_TP1 and pos.tp_hit_count == 1
                    ):
                        self._update_stop_loss(pos, pos.entry_price)

                    self._save_state()

                    if not pos.tp_levels_remaining:
                        positions_to_remove.append(asset)
                    break

        for asset in positions_to_remove:
            self._cooldown_until[asset] = time.time() + COOLDOWN_AFTER_CLOSE_MINUTES * 60
            self.positions.pop(asset, None)

        if positions_to_remove:
            self._save_state()

    def _execute_partial_tp(self, asset: str, sell_pct: float, tp_label: float,
                             current_price: float, is_last_tp: bool = False):
        """sell_pct adalah fraksi dari CURRENT remaining_size_coin (bukan original size)."""
        pos = self.positions[asset]
        size_to_sell = self._round_size(asset, pos.remaining_size_coin * sell_pct)

        if size_to_sell <= 0:
            logger.warning(f"TP size rounded to 0 for {asset}, skipping")
            return

        next_count = pos.tp_hit_count + 1
        tp_label_str = f"tp{next_count}"

        if DRY_RUN:
            logger.info(
                f"[DRY_RUN] TP{next_count} sell {sell_pct*100:.0f}% of remaining "
                f"{asset} @ +{tp_label}%"
            )
            pos.tp_hit_count = next_count
            if is_last_tp:
                self._on_position_close_full(asset, current_price, tp_label_str)
            else:
                self._on_position_close_partial(asset, current_price, tp_label_str, size_to_sell)
            pos.remaining_size_coin -= size_to_sell
            return

        try:
            result = self.exchange.market_close(asset, sz=size_to_sell)
            if result.get("status") != "ok":
                logger.error(f"TP execution failed: {result}")
                return

            pos.tp_hit_count = next_count
            if is_last_tp:
                self._on_position_close_full(asset, current_price, tp_label_str)
            else:
                self._on_position_close_partial(asset, current_price, tp_label_str, size_to_sell)

            pos.remaining_size_coin -= size_to_sell
            logger.info(
                f"✅ TP_HIT {asset} +{tp_label}%: sold {size_to_sell}, "
                f"remaining {pos.remaining_size_coin:.4f}"
            )
        except Exception as e:
            logger.exception(f"TP execution failed: {e}")

    def _update_stop_loss(self, pos: Position, new_sl_price: float):
        if pos.sl_oid:
            self._cancel_order(pos.asset, pos.sl_oid)
            pos.sl_oid = None

        remaining_size = self._round_size(pos.asset, pos.remaining_size_coin)
        if remaining_size <= 0:
            logger.warning(f"No remaining size for {pos.asset}, skip SL update")
            return

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Update SL {pos.asset}: → ${new_sl_price:.4f}")
            pos.current_sl_price = new_sl_price
            return

        new_oid = self._place_stop_loss(pos.asset, remaining_size, new_sl_price, pos.is_long)
        pos.current_sl_price = new_sl_price
        pos.sl_oid = new_oid
        logger.info(f"📐 BE_STOP {pos.asset}: SL → ${new_sl_price:.4f} (size={remaining_size})")

    def _is_position_flat(self, asset: str) -> Optional[bool]:
        """Re-query the exchange and report whether `asset` has no open position.

        Returns:
          True  → asset absent or szi == 0 (definitively flat).
          False → asset still open (szi != 0) OR the re-query failed (we cannot
                  confirm closed, so conservatively treat as NOT flat → retry).
          None  → no info client available (e.g. a unit-test harness built via
                  object.__new__ without `self.info`). A live OrderManager ALWAYS
                  has self.info (set in __init__), so None never occurs in
                  production; callers fall back to the SDK status in that case.
        """
        info = getattr(self, "info", None)
        if info is None:
            return None
        try:
            user_state = info.user_state(HYPERLIQUID_ACCOUNT)
        except Exception as e:
            logger.warning(f"Flat-check query failed for {asset}: {e}")
            return False
        for p in (user_state or {}).get("assetPositions", []):
            position = p.get("position", {})
            if position.get("coin") == asset:
                try:
                    return float(position.get("szi", 0) or 0) == 0
                except (TypeError, ValueError):
                    return False
        return True  # not present in assetPositions → flat

    def _close_confirmed(self, asset: str, result) -> bool:
        """True iff a close attempt is confirmed. The exchange re-query
        (_is_position_flat) is the definitive signal — it catches partial/zero
        fills that still report status="ok". Only when no info client is present
        (None) do we fall back to the SDK status check, mirroring the
        _execute_partial_tp pattern (`result.get("status") == "ok"`)."""
        flat = self._is_position_flat(asset)
        if flat is None:
            return bool(result) and result.get("status") == "ok"
        return flat

    def _attempt_close_until_flat(self, asset: str) -> bool:
        """Market-close `asset`, retrying up to CLOSE_MAX_ATTEMPTS until the
        exchange confirms it flat. Returns True iff confirmed closed. Does NOT
        mutate self.positions or write the trade log — the caller decides what to
        do with the result (Bug C: never record a close that isn't confirmed)."""
        for attempt in range(1, self.CLOSE_MAX_ATTEMPTS + 1):
            result = None
            try:
                result = self.exchange.market_close(asset)
            except Exception as e:
                logger.warning(
                    f"market_close {asset} attempt {attempt}/{self.CLOSE_MAX_ATTEMPTS} "
                    f"raised: {e}"
                )
            if self._close_confirmed(asset, result):
                if attempt > 1:
                    logger.info(f"{asset} close confirmed on attempt {attempt}")
                return True
            status = result.get("status") if isinstance(result, dict) else None
            logger.warning(
                f"⚠️ Close NOT confirmed for {asset} "
                f"(attempt {attempt}/{self.CLOSE_MAX_ATTEMPTS}, status={status}) — "
                f"position may still be open on exchange"
            )
            if attempt < self.CLOSE_MAX_ATTEMPTS:
                time.sleep(self.CLOSE_RETRY_SLEEP_SEC)
        return False

    def _alert_close_failed(self, asset: str, reason: str, retained: bool):
        """Telegram alert when a close could not be confirmed after all retries."""
        tail = ("kept in state for retry" if retained
                else "position was NOT in managed state")
        logger.error(
            f"CLOSE FAILED {asset} after {self.CLOSE_MAX_ATTEMPTS} attempts — {tail} "
            f"(reason={reason})"
        )
        try:
            from notifications.telegram import notify_critical_error
            notify_critical_error(
                f"⚠️ CLOSE FAILED {asset} after {self.CLOSE_MAX_ATTEMPTS} attempts — "
                f"position may still be open on exchange, {tail}. reason={reason}",
                "close_failed",
            )
        except Exception as alert_err:
            logger.warning(f"Failed to send close-failed alert for {asset}: {alert_err}")

    def _alert_sl_placement_failed(self, asset: str, reason: str = None):
        """Telegram alert when a hard SL order could not be placed (Bug B).

        The entry is NOT rolled back — the soft SL in manage_open_positions() still
        protects the position — but we surface the gap (with the exchange's exact
        rejection reason when known) so it isn't silent."""
        detail = f": {reason}" if reason else ""
        try:
            from notifications.telegram import notify_critical_error
            notify_critical_error(
                f"⚠️ Hard SL placement failed for {asset}{detail} — relying on soft SL",
                "sl_placement_failed",
            )
        except Exception as alert_err:
            logger.warning(
                f"Failed to send SL-placement-failed alert for {asset}: {alert_err}"
            )

    def _close_full_position(self, asset: str, reason: str, current_price: float) -> bool:
        """Close the full remaining position. Returns True ONLY when the close is
        confirmed flat on the exchange — and ONLY then records it to the DB.

        On an unconfirmed close (error status, None return, zero/partial fill, or
        an exception) the position is KEPT in self.positions and an alert is sent,
        so the soft-SL/management loop retries it next cycle. This is the Bug C
        fix: previously the result was ignored and the trade was recorded closed +
        popped unconditionally, orphaning still-open positions (the LIT phantom)."""
        if DRY_RUN:
            logger.info(f"[DRY_RUN] Close {asset}: {reason}")
            self._on_position_close_full(asset, current_price, reason)
            return True

        if self._attempt_close_until_flat(asset):
            logger.info(f"✅ CLOSED {asset}: {reason}")
            self._on_position_close_full(asset, current_price, reason)
            return True

        self._alert_close_failed(asset, reason, retained=True)
        return False

    def _realized_fees_for(self, asset: str, since_ms: int) -> Optional[float]:
        """Sum the ACTUAL fees (USD) charged on `asset` since `since_ms`, read from
        the exchange fill history (user_fills). This is the whole round-trip cost
        for a position opened at entry_time_ms.

        Returns None when the query fails OR when there are no matching fills (e.g.
        DRY_RUN, where no real orders were sent) — the caller then falls back to a
        taker-rate estimate. Returning None rather than 0.0 on "no fills" is what
        makes the estimate path run in DRY_RUN. Purely informational — never raises,
        never affects a close."""
        try:
            fills = self.info.user_fills(HYPERLIQUID_ACCOUNT)
            matched = [
                f for f in (fills or [])
                if f.get("coin") == asset and int(f.get("time", 0)) >= since_ms
            ]
            if not matched:
                return None  # no real fills → caller uses the taker-rate estimate
            return sum(float(f["fee"]) for f in matched)
        except Exception as e:
            logger.warning(f"Realized-fee lookup failed for {asset}: {e}")
            return None

    def _on_position_close_partial(self, asset: str, exit_price: float,
                                    exit_reason: str, size_sold: float):
        from monitoring.trade_logger import log_trade
        from monitoring.tax_logger import log_taxable_event
        from execution.withdraw_manager import WithdrawManager

        pos = self.positions.get(asset)
        if not pos:
            return

        pnl_usd = self._compute_pnl_usd(pos, exit_price, size_sold)
        pnl_pct = self._compute_pnl_pct(pos, exit_price)
        size_usd = size_sold * exit_price

        # Fee: this partial's own taker fee (estimate). Accumulate on the Position
        # so the full-close row can subtract it and keep SUM(fees_usd) == round-trip
        # total. Fee math is best-effort and must never block the close.
        fees_usd = 0.0
        try:
            fees_usd = size_usd * TAKER_FEE_RATE
            pos.fees_partial_usd = (pos.fees_partial_usd or 0.0) + fees_usd
        except Exception as e:
            logger.warning(f"Partial fee computation failed for {asset}: {e}")
            fees_usd = 0.0

        try:
            logger.info(f"FEE {asset}: source=partial_estimate value=${fees_usd:.4f} "
                        f"size_sold={size_sold}")
        except Exception:
            pass

        log_trade(
            asset=asset,
            side=self._partial_close_side(pos),
            size_coin=size_sold,
            size_usd=size_usd,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time_ms=pos.entry_time_ms,
            exit_time_ms=int(time.time() * 1000),
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            fees_usd=fees_usd,
            exit_reason=exit_reason,
            notes=f"partial; size_sold={size_sold}",
            strategy_type=pos.strategy_type,
            leverage=pos.leverage,
            entry_swing_price=pos.entry_swing_price,
            indicators_snapshot=pos.indicators_snapshot,
            mfe_pct=pos.max_favorable_pct,
            mae_pct=pos.max_adverse_pct,
        )
        log_taxable_event(
            "trade_partial_close", asset, size_usd,
            pnl_usd=pnl_usd, notes=f"reason={exit_reason} sold={size_sold}",
        )
        if pnl_usd > 0:
            WithdrawManager().record_profit(pnl_usd)

    def _on_position_close_full(self, asset: str, exit_price: float, exit_reason: str):
        from monitoring.trade_logger import log_trade
        from monitoring.tax_logger import log_taxable_event
        from monitoring.health import HealthMonitor
        from execution.withdraw_manager import WithdrawManager

        pos = self.positions.get(asset)
        if not pos:
            return

        remaining = pos.remaining_size_coin
        pnl_usd = self._compute_pnl_usd(pos, exit_price, remaining)
        pnl_pct = self._compute_pnl_pct(pos, exit_price)
        size_usd = remaining * exit_price

        # Fee: prefer the ACTUAL round-trip fees from fills; fall back to an
        # entry+exit taker estimate when fills are unavailable (e.g. DRY_RUN).
        # Subtract fees already logged on this position's partial rows so the
        # per-position SUM(fees_usd) equals the round-trip total. Best-effort —
        # must never block the close.
        fees_usd = 0.0
        try:
            actual = self._realized_fees_for(asset, pos.entry_time_ms)
            if actual is not None:
                round_trip = actual                                # whole round-trip actual
            else:
                round_trip = pos.entry_size_usd * TAKER_FEE_RATE * 2  # entry+exit estimate
            fees_usd = max(0.0, round_trip - (pos.fees_partial_usd or 0.0))
        except Exception as e:
            logger.warning(f"Fee computation failed for {asset}: {e}")
            fees_usd = 0.0

        try:
            _src = "actual" if actual is not None else "estimate"
            logger.info(f"FEE {asset}: source={_src} value=${fees_usd:.4f} "
                        f"strat={pos.strategy_type} tp_hits={pos.tp_hit_count}")
        except Exception:
            pass

        log_trade(
            asset=asset,
            side=self._close_side(pos),
            size_coin=remaining,
            size_usd=size_usd,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time_ms=pos.entry_time_ms,
            exit_time_ms=int(time.time() * 1000),
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            fees_usd=fees_usd,
            exit_reason=exit_reason,
            strategy_type=pos.strategy_type,
            leverage=pos.leverage,
            entry_swing_price=pos.entry_swing_price,
            indicators_snapshot=pos.indicators_snapshot,
            mfe_pct=pos.max_favorable_pct,
            mae_pct=pos.max_adverse_pct,
        )
        log_taxable_event(
            "trade_close", asset, size_usd,
            pnl_usd=pnl_usd, notes=f"reason={exit_reason}",
        )
        HealthMonitor().on_trade_close(pnl_usd, INITIAL_CAPITAL_USD)
        if pnl_usd > 0:
            WithdrawManager().record_profit(pnl_usd)

    def get_open_positions_value_usd(self, current_prices: dict[str, float]) -> float:
        total = 0
        for asset, pos in self.positions.items():
            price = current_prices.get(asset, pos.entry_price)
            total += pos.remaining_size_coin * price
        return total
