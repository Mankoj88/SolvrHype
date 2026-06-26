"""
Per-refresh market-context + regime snapshots → daily JSONL (offline backtest feed).

Pure and self-contained: NO imports from strategy/. Hooked once, after the cache is
set, in UniverseFetcher._refresh(). Best-effort and fully isolated — the entire body
is wrapped in try/except so a snapshot-write failure can NEVER raise into the universe
refresh / scan / entry path. Reuses the ctx already fetched by meta_and_asset_ctxs()
(no extra API calls).

One JSONL line per refresh cycle:
  {
    "ts": <int ms>, "iso": <utc iso>, "n_assets": <count>,
    "regime": {btc_px, btc_chg_24h_pct, eth_px, eth_chg_24h_pct,
               pct_assets_down, median_chg_24h_pct, pct_assets_down_gt5},
    "assets": {"<ASSET>": {markPx, prevDayPx, dayNtlVlm, openInterest, funding}, ...}
  }
"""
import json
import time
import statistics
from datetime import datetime, timezone

from loguru import logger

import config

# Per-asset ctx fields captured verbatim (coerced to float; missing/None skipped).
_FIELDS = ("markPx", "prevDayPx", "dayNtlVlm", "openInterest", "funding")


def _f(x):
    """Safe float coercion. Returns None for missing/None/uncoercible values."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _chg_24h_pct(ctx: dict):
    """24h change ≈ (markPx/prevDayPx - 1)*100, only when prevDayPx > 0."""
    mark = _f(ctx.get("markPx"))
    prev = _f(ctx.get("prevDayPx"))
    if mark is None or prev is None or prev <= 0:
        return None
    return (mark / prev - 1.0) * 100.0


def write_ctx_snapshot(assets_ctx: "list[tuple[str, dict]]") -> None:
    """Append one regime+ctx JSONL record for this refresh. Never raises.

    `assets_ctx` is the same list[(asset, ctx)] UniverseFetcher caches.
    """
    try:
        if not config.CAPTURE_CTX_SNAPSHOTS:
            return

        now_ms = int(time.time() * 1000)
        now = datetime.now(timezone.utc)

        assets: dict = {}
        changes: list = []
        btc_px = btc_chg = eth_px = eth_chg = None

        for name, ctx in assets_ctx:
            if not name or not isinstance(ctx, dict):
                continue
            rec = {}
            for field in _FIELDS:
                val = _f(ctx.get(field))
                if val is not None:
                    rec[field] = val
            assets[name] = rec

            chg = _chg_24h_pct(ctx)
            if chg is not None:
                changes.append(chg)
            if name == "BTC":
                btc_px, btc_chg = _f(ctx.get("markPx")), chg
            elif name == "ETH":
                eth_px, eth_chg = _f(ctx.get("markPx")), chg

        if changes:
            n = len(changes)
            pct_down = 100.0 * sum(1 for c in changes if c < 0) / n
            pct_down_gt5 = 100.0 * sum(1 for c in changes if c <= -5) / n
            median_chg = statistics.median(changes)
        else:
            pct_down = pct_down_gt5 = median_chg = None

        record = {
            "ts": now_ms,
            "iso": now.isoformat(),
            "n_assets": len(assets),
            "regime": {
                "btc_px": btc_px,
                "btc_chg_24h_pct": btc_chg,
                "eth_px": eth_px,
                "eth_chg_24h_pct": eth_chg,
                "pct_assets_down": pct_down,
                "median_chg_24h_pct": median_chg,
                "pct_assets_down_gt5": pct_down_gt5,
            },
            "assets": assets,
        }

        out_dir = config.CTX_SNAPSHOT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning(f"ctx snapshot write failed (non-fatal): {e}")
        return
