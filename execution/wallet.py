"""
WalletReader — unified view atas perp + spot wallet di Hyperliquid.

Hyperliquid SDK memisahkan:
- Info.user_state(addr)        → perp account (marginSummary.accountValue, assetPositions)
- Info.spot_user_state(addr)   → spot wallet (balances: list of {coin, total, hold, entryNtl})

Bot trading SEMUANYA terjadi di perp (spot strategy = 1x perp, deriv = ≤5x perp).
Maka USDC harus berada di PERP wallet supaya bisa dipakai sebagai margin.

Fungsi modul ini:
1. get_unified_balance() — gabungan view: perp_equity, spot_usdc, spot_tokens, total
2. auto_sweep_spot_to_perp() — pindahkan USDC dari spot ke perp via usd_class_transfer
   sehingga user tidak perlu manual transfer setelah deposit.

Cache 30s untuk hindari hammer API tiap heartbeat.
"""
import time
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

from config import (
    HYPERLIQUID_ACCOUNT, DRY_RUN,
    AUTO_SWEEP_SPOT_TO_PERP, MIN_SPOT_SWEEP_USD,
)


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
    """Unified balance view + auto-sweep dari spot ke perp."""

    CACHE_TTL_SECONDS = 30

    def __init__(self, info: Info, exchange: Optional[Exchange] = None,
                 account: str = None):
        self._info = info
        self._exchange = exchange
        self._account = account or HYPERLIQUID_ACCOUNT
        self._cache: Optional[WalletBalance] = None
        # usdClassTransfer is an EIP-712 user-signed action: Hyperliquid resolves
        # the user from signature recovery, ignoring Exchange.account_address.
        # If the Exchange signer is an API wallet (≠ main account), the transfer
        # will fail with "Must deposit before performing actions" — so we must
        # detect this and skip sweep entirely.
        self._is_api_wallet = False
        if exchange is not None and self._account:
            try:
                signer = getattr(exchange, "wallet", None)
                signer_addr = getattr(signer, "address", None)
                if signer_addr and signer_addr.lower() != self._account.lower():
                    self._is_api_wallet = True
            except Exception as e:
                logger.debug(f"Wallet: signer address check failed: {e}")

    # ---------------------------------------------------------------- read

    def get_unified_balance(self, force_refresh: bool = False) -> WalletBalance:
        now = time.time()
        if (not force_refresh
                and self._cache is not None
                and (now - self._cache.fetched_at) < self.CACHE_TTL_SECONDS):
            return self._cache

        bal = WalletBalance(fetched_at=now)

        # Perp side
        try:
            perp_state = self._info.user_state(self._account)
            ms = perp_state.get("marginSummary", {}) if perp_state else {}
            bal.perp_equity = float(ms.get("accountValue", 0) or 0)
            bal.perp_withdrawable = float(perp_state.get("withdrawable", 0) or 0)
        except Exception as e:
            logger.warning(f"Wallet: perp user_state fetch failed: {e}")

        # Spot side
        spot_balances = []
        try:
            spot_state = self._info.spot_user_state(self._account)
            spot_balances = (spot_state or {}).get("balances", []) or []
        except Exception as e:
            logger.warning(f"Wallet: spot_user_state fetch failed: {e}")

        # Spot prices via all_mids — Hyperliquid spot keys can be "@N" (index)
        # or "TOKEN/USDC". Token name → mid mapping via spot_meta.
        mids = {}
        spot_pair_map = {}  # token_name → mid_key in all_mids
        try:
            mids = self._info.all_mids() or {}
        except Exception as e:
            logger.debug(f"Wallet: all_mids fetch failed: {e}")

        try:
            sm = self._info.spot_meta() or {}
            for pair in sm.get("universe", []):
                # pair["tokens"] = [base_token_index, quote_token_index]; name like "@N" or "PURR/USDC"
                tokens = pair.get("tokens", [])
                if len(tokens) != 2:
                    continue
                base_idx = tokens[0]
                pair_name = pair.get("name", "")
                token_meta = sm.get("tokens", [])
                if base_idx < len(token_meta):
                    base_name = token_meta[base_idx].get("name", "")
                    if base_name:
                        spot_pair_map[base_name] = pair_name
        except Exception as e:
            logger.debug(f"Wallet: spot_meta fetch failed: {e}")

        for b in spot_balances:
            coin = b.get("coin", "")
            try:
                total = float(b.get("total", 0) or 0)
                hold = float(b.get("hold", 0) or 0)
            except (TypeError, ValueError):
                continue
            if total <= 0:
                continue
            if coin == "USDC":
                bal.spot_usdc += total
                continue
            # Non-USDC token: cari mark price dari spot mids
            pair_key = spot_pair_map.get(coin)
            mark_px = 0.0
            if pair_key and pair_key in mids:
                try:
                    mark_px = float(mids[pair_key])
                except (TypeError, ValueError):
                    mark_px = 0.0
            elif coin in mids:
                try:
                    mark_px = float(mids[coin])
                except (TypeError, ValueError):
                    mark_px = 0.0
            value_usd = total * mark_px
            bal.spot_tokens[coin] = {
                "total": total, "hold": hold,
                "mark_px": mark_px, "value_usd": value_usd,
            }
            bal.spot_tokens_value_usd += value_usd

        bal.total_equity = bal.perp_equity + bal.spot_usdc + bal.spot_tokens_value_usd

        self._cache = bal
        return bal

    # ---------------------------------------------------------------- sweep

    def auto_sweep_spot_to_perp(self, min_amount: float = None) -> Optional[float]:
        """
        Pindahkan USDC dari spot wallet ke perp wallet via usd_class_transfer.
        Return amount yang di-sweep (float), atau None kalau skip/gagal.
        """
        if not AUTO_SWEEP_SPOT_TO_PERP:
            return None
        if self._is_api_wallet:
            bal = self.get_unified_balance(force_refresh=True)
            if bal.spot_usdc >= (min_amount if min_amount is not None else MIN_SPOT_SWEEP_USD):
                logger.warning(
                    f"Wallet sweep SKIPPED: API-wallet mode detected "
                    f"(signer != HYPERLIQUID_ACCOUNT). usdClassTransfer requires "
                    f"main wallet's private key. ${bal.spot_usdc:.2f} USDC stuck in "
                    f"spot — please transfer Spot→Perp manually in the Hyperliquid UI."
                )
            return None
        threshold = min_amount if min_amount is not None else MIN_SPOT_SWEEP_USD

        bal = self.get_unified_balance(force_refresh=True)
        amount = bal.spot_usdc
        if amount < threshold:
            if amount > 0:
                logger.debug(
                    f"Wallet sweep: spot USDC ${amount:.4f} < threshold ${threshold:.2f}, skip"
                )
            return None

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would sweep ${amount:.2f} USDC spot → perp")
            return amount

        if self._exchange is None:
            logger.warning("Wallet sweep: no Exchange instance; cannot transfer")
            return None

        try:
            result = self._exchange.usd_class_transfer(amount, to_perp=True)
            if isinstance(result, dict) and result.get("status") == "ok":
                logger.info(f"💱 SWEEP: transferred ${amount:.2f} USDC spot → perp")
                # Invalidate cache karena balance berubah
                self._cache = None
                return amount
            logger.warning(f"Wallet sweep failed: {result}")
        except Exception as e:
            logger.warning(f"Wallet sweep exception: {e}")
        return None
