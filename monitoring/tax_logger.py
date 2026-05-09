"""
Tax logging untuk lapor mandiri Indonesia.
"""
import csv
import time
import requests
from datetime import datetime, timezone
from typing import Optional
from loguru import logger
from config import DATA_DIR


TAX_LOG_PATH = DATA_DIR / "tax_log.csv"
USD_IDR_CACHE = {"rate": 16500.0, "fetched_at": 0}


def _fetch_usd_idr() -> float:
    now = time.time()
    if now - USD_IDR_CACHE["fetched_at"] < 3600:
        return USD_IDR_CACHE["rate"]

    # Bug #19: try multiple sources; sanity-check the result before caching
    sources = [
        ("https://api.exchangerate-api.com/v4/latest/USD", lambda j: j["rates"]["IDR"]),
        ("https://open.er-api.com/v6/latest/USD", lambda j: j["rates"]["IDR"]),
        ("https://api.frankfurter.app/latest?from=USD&to=IDR", lambda j: j["rates"]["IDR"]),
    ]
    for url, parser in sources:
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            rate = float(parser(r.json()))
            if 10000 < rate < 25000:  # sanity-check: plausible IDR/USD range
                USD_IDR_CACHE["rate"] = rate
                USD_IDR_CACHE["fetched_at"] = now
                return rate
        except Exception as e:
            logger.debug(f"USD/IDR source {url} failed: {e}")
            continue

    logger.warning(f"All USD/IDR sources failed, using cached rate {USD_IDR_CACHE['rate']}")
    return USD_IDR_CACHE["rate"]


def log_taxable_event(event_type: str, asset: str, amount_usd: float,
                      pnl_usd: Optional[float] = None, notes: str = ""):
    TAX_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_exists = TAX_LOG_PATH.exists()
    
    rate_idr = _fetch_usd_idr()
    now_utc = datetime.now(timezone.utc)
    now_jkt = now_utc.astimezone()
    
    amount_idr = amount_usd * rate_idr
    pnl_idr = (pnl_usd * rate_idr) if pnl_usd else None
    
    with open(TAX_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp_utc", "timestamp_jakarta", "event_type", "asset",
                "amount_usd", "pnl_usd", "rate_idr", "amount_idr", "pnl_idr", "notes",
            ])
        
        writer.writerow([
            now_utc.isoformat(),
            now_jkt.isoformat(),
            event_type, asset,
            f"{amount_usd:.4f}",
            f"{pnl_usd:.4f}" if pnl_usd is not None else "",
            f"{rate_idr:.0f}",
            f"{amount_idr:.0f}",
            f"{pnl_idr:.0f}" if pnl_idr else "",
            notes,
        ])
    
    logger.info(f"Tax log: {event_type} {asset} ${amount_usd:.2f}")
    