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
    SPOT, DERIVATIVE,
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

    def _reconcile_with_exchange(self):
        """Two-way reconciliation: drop stale local, import exchange-only positions."""
        if DRY_RUN:
            return
        try:
            user_state = self.info.user_state(HYPERLIQUID_ACCOUNT)
            exchange_positions = {
                p["position"]["coin"]: float(p["position"]["szi"])
                for p in user_state.get("assetPositions", [])
                if float(p["position"]["szi"]) != 0
            }

            stale_assets = []
            for asset, local_pos in list(self.positions.items()):
                if asset not in exchange_positions:
                    logger.warning(
                        f"STALE STATE: {asset} in local but not on exchange. "
                        f"Likely closed while bot was offline."
                    )
                    stale_assets.append(asset)
                    continue

                exchange_size = abs(exchange_positions[asset])
                if abs(exchange_size - local_pos.remaining_size_coin) > 0.01 * local_pos.entry_size_coin:
                    logger.warning(
                        f"SIZE MISMATCH {asset}: local={local_pos.remaining_size_coin:.4f}, "
                        f"exchange={exchange_size:.4f}. Updating local."
                    )
                    local_pos.remaining_size_coin = exchange_size
                local_pos.sl_oid = None

            for asset in stale_assets:
                pos = self.positions[asset]
                try:
                    mids = self.info.all_mids()
                    exit_price = float(mids.get(asset, pos.entry_price))
                except Exception:
                    exit_price = pos.entry_price

                pnl_usd = self._compute_pnl_usd(pos, exit_price, pos.remaining_size_coin)
                try:
                    from monitoring.trade_logger import log_trade
                    from monitoring.tax_logger import log_taxable_event
                    log_trade(
                        asset=asset, side=self._close_side(pos),
                        size_coin=pos.remaining_size_coin,
                        size_usd=pos.remaining_size_coin * exit_price,
                        entry_price=pos.entry_price, exit_price=exit_price,
                        entry_time_ms=pos.entry_time_ms,
                        exit_time_ms=int(time.time() * 1000),
                        pnl_usd=pnl_usd,
                        pnl_pct=self._compute_pnl_pct(pos, exit_price),
                        exit_reason="reconcile_stale",
                        notes="Closed externally while bot was offline",
                        strategy_type=pos.strategy_type,
                        leverage=pos.leverage,
                        entry_swing_price=pos.entry_swing_price,
                    )
                    log_taxable_event(
                        "trade_close", asset, pos.remaining_size_coin * exit_price,
                        pnl_usd=pnl_usd, notes="reconcile_stale",
                    )
                except Exception as log_err:
                    logger.warning(f"Failed to log stale close for {asset}: {log_err}")

                self.positions.pop(asset, None)

            if stale_assets:
                self._save_state()
                try:
                    from notifications.telegram import notify_critical_error
                    notify_critical_error(
                        f"Cleaned {len(stale_assets)} stale positions on startup: {stale_assets}",
                        "stale_state",
                    )
                except Exception:
                    pass

            # Import exchange-only positions (default ke spot long, SL pct)
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
                    logger.warning(f"Skipping import of {asset}: missing entryPx")
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
                    f"IMPORTED {asset} from exchange: {size_coin} @ "
                    f"${entry_price:.4f}, will set SL=${sl_price:.4f}"
                )

            if imported_assets:
                try:
                    from notifications.telegram import notify_critical_error
                    notify_critical_error(
                        f"Imported {len(imported_assets)} exchange positions on "
                        f"startup: {imported_assets}",
                        "startup_import",
                    )
                except Exception:
                    pass

            for asset, pos in self.positions.items():
                if pos.remaining_size_coin > 0 and pos.current_sl_price > 0:
                    logger.info(f"Re-placing SL for {asset} after startup reconcile")
                    pos.sl_oid = self._place_stop_loss(
                        asset, pos.remaining_size_coin, pos.current_sl_price, pos.is_long
                    )
            self._save_state()

        except Exception as e:
            logger.exception(f"Reconcile failed: {e} — proceeding with local state (RISKY)")

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

    def _round_size(self, asset: str, size: float) -> float:
        if asset not in self._szDecimals_cache:
            meta = self.info.meta()
            for asset_info in meta["universe"]:
                if asset_info["name"] == asset:
                    self._szDecimals_cache[asset] = asset_info.get("szDecimals", 4)
                    break
            else:
                self._szDecimals_cache[asset] = 4
        return round(size, self._szDecimals_cache[asset])

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
                try:
                    self.exchange.market_close(signal.asset)
                except Exception as close_err:
                    logger.error(f"Slippage-rollback close failed: {close_err}")
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
            )

            sl_oid = self._place_stop_loss(signal.asset, actual_size, sl_price, signal.is_long)
            position.sl_oid = sl_oid

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

    def _place_stop_loss(self, asset: str, size: float, trigger_price: float,
                         is_long: bool = True) -> Optional[int]:
        """SL order: long → sell when price <= trigger; short → buy when price >= trigger."""
        try:
            result = self.exchange.order(
                asset,
                not is_long,  # opposite side
                size,
                trigger_price,
                {"trigger": {"isMarket": True, "triggerPx": str(trigger_price), "tpsl": "sl"}},
                reduce_only=True,
            )
            if result["status"] == "ok":
                statuses = result["response"]["data"]["statuses"]
                resting = next((s["resting"] for s in statuses if "resting" in s), None)
                if resting:
                    return int(resting["oid"])
        except Exception as e:
            logger.error(f"SL placement failed for {asset}: {e}")
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
            time_held_hours = (time.time() * 1000 - pos.entry_time_ms) / 3600_000

            if time_held_hours >= self._max_hold_hours(pos):
                logger.info(f"⏰ MAX_HOLD {asset}: {time_held_hours:.1f}h, force close")
                self._close_full_position(asset, "max_hold", current_price)
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
                self._close_full_position(asset, reason, current_price)
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

    def _close_full_position(self, asset: str, reason: str, current_price: float):
        if DRY_RUN:
            logger.info(f"[DRY_RUN] Close {asset}: {reason}")
            self._on_position_close_full(asset, current_price, reason)
            return
        try:
            self.exchange.market_close(asset)
            logger.info(f"✅ CLOSED {asset}: {reason}")
            self._on_position_close_full(asset, current_price, reason)
        except Exception as e:
            logger.exception(f"Close failed: {e}")

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
            exit_reason=exit_reason,
            notes=f"partial; size_sold={size_sold}",
            strategy_type=pos.strategy_type,
            leverage=pos.leverage,
            entry_swing_price=pos.entry_swing_price,
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
            exit_reason=exit_reason,
            strategy_type=pos.strategy_type,
            leverage=pos.leverage,
            entry_swing_price=pos.entry_swing_price,
        )
        log_taxable_event(
            "trade_close", asset, size_usd,
            pnl_usd=pnl_usd, notes=f"reason={exit_reason}",
        )
        HealthMonitor().on_trade_close(pnl_usd, size_usd)
        if pnl_usd > 0:
            WithdrawManager().record_profit(pnl_usd)

    def get_open_positions_value_usd(self, current_prices: dict[str, float]) -> float:
        total = 0
        for asset, pos in self.positions.items():
            price = current_prices.get(asset, pos.entry_price)
            total += pos.remaining_size_coin * price
        return total
