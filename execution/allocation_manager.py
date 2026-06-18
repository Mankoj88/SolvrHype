"""
Allocation manager: distribusi modal antar 2 pool strategi (spot & derivative).

Spot: alokasi tier-based dari pool spot (30/30/40 untuk 3 slot).
Derivative: risk-based sizing — 1.5% dari TOTAL equity per trade, dibagi
            sl_distance_pct lalu dibagi leverage = margin (size_usd).
"""
from typing import Optional
from loguru import logger
from config import (
    MIN_POSITION_SIZE_USD, MAX_POSITION_SIZE_USD,
    STRATEGY_POOL_SPLIT, SPOT, DERIVATIVE,
    SPOT_MIN_POSITION_FLOOR, HL_PLATFORM_MIN_ORDER_USD,
)


class AllocationManager:
    def __init__(
        self,
        info: Optional[object] = None,
        order_manager: Optional[object] = None,
    ):
        # Live source of truth for open positions. Pool usage is DERIVED from
        # order_manager.positions every cycle (see pool_used) — there is no
        # separately-maintained reservation dict to drift/leak. When a position
        # closes it disappears from order_manager.positions and its pool usage
        # vanishes automatically.
        self._order_manager = order_manager
        # Optional HL Info client for runtime exchange-min lookup. When absent
        # (e.g. unit tests) the spot min resolves to SPOT_MIN_POSITION_FLOOR.
        self._info = info
        self._spot_min_cache: dict[str, float] = {}

    def set_order_manager(self, order_manager: object) -> None:
        """Late-bind the order manager if it wasn't available at construction."""
        self._order_manager = order_manager

    def _open_positions(self) -> dict:
        """Live open positions keyed by asset, or {} if unwired (e.g. tests)."""
        if self._order_manager is None:
            return {}
        return self._order_manager.positions

    def _spot_min_notional(self, asset: str) -> float:
        """Effective spot minimum order size for `asset` in USD.

        Returns max(exchange_min_notional, SPOT_MIN_POSITION_FLOOR). The
        exchange min is fetched from the Hyperliquid spot meta at runtime; HL
        exposes no per-asset min-notional field, so it resolves to the verified
        platform-wide minimum order value. On lookup failure, falls back to the
        floor and logs a warning.
        """
        cached = self._spot_min_cache.get(asset)
        if cached is not None:
            return cached

        exch_min = SPOT_MIN_POSITION_FLOOR
        if self._info is not None:
            try:
                # Touch spot meta to confirm the asset is tradeable / meta is
                # reachable. HL spot meta has no per-asset min-notional field;
                # the platform-wide minimum order value applies.
                self._info.spot_meta()
                exch_min = HL_PLATFORM_MIN_ORDER_USD
            except Exception as e:
                logger.warning(
                    f"Spot meta min-notional lookup failed for {asset}: {e}; "
                    f"falling back to floor ${SPOT_MIN_POSITION_FLOOR}"
                )
                exch_min = SPOT_MIN_POSITION_FLOOR

        result = max(exch_min, SPOT_MIN_POSITION_FLOOR)
        self._spot_min_cache[asset] = result
        return result

    def pool_used(self, strategy_type: str) -> float:
        """USD pool usage for `strategy_type`, DERIVED from live open positions.

        For each open position of this strategy we count its REMAINING
        entry-time margin:

            margin = remaining_size_coin × entry_price / leverage

        Rationale:
          • Entry-time price (not live mark) keeps capacity stable within a
            cycle — capacity must not fluctuate with market moves.
          • remaining_size_coin (not entry_size_coin) means a partial TP frees
            its share of the pool naturally as the position shrinks.
          • Dividing by leverage recovers the MARGIN committed. Position stores
            entry_size_usd = entry_size_coin × entry_price = leveraged notional;
            the pool reserves margin (spot: leverage=1 → notional == margin;
            derivative: notional / leverage == margin), matching the size_usd
            originally produced by calculate_position_size.

        Because usage is recomputed from order_manager.positions every call, a
        closed position contributes ZERO — leaks are structurally impossible.
        """
        total = 0.0
        for pos in self._open_positions().values():
            if getattr(pos, "strategy_type", "spot") != strategy_type:
                continue
            lev = getattr(pos, "leverage", 1) or 1
            total += pos.remaining_size_coin * pos.entry_price / lev
        return total

    def pool_capacity(self, strategy_type: str, total_capital: float) -> float:
        pool_total = total_capital * STRATEGY_POOL_SPLIT.get(strategy_type, 0)
        return pool_total - self.pool_used(strategy_type)

    def calculate_position_size(
        self,
        asset: str,
        total_capital: float,
        strategy_type: str = "spot",
        signal: Optional[object] = None,
    ) -> float:
        """
        Return size USD (margin), atau 0 kalau tidak ada slot/budget.
        Untuk derivative, butuh `signal` dengan suggested_sl_price untuk risk-based sizing.
        """
        # Anti-duplicate: never size a second entry for an asset already held.
        # Derived from the live position book (matches spot_strategy's gate).
        if asset in self._open_positions():
            return 0
        if strategy_type not in STRATEGY_POOL_SPLIT:
            logger.warning(f"Unknown strategy_type: {strategy_type}")
            return 0

        # Per-strategy minimum: spot uses the exchange-min-aware floor so small
        # pools can still trade; derivative keeps the global hard minimum.
        min_size = (
            self._spot_min_notional(asset) if strategy_type == "spot"
            else MIN_POSITION_SIZE_USD
        )

        capacity = self.pool_capacity(strategy_type, total_capital)
        if capacity < min_size:
            logger.debug(f"{strategy_type} pool capacity ${capacity:.2f} < min, skip {asset}")
            return 0

        if strategy_type == "spot":
            size_usd = self._spot_size(asset, total_capital)
            if size_usd <= 0:
                # No slot available (pool exhausted / all slots filled).
                return 0
            if size_usd < min_size:
                # Tier allocation fell below the exchange minimum; floor up to
                # the min so the order is tradeable (capacity already >= min).
                size_usd = min_size
        elif strategy_type == "derivative":
            size_usd = self._derivative_size(total_capital, signal)
            if size_usd < min_size:
                logger.debug(f"Size ${size_usd:.2f} < min, skip {asset}")
                return 0
        else:
            return 0

        if size_usd > MAX_POSITION_SIZE_USD:
            size_usd = MAX_POSITION_SIZE_USD
        if size_usd > capacity:
            # Cap to remaining pool capacity (jangan reject seluruhnya)
            size_usd = max(min_size, capacity)
            if size_usd < min_size:
                return 0
        return size_usd

    # ---------------------------------------------------------------- spot

    def _spot_size(self, asset: str, total_capital: float) -> float:
        """Pool spot (50% × capital) didistribusi sesuai SPOT['allocation_split']."""
        existing = sum(
            1 for pos in self._open_positions().values()
            if getattr(pos, "strategy_type", "spot") == "spot"
        )
        allocation = sorted(SPOT["allocation_split"], reverse=True)
        if existing >= len(allocation):
            return 0
        slot_pct = allocation[existing]
        spot_pool = total_capital * STRATEGY_POOL_SPLIT["spot"]
        return spot_pool * slot_pct

    # ---------------------------------------------------------------- derivative

    def _derivative_size(self, total_capital: float, signal) -> float:
        """
        Risk-based sizing dari TOTAL equity:
            risk_usd = 1.5% × total_capital
            sl_distance = |entry - suggested_sl| / entry
            notional = risk_usd / sl_distance
            margin = notional / leverage
        """
        if signal is None or signal.suggested_sl_price is None:
            logger.warning("Derivative sizing requires signal.suggested_sl_price")
            return 0
        if signal.price <= 0 or signal.leverage <= 0:
            return 0
        sl_distance = abs(signal.price - signal.suggested_sl_price) / signal.price
        if sl_distance <= 0:
            return 0
        risk_usd = total_capital * (DERIVATIVE["risk_per_trade_pct"] / 100)
        notional = risk_usd / sl_distance
        margin = notional / signal.leverage
        return margin
