"""
Telegram notifications dengan throttling per kategori.
Sesuai keputusan user: daily summary + critical errors only.
"""
import os
import time
import html as _html
import requests
from collections import defaultdict
from loguru import logger

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

THROTTLE_WINDOWS = {
    "general": 3600,
    "error_api": 1800,
    "error_network": 1800,
    "error_unknown": 600,
    "circuit_breaker": 0,
    "daily_summary": 0,
    "withdrawal": 0,
    "startup": 0,
    "shutdown": 0,
    "review": 0,
}

_last_sent: dict[str, float] = defaultdict(float)


def _esc(s: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return _html.escape(str(s), quote=False)


def _post(text: str, parse_mode: str = "HTML") -> bool:
    # Bug #15: use HTML instead of Markdown to avoid parse failures on special chars
    if not TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        logger.warning(f"Telegram failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram exception: {e}")
    return False


def send(text: str, category: str = "general", force: bool = False) -> bool:
    if not force:
        window = THROTTLE_WINDOWS.get(category, 3600)
        if window > 0:
            elapsed = time.time() - _last_sent[category]
            if elapsed < window:
                return False

    success = _post(text)
    if success:
        _last_sent[category] = time.time()
    return success


def notify_startup(version: str = "v4-phase-a"):
    msg = f"🟢 <b>Solvira started</b>\nVersion: <code>{_esc(version)}</code>"
    send(msg, "startup", force=True)


def notify_shutdown(reason: str = "normal"):
    send(f"🔴 <b>Solvira stopped</b>\nReason: {_esc(reason)}", "shutdown", force=True)


def notify_daily_summary(stats: dict):
    pnl_emoji = "📈" if stats["pnl_usd"] >= 0 else "📉"
    bal = stats.get("balance") or {}
    perp_eq = float(bal.get("perp_equity", stats.get("capital", 0)))
    spot_usdc = float(bal.get("spot_usdc", 0))
    spot_tokens_value = float(bal.get("spot_tokens_value_usd", 0))
    total_equity = float(bal.get("total_equity", perp_eq + spot_usdc + spot_tokens_value))
    pending_withdraw = float(
        stats.get("pending_withdraw", stats.get("usdt_wallet", 0))
    )

    msg = (
        f"📊 <b>Solvira Daily Summary</b>\n"
        f"Date: <code>{_esc(stats['date'])}</code>\n\n"
        f"Trades: {stats['total_trades']}\n"
        f"Wins/Losses: {stats['wins']}/{stats['losses']}\n"
        f"Win Rate: {stats['win_rate']:.1%}\n"
        f"{pnl_emoji} PnL: ${stats['pnl_usd']:+.2f} ({stats['pnl_pct']:+.2f}%)\n\n"
        f"💼 <b>Unified Wallet</b>\n"
        f"• Perp equity: ${perp_eq:.2f}\n"
        f"• Spot USDC: ${spot_usdc:.2f}\n"
    )

    spot_tokens = bal.get("spot_tokens") or {}
    if spot_tokens:
        msg += f"• Spot tokens (${spot_tokens_value:.2f}):\n"
        # Tampilkan max 5 token teratas by value
        sorted_tokens = sorted(
            spot_tokens.items(),
            key=lambda kv: float(kv[1].get("value_usd", 0)),
            reverse=True,
        )[:5]
        for coin, t in sorted_tokens:
            value = float(t.get("value_usd", 0))
            amount = float(t.get("total", 0))
            if value < 0.01 and amount < 1e-6:
                continue
            msg += (f"  · {_esc(coin)}: {amount:.4f} (${value:.2f})\n")

    msg += (
        f"💰 <b>Total equity: ${total_equity:.2f}</b>\n"
        f"💵 Pending withdraw: ${pending_withdraw:.2f}\n"
    )

    if stats.get("top_trade"):
        msg += f"\nTop: {_esc(stats['top_trade']['asset'])} {stats['top_trade']['pnl_pct']:+.1f}%"
    if stats.get("worst_trade"):
        msg += f"\nWorst: {_esc(stats['worst_trade']['asset'])} {stats['worst_trade']['pnl_pct']:+.1f}%"

    send(msg, "daily_summary", force=True)


def notify_critical_error(error_msg: str, error_type: str = "unknown"):
    truncated = _esc(error_msg[:500])
    msg = (
        f"🚨 <b>CRITICAL ERROR</b>\n"
        f"Type: <code>{_esc(error_type)}</code>\n"
        f"<pre>{truncated}</pre>"
    )
    send(msg, f"error_{error_type}")


def notify_circuit_breaker(reason: str):
    msg = f"⏸️ <b>BOT HALTED</b>\n{_esc(reason)}\n\nManual review required."
    send(msg, "circuit_breaker", force=True)


def notify_withdrawal(amount_usd: float, tx_hash: str, status: str = "complete"):
    short_hash = f"{tx_hash[:10]}...{tx_hash[-8:]}" if len(tx_hash) > 20 else tx_hash
    emoji = "💸" if status == "complete" else "⏳"
    msg = (
        f"{emoji} <b>Withdrawal {_esc(status)}</b>\n"
        f"${amount_usd:.2f} → USDT-Arbitrum\n"
        f"Tx: <code>{_esc(short_hash)}</code>"
    )
    send(msg, "withdrawal", force=True)


def send_alert(text: str, category: str = "general") -> bool:
    """Generic alert with HTML escaping — safe for any text content."""
    return send(f"⚠️ {_esc(text)}", category, force=True)


def notify_review_ready(suggestions_summary: str):
    msg = (
        f"📝 <b>Weekly Review Ready</b>\n\n"
        f"{_esc(suggestions_summary[:1500])}\n\n"
        f"⚠️ TIDAK auto-deploy. SSH ke VPS untuk review &amp; approve."
    )
    send(msg, "review", force=True)
