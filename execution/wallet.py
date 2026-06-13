"""
WalletReader — unified view balance Hyperliquid (Unified Account mode).

Akun Unified Account: spot USDC dipakai otomatis sebagai margin untuk perp.
marginSummary.accountValue HANYA mencerminkan collateral yang sedang terpakai
(= 0 ketika tidak ada open position), sehingga TIDAK bisa dipakai sebagai
sumber tunggal kebenaran untuk total tradeable capital.

Total equity yang benar di unified mode =
    perp_equity (collateral aktif di marginSummary)
  + spot USDC (margin yang masih bebas)

Fungsi modul ini:
1. get_unified_balance() — gabung perp accountValue + spot USDC jadi
   total_equity, lalu propagate ke field perp_equity supaya konsumen
   (get_total_capital di main.py) tetap dapat angka yang benar.
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

    def _log_diagnostic(self, perp_state: dict, spot_state: dict, bal: 'WalletBalance') -> None:
        """Dump raw API state + computed balance for equity reconciliation.
        Logged at INFO level with tag [EQUITY_DIAG] so it's grep-friendly."""
        try:
            ms = perp_state.get("marginSummary", {}) if perp_state else {}
            asset_positions = perp_state.get("assetPositions", []) if perp_state else []
            withdrawable = perp_state.get("withdrawable", 0) if perp_state else 0
            spot_balances = spot_state.get("balances", []) if spot_state else []

            # Per-position summary (no truncation)
            pos_summary = []
            for p in asset_positions:
                pos = p.get("position", {})
                pos_summary.append({
                    "coin": pos.get("coin"),
                    "szi": pos.get("szi"),
                    "entryPx": pos.get("entryPx"),
                    "positionValue": pos.get("positionValue"),
                    "unrealizedPnl": pos.get("unrealizedPnl"),
                    "marginUsed": pos.get("marginUsed"),
                })

            # All spot balances (USDC + tokens)
            spot_summary = []
            for b in spot_balances:
                total = float(b.get("total", 0) or 0)
                if total > 0:
                    spot_summary.append({
                        "coin": b.get("coin"),
                        "total": total,
                        "hold": float(b.get("hold", 0) or 0),
                        "entryNtl": b.get("entryNtl"),
                    })

            logger.info(
                f"[EQUITY_DIAG] perp_accountValue={ms.get('accountValue', 'N/A')} "
                f"perp_totalMarginUsed={ms.get('totalMarginUsed', 'N/A')} "
                f"perp_totalNtlPos={ms.get('totalNtlPos', 'N/A')} "
                f"perp_totalRawUsd={ms.get('totalRawUsd', 'N/A')} "
                f"perp_withdrawable={withdrawable} "
                f"open_positions={len(asset_positions)} "
                f"positions={pos_summary} "
                f"spot_balances={spot_summary} "
                f"computed_total_equity={bal.total_equity} "
                f"computed_spot_usdc={bal.spot_usdc}"
            )
        except Exception as e:
            logger.warning(f"[EQUITY_DIAG] failed: {e}")

    def get_unified_balance(self, force_refresh: bool = False) -> WalletBalance:
        now = time.time()
        if (not force_refresh
                and self._cache is not None
                and (now - self._cache.fetched_at) < self.CACHE_TTL_SECONDS):
            return self._cache

        bal = WalletBalance(fetched_at=now)

        # Unified account: accountValue = collateral aktif (0 kalau tidak ada
        # posisi). Spot USDC dipakai otomatis sebagai margin, jadi harus
        # ditambahkan untuk dapat total tradeable equity yang sebenarnya.
        perp_equity_raw = 0.0
        perp_state = None
        try:
            perp_state = self._info.user_state(self._account)
            ms = perp_state.get("marginSummary", {}) if perp_state else {}
            perp_equity_raw = float(ms.get("accountValue", 0) or 0)
            bal.perp_withdrawable = float(perp_state.get("withdrawable", 0) or 0)
        except Exception as e:
            logger.warning(f"Wallet: perp user_state fetch failed: {e}")

        spot_usdc = 0.0
        spot_state = None
        try:
            spot_state = self._info.spot_user_state(self._account)
            for b in (spot_state or {}).get("balances", []) or []:
                if b.get("coin") == "USDC":
                    spot_usdc += float(b.get("total", 0) or 0)
        except Exception as e:
            logger.warning(f"Wallet: spot_user_state fetch failed: {e}")

        bal.spot_usdc = spot_usdc
        bal.total_equity = perp_equity_raw + spot_usdc
        # Propagate ke perp_equity supaya get_total_capital() di main.py
        # (yang baca bal.perp_equity) dapat total equity unified, bukan 0.
        bal.perp_equity = bal.total_equity

        logger.info(
            f"Balance: perp={perp_equity_raw:.2f}, spot_usdc={spot_usdc:.2f}, "
            f"total={bal.total_equity:.2f}"
        )

        self._log_diagnostic(perp_state, spot_state, bal)
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
