"""
WalletReader — unified view balance Hyperliquid (Unified Account mode).

Akun dalam mode Unified Account: spot+perp balance SUDAH MERGED — USDC adalah
satu balance yang cover spot + perps sekaligus, dan marginSummary.accountValue
sudah include semua USDC. usdClassTransfer (Spot→Perp) tidak diperlukan dan
tidak berlaku, jadi kita tidak fetch spot_user_state untuk add ke total.

Fungsi modul ini:
1. get_unified_balance() — return perp_equity = total_equity dari
   marginSummary.accountValue (sumber tunggal kebenaran di unified mode).
2. auto_sweep_spot_to_perp() — no-op di unified mode (silent return).

Cache 30s untuk hindari hammer API tiap heartbeat.
"""
import time
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

from config import HYPERLIQUID_ACCOUNT


@dataclass
class WalletBalance:
    perp_equity: float = 0.0                # marginSummary.accountValue
    perp_withdrawable: float = 0.0          # USDC bebas (bukan margin used)
    spot_usdc: float = 0.0                  # USDC di spot wallet
    spot_tokens: dict[str, dict] = field(default_factory=dict)
    # {coin: {"total": float, "hold": float, "value_usd": float, "mark_px": float}}
    spot_tokens_value_usd: float = 0.0      # sum value_usd
    total_equity: float = 0.0               # perp_equity + spot_usdc + spot_tokens_value
    fetched_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "perp_equity": round(self.perp_equity, 4),
            "perp_withdrawable": round(self.perp_withdrawable, 4),
            "spot_usdc": round(self.spot_usdc, 4),
            "spot_tokens": {
                k: {"total": round(v["total"], 6),
                    "value_usd": round(v["value_usd"], 4)}
                for k, v in self.spot_tokens.items()
            },
            "spot_tokens_value_usd": round(self.spot_tokens_value_usd, 4),
            "total_equity": round(self.total_equity, 4),
            "fetched_at": self.fetched_at,
        }


class WalletReader:
    """Unified balance view dari marginSummary.accountValue (unified account mode)."""

    CACHE_TTL_SECONDS = 30

    def __init__(self, info: Info, exchange: Optional[Exchange] = None,
                 account: str = None):
        self._info = info
        self._exchange = exchange
        self._account = account or HYPERLIQUID_ACCOUNT
        self._cache: Optional[WalletBalance] = None

    # ---------------------------------------------------------------- read

    def get_unified_balance(self, force_refresh: bool = False) -> WalletBalance:
        now = time.time()
        if (not force_refresh
                and self._cache is not None
                and (now - self._cache.fetched_at) < self.CACHE_TTL_SECONDS):
            return self._cache

        bal = WalletBalance(fetched_at=now)

        # Unified account: spot+perp balance sudah merged. marginSummary.accountValue
        # adalah sumber tunggal kebenaran untuk total equity — TIDAK fetch
        # spot_user_state untuk add ke total (USDC spot sudah include di sini).
        try:
            perp_state = self._info.user_state(self._account)
            ms = perp_state.get("marginSummary", {}) if perp_state else {}
            bal.perp_equity = float(ms.get("accountValue", 0) or 0)
            bal.perp_withdrawable = float(perp_state.get("withdrawable", 0) or 0)
        except Exception as e:
            logger.warning(f"Wallet: perp user_state fetch failed: {e}")

        bal.total_equity = bal.perp_equity

        self._cache = bal
        return bal

    # ---------------------------------------------------------------- sweep

    def auto_sweep_spot_to_perp(self, min_amount: float = None) -> Optional[float]:
        """
        Unified account: spot+perp balance sudah merged, jadi sweep no-op.
        usdClassTransfer tidak diperlukan dan tidak berlaku di mode ini.
        Dipertahankan untuk kompat dengan caller di main.py.
        """
        return None
