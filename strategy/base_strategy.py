"""
Base abstractions untuk strategi trading di Solvira.
TradeSignal dipakai oleh semua strategy implementation.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeSignal:
    asset: str
    price: float
    timestamp_ms: int
    reason: str
    indicators_snapshot: dict
    strategy_type: str = "spot"                # "spot" | "derivative"
    leverage: int = 1
    is_long: bool = True
    suggested_sl_price: Optional[float] = None
    sl_mode: str = "pct"                        # "pct" | "swing_low" | "swing_high"


class BaseStrategy(ABC):
    """Interface yang dipakai orchestrator untuk menjalankan scan tiap cycle."""

    strategy_type: str = "base"

    @abstractmethod
    def scan(self) -> list[TradeSignal]:
        """Return list of actionable signals. Empty list jika tidak ada setup."""
        raise NotImplementedError
