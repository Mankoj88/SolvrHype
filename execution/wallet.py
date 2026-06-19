"""
WalletReader — unified view balance Hyperliquid (Unified Account mode).

Akun Unified Account: spot USDC dipakai otomatis sebagai margin untuk perp.
marginSummary.accountValue HANYA mencerminkan collateral yang sedang terpakai
(= 0 ketika tidak ada open position), sehingga TIDAK bisa dipakai sebagai
sumber tunggal kebenaran untuk total tradeable capital.

Total equity yang benar di unified mode =
    accountValue (perp equity = margin terkunci + uPnL)
  + (spot USDC total - spot USDC hold)   # HANYA spot USDC yang BEBAS

CATATAN double-count (Bug A): spot USDC `hold` = margin yang dipakai perp,
dan margin itu SUDAH terwakili di accountValue. Jadi `accountValue + spot_total`
menghitung margin DUA KALI (mis. 108.8 + 122.9 = 231 padahal real ~123).
Formula yang benar = perp equity + spot BEBAS saja (lihat get_unified_balance).
Asumsi: `hold` murni dari margin perp — valid karena bot ini perp-only (label
"spot" kosmetik, tak ada spot limit order yang bikin hold non-margin).

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
    spot_usdc: float = 0.0                  # USDC di spot wallet (total)
    spot_usdc_hold: float = 0.0             # USDC terkunci sbg margin perp (hold)
    spot_tokens: dict[str, dict] = field(default_factory=dict)
    # {coin: {"total": float, "hold": float, "value_usd": float, "mark_px": float}}
    spot_tokens_value_usd: float = 0.0      # sum value_usd
    total_equity: float = 0.0               # accountValue + (spot_usdc - hold)
    fetched_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "perp_equity": round(self.perp_equity, 4),
            "perp_withdrawable": round(self.perp_withdrawable, 4),
            "spot_usdc": round(self.spot_usdc, 4),
            "spot_usdc_hold": round(self.spot_usdc_hold, 4),
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

            # Old (buggy, double-counted) value vs new (fixed) value. Kept during
            # the transition so prod logs confirm the fix changed the number:
            #   equity_old_doublecount = accountValue + spot_total  (double-counts margin)
            #   equity_new             = accountValue + (spot_total - hold)
            try:
                acct_val = float(ms.get("accountValue", 0) or 0)
            except (ValueError, TypeError):
                acct_val = 0.0
            equity_old_doublecount = acct_val + bal.spot_usdc
            equity_new = bal.total_equity

            logger.info(
                f"[EQUITY_DIAG] perp_accountValue={ms.get('accountValue', 'N/A')} "
                f"perp_totalMarginUsed={ms.get('totalMarginUsed', 'N/A')} "
                f"perp_totalNtlPos={ms.get('totalNtlPos', 'N/A')} "
                f"perp_totalRawUsd={ms.get('totalRawUsd', 'N/A')} "
                f"perp_withdrawable={withdrawable} "
                f"open_positions={len(asset_positions)} "
                f"positions={pos_summary} "
                f"spot_balances={spot_summary} "
                f"spot_usdc_hold={bal.spot_usdc_hold} "
                f"equity_old_doublecount={equity_old_doublecount:.4f} "
                f"equity_new={equity_new:.4f} "
                f"computed_total_equity={bal.total_equity} "
                f"computed_spot_usdc={bal.spot_usdc}"
            )
        except Exception as e:
            logger.warning(f"[EQUITY_DIAG] failed: {e}")

    @staticmethod
    def _liq_metrics(pos: dict) -> dict:
        """Pure helper (no API) — liquidation metrics for ONE perp position.

        Returns {coin, entry, mark, liq, distance_pct}. `liq`/`distance_pct` are
        None when liquidationPx is absent/null so callers can render "n/a".
        mark is derived from positionValue/|szi| (the marked value), falling back
        to entryPx if positionValue is unavailable.
        distance_pct = (mark - liq) / mark * 100  (positive for a 1x long; the
        sign just reflects side — this is LOG-ONLY, no action is taken on it).
        """
        def _f(v):
            if v in (None, "", "null"):
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        coin = pos.get("coin")
        szi = _f(pos.get("szi")) or 0.0
        entry = _f(pos.get("entryPx"))
        pos_val = _f(pos.get("positionValue"))

        mark = None
        if pos_val is not None and szi != 0:
            mark = abs(pos_val) / abs(szi)
        if mark is None:
            mark = entry  # fallback when positionValue missing

        liq = _f(pos.get("liquidationPx"))
        distance_pct = None
        if liq is not None and mark not in (None, 0):
            distance_pct = (mark - liq) / mark * 100.0

        return {"coin": coin, "entry": entry, "mark": mark,
                "liq": liq, "distance_pct": distance_pct}

    def _log_liquidation(self, perp_state: dict) -> None:
        """Per-position liquidation-price logging (Bug A, Part 2). LOG ONLY at
        INFO — no alert, no auto-close, no behavior change. Graceful "n/a" when
        liquidationPx is null/absent; never raises into the balance cycle."""
        try:
            asset_positions = perp_state.get("assetPositions", []) if perp_state else []
            for p in asset_positions:
                pos = p.get("position", {}) or {}
                if not pos:
                    continue
                m = self._liq_metrics(pos)
                mark_s = f"{m['mark']:.6f}" if m["mark"] is not None else "n/a"
                entry_s = f"{m['entry']:.6f}" if m["entry"] is not None else "n/a"
                if m["liq"] is None:
                    logger.info(
                        f"[LIQ_DIAG] coin={m['coin']} entry={entry_s} mark={mark_s} "
                        f"liq=n/a distance=n/a"
                    )
                else:
                    logger.info(
                        f"[LIQ_DIAG] coin={m['coin']} entry={entry_s} mark={mark_s} "
                        f"liq={m['liq']:.6f} distance={m['distance_pct']:.2f}%"
                    )
        except Exception as e:
            logger.warning(f"[LIQ_DIAG] failed: {e}")

    def get_unified_balance(self, force_refresh: bool = False) -> WalletBalance:
        now = time.time()
        if (not force_refresh
                and self._cache is not None
                and (now - self._cache.fetched_at) < self.CACHE_TTL_SECONDS):
            return self._cache

        bal = WalletBalance(fetched_at=now)

        # Unified account: accountValue = collateral aktif + uPnL (0 kalau tidak
        # ada posisi). Spot USDC dipakai otomatis sebagai margin (porsi `hold`),
        # jadi hanya spot yang BEBAS (total - hold) yang boleh ditambahkan ke
        # accountValue — kalau total ditambahkan penuh, margin terhitung 2x.
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
        spot_usdc_hold = 0.0
        spot_state = None
        try:
            spot_state = self._info.spot_user_state(self._account)
            for b in (spot_state or {}).get("balances", []) or []:
                if b.get("coin") == "USDC":
                    spot_usdc += float(b.get("total", 0) or 0)
                    spot_usdc_hold += float(b.get("hold", 0) or 0)
        except Exception as e:
            logger.warning(f"Wallet: spot_user_state fetch failed: {e}")

        bal.spot_usdc = spot_usdc
        bal.spot_usdc_hold = spot_usdc_hold

        # Bug A fix — equity formula (c): accountValue + FREE spot USDC.
        #   accountValue (perp equity) already includes the locked margin + uPnL.
        #   spot_usdc total ALSO includes that same locked margin (as `hold`).
        #   Adding them double-counts the margin → inflated total_equity (231 vs
        #   real ~123). Subtract `hold` so only the FREE spot USDC is added.
        # At 0 positions: accountValue=0, hold=0 → total == spot_usdc (correct).
        free_spot_usdc = spot_usdc - spot_usdc_hold
        bal.total_equity = perp_equity_raw + free_spot_usdc
        # Propagate ke perp_equity supaya get_total_capital() di main.py
        # (yang baca bal.perp_equity) dapat total equity unified yang benar.
        bal.perp_equity = bal.total_equity

        # Transition diagnostic: prove in prod logs that the fix moved the number.
        equity_old_doublecount = perp_equity_raw + spot_usdc
        logger.info(
            f"Balance: perp={perp_equity_raw:.2f}, spot_usdc={spot_usdc:.2f}, "
            f"spot_hold={spot_usdc_hold:.2f}, free_spot={free_spot_usdc:.2f}, "
            f"equity_old_doublecount={equity_old_doublecount:.2f} "
            f"equity_new={bal.total_equity:.2f}"
        )

        self._log_diagnostic(perp_state, spot_state, bal)
        self._log_liquidation(perp_state)
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
