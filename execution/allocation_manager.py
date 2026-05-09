"""
Allocation manager: distribusi modal antar posisi aktif.
"""
from loguru import logger
from config import get_allocation, MIN_POSITION_SIZE_USD, MAX_POSITION_SIZE_USD


class AllocationManager:
    def __init__(self):
        self._reserved_per_asset: dict[str, float] = {}
    
    def reserve(self, asset: str, size_usd: float):
        self._reserved_per_asset[asset] = size_usd
    
    def release(self, asset: str):
        self._reserved_per_asset.pop(asset, None)
    
    def calculate_position_size(
        self,
        asset: str,
        total_capital: float,
        existing_positions_count: int,
    ) -> float:
        """Return size USD, atau 0 kalau tidak ada slot/budget."""
        if asset in self._reserved_per_asset:
            return 0
        
        allocation = get_allocation(total_capital)
        max_positions = len(allocation)
        
        if existing_positions_count >= max_positions:
            return 0
        
        # Slot terbesar dipakai dulu
        sorted_alloc = sorted(allocation, reverse=True)
        next_slot_pct = sorted_alloc[existing_positions_count]
        
        size_usd = total_capital * next_slot_pct
        
        if size_usd < MIN_POSITION_SIZE_USD:
            logger.debug(f"Size ${size_usd:.2f} < min, skip {asset}")
            return 0
        if size_usd > MAX_POSITION_SIZE_USD:
            size_usd = MAX_POSITION_SIZE_USD
        
        return size_usd