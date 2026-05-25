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
)


class AllocationManager:
    def __init__(self):
        # asset → (size_usd, strategy_type)
        self._reserved: dict[str, tuple[float, str]] = {}

    def reserve(self, asset: str, size_usd: float, strategy_type: str = "spot"):
        self._reserved[asset] = (size_usd, strategy_type)

    def release(self, asset: str):
        self._reserved.pop(asset, None)

    def pool_used(self, strategy_type: str) -> float:
        return sum(s for s, st in self._reserved.values() if st == strategy_type)

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
        if asset in self._reserved:
            return 0
        if strategy_type not in STRATEGY_POOL_SPLIT:
            logger.warning(f"Unknown strategy_type: {strategy_type}")
            return 0

        capacity = self.pool_capacity(strategy_type, total_capital)
        if capacity < MIN_POSITION_SIZE_USD:
            logger.debug(f"{strategy_type} pool capacity ${capacity:.2f} < min, skip {asset}")
            return 0

        if strategy_type == "spot":
            size_usd = self._spot_size(asset, total_capital)
        elif strategy_type == "derivative":
            size_usd = self._derivative_size(total_capital, signal)
        else:
            return 0

        if size_usd < MIN_POSITION_SIZE_USD:
            logger.debug(f"Size ${size_usd:.2f} < min, skip {asset}")
            return 0
        if size_usd > MAX_POSITION_SIZE_USD:
            size_usd = MAX_POSITION_SIZE_USD
        if size_usd > capacity:
            # Cap to remaining pool capacity (jangan reject seluruhnya)
            size_usd = max(MIN_POSITION_SIZE_USD, capacity)
            if size_usd < MIN_POSITION_SIZE_USD:
                return 0
        return size_usd

    # ---------------------------------------------------------------- spot

    def _spot_size(self, asset: str, total_capital: float) -> float:
        """Pool spot (50% × capital) didistribusi sesuai SPOT['allocation_split']."""
        existing = sum(1 for _, st in self._reserved.values() if st == "spot")
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
