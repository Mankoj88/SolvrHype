"""
OrderManager: handle entry, partial TP, stop loss, max-hold dengan persistent state.
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
)
from strategy.scanner import TradeSignal


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
    remaining_size_coin: float = 0.0  # Bug #2: tracks remaining size after partial TPs
    last_signal_time: Optional[str] = None  # Bug #18: persists anti-spam filter across restarts

    def __post_init__(self):
        # Backwards-compatible: init remaining = entry if not explicitly set (e.g. from old state)
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
        self._cooldown_until: dict[str, float] = {}  # Bug #8: per-asset cooldown

        # Bug #5: reconcile local state vs exchange on every startup
        self._reconcile_with_exchange()

    def _load_state(self) -> dict[str, Position]:
        if not self.STATE_FILE.exists():
            return {}
        try:
            with open(self.STATE_FILE) as f:
                data = json.load(f)

            # Bug #14: tolerant loader — filter to valid fields so new fields don't crash old state
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
        """Bug #5: drop stale local positions that were closed on exchange while bot was offline."""
        if DRY_RUN or not self.positions:
            return
        try:
            user_state = self.info.user_state(HYPERLIQUID_ACCOUNT)
            exchange_positions = {
                p["position"]["coin"]: abs(float(p["position"]["szi"]))
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

                exchange_size = exchange_positions[asset]
                if abs(exchange_size - local_pos.remaining_size_coin) > 0.01 * local_pos.entry_size_coin:
                    logger.warning(
                        f"SIZE MISMATCH {asset}: local={local_pos.remaining_size_coin:.4f}, "
                        f"exchange={exchange_size:.4f}. Updating local."
                    )
                    local_pos.remaining_size_coin = exchange_size
                # Reset SL oid — old order may have been filled or cancelled
                local_pos.sl_oid = None

            for asset in stale_assets:
                pos = self.positions[asset]
                try:
                    mids = self.info.all_mids()
                    exit_price = float(mids.get(asset, pos.entry_price))
                except Exception:
                    exit_price = pos.entry_price

                pnl_usd = (exit_price - pos.entry_price) * pos.remaining_size_coin
                try:
                    from monitoring.trade_logger import log_trade
                    from monitoring.tax_logger import log_taxable_event
                    log_trade(
                        asset=asset, side="long_close",
                        size_coin=pos.remaining_size_coin,
                        size_usd=pos.remaining_size_coin * exit_price,
                        entry_price=pos.entry_price, exit_price=exit_price,
                        entry_time_ms=pos.entry_time_ms,
                        exit_time_ms=int(time.time() * 1000),
                        pnl_usd=pnl_usd,
                        pnl_pct=(exit_price / pos.entry_price - 1) * 100,
                        exit_reason="reconcile_stale",
                        notes="Closed externally while bot was offline",
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

            # Re-place SL orders for still-live positions (old oids are now invalid)
            for asset, pos in self.positions.items():
                if pos.remaining_size_coin > 0 and pos.current_sl_price > 0:
                    logger.info(f"Re-placing SL for {asset} after startup reconcile")
                    pos.sl_oid = self._place_stop_loss(
                        asset, pos.remaining_size_coin, pos.current_sl_price
                    )
            self._save_state()

        except Exception as e:
            logger.exception(f"Reconcile failed: {e} — proceeding with local state (RISKY)")

    def open_position_count(self) -> int:
        return len(self.positions)

    def setup_leverage_for_asset(self, asset: str):
        try:
            self.exchange.update_leverage(LEVERAGE, asset, is_cross=not USE_ISOLATED_MARGIN)
            logger.info(f"Leverage set: {asset} @ {LEVERAGE}x isolated")
        except Exception as e:
            logger.warning(f"Failed to set leverage for {asset}: {e}")

    def _round_size(self, asset: str, size: float) -> float:
        """Round size based on asset-specific szDecimals."""
        if asset not in self._szDecimals_cache:
            meta = self.info.meta()
            for asset_info in meta["universe"]:
                if asset_info["name"] == asset:
                    self._szDecimals_cache[asset] = asset_info.get("szDecimals", 4)
                    break
            else:
                self._szDecimals_cache[asset] = 4
        return round(size, self._szDecimals_cache[asset])

    def execute_entry(self, signal: TradeSignal, size_usd: float) -> bool:
        """Execute market buy + place stop loss order."""
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

        # Bug #8: check per-asset cooldown after recent close
        cooldown_ts = self._cooldown_until.get(signal.asset, 0)
        if time.time() < cooldown_ts:
            remaining_min = (cooldown_ts - time.time()) / 60
            logger.debug(f"{signal.asset} in cooldown ({remaining_min:.1f}m remaining), skip")
            return False

        self.setup_leverage_for_asset(signal.asset)

        size_coin = self._round_size(signal.asset, size_usd / signal.price)

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would buy {signal.asset}: {size_coin} @ ~{signal.price}")
            self.positions[signal.asset] = Position(
                asset=signal.asset,
                entry_price=signal.price,
                entry_size_coin=size_coin,
                remaining_size_coin=size_coin,  # Bug #2
                entry_size_usd=size_coin * signal.price,
                entry_time_ms=int(time.time() * 1000),
                tp_levels_remaining=list(TAKE_PROFITS),
                initial_sl_price=signal.price * (1 + CUTLOSS_PCT / 100),
                current_sl_price=signal.price * (1 + CUTLOSS_PCT / 100),
            )
            self._save_state()
            return True

        try:
            result = self.exchange.market_open(
                signal.asset, True, size_coin, None, SLIPPAGE_TOLERANCE
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

            sl_price = actual_price * (1 + CUTLOSS_PCT / 100)
            position = Position(
                asset=signal.asset,
                entry_price=actual_price,
                entry_size_coin=actual_size,
                remaining_size_coin=actual_size,  # Bug #2
                entry_size_usd=actual_size * actual_price,
                entry_time_ms=int(time.time() * 1000),
                tp_levels_remaining=list(TAKE_PROFITS),
                initial_sl_price=sl_price,
                current_sl_price=sl_price,
            )

            sl_oid = self._place_stop_loss(signal.asset, actual_size, sl_price)
            position.sl_oid = sl_oid

            self.positions[signal.asset] = position
            self._save_state()

            logger.info(f"✅ ENTERED {signal.asset}: {actual_size} @ ${actual_price:.4f}")
            return True

        except Exception as e:
            logger.exception(f"Entry exception for {signal.asset}: {e}")
            return False

    def _place_stop_loss(self, asset: str, size: float, trigger_price: float) -> Optional[int]:
        try:
            result = self.exchange.order(
                asset, False, size, trigger_price,
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

    def manage_open_positions(self, current_prices: dict[str, float]):
        """Check each position untuk TP/SL/max-hold."""
        positions_to_remove = []

        for asset, pos in list(self.positions.items()):
            if asset not in current_prices:
                continue

            current_price = current_prices[asset]
            pct_change = (current_price - pos.entry_price) / pos.entry_price * 100
            time_held_hours = (time.time() * 1000 - pos.entry_time_ms) / 3600_000

            # Max hold check
            if time_held_hours >= MAX_HOLD_HOURS:
                logger.info(f"⏰ MAX_HOLD {asset}: {time_held_hours:.1f}h, force close")
                self._close_full_position(asset, "max_hold", current_price)
                positions_to_remove.append(asset)
                continue

            # Bug #3: SL check — cancel resting order first, then force-close on exchange
            if current_price <= pos.current_sl_price:
                logger.warning(
                    f"🛑 SL TRIGGERED {asset}: ${current_price:.4f} <= SL ${pos.current_sl_price:.4f}"
                )
                if pos.sl_oid:
                    self._cancel_order(asset, pos.sl_oid)
                    pos.sl_oid = None
                reason = "sl" if pos.tp_hit_count == 0 else "be_stop"
                self._close_full_position(asset, reason, current_price)
                positions_to_remove.append(asset)
                continue

            # TP check
            for i, (tp_pct, sell_pct) in enumerate(pos.tp_levels_remaining):
                if pct_change >= tp_pct:
                    is_last_tp = (i == len(pos.tp_levels_remaining) - 1)
                    self._execute_partial_tp(asset, sell_pct, tp_pct, current_price, is_last_tp)
                    pos.tp_levels_remaining = pos.tp_levels_remaining[i+1:]

                    if USE_BREAKEVEN_AFTER_TP1 and pos.tp_hit_count == 1:
                        self._update_stop_loss(pos, pos.entry_price)

                    self._save_state()

                    if not pos.tp_levels_remaining:
                        positions_to_remove.append(asset)
                    break

        for asset in positions_to_remove:
            # Bug #8: set cooldown after any position close
            self._cooldown_until[asset] = time.time() + COOLDOWN_AFTER_CLOSE_MINUTES * 60
            self.positions.pop(asset, None)

        if positions_to_remove:
            self._save_state()

    def _execute_partial_tp(self, asset: str, sell_pct: float, tp_label: float,
                             current_price: float, is_last_tp: bool = False):
        """Bug #2: sell_pct is fraction of CURRENT remaining position (not original size)."""
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
                return  # tp_hit_count NOT incremented — fill unconfirmed

            pos.tp_hit_count = next_count  # only after confirmed fill
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
        """Bug #7: use remaining_size_coin (not hardcoded * 0.40) for new SL size."""
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

        new_oid = self._place_stop_loss(pos.asset, remaining_size, new_sl_price)
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
        """Bug #6: log only the actual sold size; no health circuit breaker on partials."""
        from monitoring.trade_logger import log_trade
        from monitoring.tax_logger import log_taxable_event
        from execution.withdraw_manager import WithdrawManager

        pos = self.positions.get(asset)
        if not pos:
            return

        pnl_usd = (exit_price - pos.entry_price) * size_sold
        pnl_pct = (exit_price / pos.entry_price - 1) * 100
        size_usd = size_sold * exit_price

        log_trade(
            asset=asset,
            side="long_partial_close",
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
        )
        log_taxable_event(
            "trade_partial_close", asset, size_usd,
            pnl_usd=pnl_usd, notes=f"reason={exit_reason} sold={size_sold}",
        )
        if pnl_usd > 0:
            WithdrawManager().record_profit(pnl_usd)

    def _on_position_close_full(self, asset: str, exit_price: float, exit_reason: str):
        """Bug #6: log remaining size at final close; triggers health circuit breaker once."""
        from monitoring.trade_logger import log_trade
        from monitoring.tax_logger import log_taxable_event
        from monitoring.health import HealthMonitor
        from execution.withdraw_manager import WithdrawManager

        pos = self.positions.get(asset)
        if not pos:
            return

        remaining = pos.remaining_size_coin
        pnl_usd = (exit_price - pos.entry_price) * remaining
        pnl_pct = (exit_price / pos.entry_price - 1) * 100
        size_usd = remaining * exit_price

        log_trade(
            asset=asset,
            side="long_close",
            size_coin=remaining,
            size_usd=size_usd,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time_ms=pos.entry_time_ms,
            exit_time_ms=int(time.time() * 1000),
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
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
            total += pos.remaining_size_coin * price  # Bug #2: use remaining, not entry size
        return total
