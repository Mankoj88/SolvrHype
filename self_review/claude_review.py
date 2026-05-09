"""
Weekly Claude review via Anthropic API.
TIDAK auto-deploy — kirim suggestions ke Telegram untuk manual approval.
"""
import os
import json
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger
import anthropic

from monitoring.trade_logger import get_recent_trades
from notifications.telegram import notify_review_ready, notify_critical_error


MODEL_NAME = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a skeptical quantitative analyst reviewing a retail crypto trading bot's weekly performance.

Your job:
1. Compute statistics ONLY from data given (do not assume or extrapolate)
2. Identify 3 observable patterns with specific evidence
3. Suggest 1 SPECIFIC parameter change (numeric range)
4. Flag 1 risk you observe in the log

CRITICAL constraints:
- If <30 trades in data, say "insufficient data, need 30+ more trades"
- Do NOT extrapolate to annualized returns from 1 week
- Do NOT suggest major strategy overhauls (only parameter tuning)
- If uncertain, say "uncertain" — do not guess
- Output MUST be valid JSON only, no preamble

Output format:
{
  "stats": {
    "total_trades": int,
    "win_rate": float,
    "avg_win_pct": float,
    "avg_loss_pct": float,
    "max_consecutive_losses": int,
    "asset_breakdown": {"BTC": {"trades": 5, "wr": 0.4}}
  },
  "patterns": [
    {"observation": "string", "evidence_trades": ["id1"], "confidence": "low|medium|high"}
  ],
  "suggested_change": {
    "parameter": "string",
    "from": current_value,
    "to": suggested_value,
    "reasoning": "data-driven explanation",
    "failure_condition": "when this change would fail"
  },
  "risk_alert": "string"
}
"""


def parse_review_response(text: str) -> str:
    """Strip markdown code fences, return raw JSON string."""
    text = text.strip()
    if "```" in text:
        text = text[text.index("```"):].split("\n", 1)[1]
        text = text.rsplit("```", 1)[0].strip()
    return text


def run_weekly_review() -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return {}
    
    trades = get_recent_trades(days_back=7)
    if not trades:
        logger.info("No trades in last 7 days, skip review")
        return {}
    
    compact_trades = [
        {
            "id": t["id"],
            "asset": t["asset"],
            "entry_t": t["entry_time_utc"],
            "exit_t": t["exit_time_utc"],
            "pnl_pct": t["pnl_pct"],
            "pnl_usd": t["pnl_usd"],
            "exit_reason": t["exit_reason"],
            "duration_s": t["hold_duration_seconds"],
        }
        for t in trades
    ]
    
    client = anthropic.Anthropic(api_key=api_key)
    user_message = f"""Trade log (last 7 days, {len(trades)} trades):

{json.dumps(compact_trades, indent=1)}

Analyze and respond with JSON per spec."""
    
    try:
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        text = response.content[0].text.strip()
        review = json.loads(parse_review_response(text))
        review = _sanity_check(review)
        
        review_dir = Path(__file__).parent.parent / "data" / "reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"review_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
        with open(review_path, "w") as f:
            json.dump(review, f, indent=2)
        
        summary = _format_summary(review)
        notify_review_ready(summary)
        
        logger.info(f"Weekly review complete: {review_path}")
        return review
        
    except json.JSONDecodeError as e:
        logger.exception(f"Failed to parse Claude response: {e}")
        notify_critical_error(f"Review parse failed: {e}", "review_parse")
        return {}
    except Exception as e:
        logger.exception(f"Review API call failed: {e}")
        notify_critical_error(f"Review failed: {e}", "review_api")
        return {}


def _sanity_check(review: dict) -> dict:
    """Filter out hallucinated suggestions on hard-limit params."""
    HARD_LIMIT_PARAMS = {
        "MAX_POSITION_SIZE_USD", "MAX_DAILY_LOSS_USD", "MAX_OPEN_POSITIONS",
        "LEVERAGE", "USE_ISOLATED_MARGIN", "EVALUATION_LOSS_THRESHOLD_USD",
        "CUTLOSS_PCT",
    }
    
    suggestion = review.get("suggested_change", {})
    if suggestion and suggestion.get("parameter") in HARD_LIMIT_PARAMS:
        logger.warning(f"Claude suggested hard-limit param change — IGNORE")
        review["suggested_change"] = {
            "parameter": suggestion["parameter"],
            "blocked": True,
            "reason": "Hard-limit parameter cannot be auto-tuned.",
        }
    
    forbidden_phrases = ["annualized return", "yearly extrapolat", "expected per year"]
    full_text = json.dumps(review).lower()
    for phrase in forbidden_phrases:
        if phrase in full_text:
            review["risk_alert"] = (review.get("risk_alert", "") + 
                                     " WARNING: Claude included annualized projections.")
            break
    
    return review


def _format_summary(review: dict) -> str:
    stats = review.get("stats", {})
    suggestion = review.get("suggested_change", {})
    
    summary = f"📊 Weekly Stats:\n"
    summary += f"• Trades: {stats.get('total_trades', 'N/A')}\n"
    summary += f"• Win Rate: {stats.get('win_rate', 0)*100:.1f}%\n"
    summary += f"• Avg Win/Loss: +{stats.get('avg_win_pct', 0):.2f}% / {stats.get('avg_loss_pct', 0):.2f}%\n\n"
    
    summary += "🔍 Patterns:\n"
    for i, p in enumerate(review.get("patterns", [])[:3], 1):
        summary += f"{i}. {p.get('observation', 'N/A')[:100]} ({p.get('confidence', '?')})\n"
    
    summary += "\n💡 Suggested Change:\n"
    if suggestion.get("blocked"):
        summary += f"⛔ {suggestion['parameter']}: BLOCKED\n"
    elif suggestion:
        summary += f"• {suggestion.get('parameter', 'N/A')}: {suggestion.get('from', '?')} → {suggestion.get('to', '?')}\n"
        summary += f"• {suggestion.get('reasoning', 'N/A')[:200]}\n"
    
    summary += f"\n⚠️ Risk: {review.get('risk_alert', 'None')[:200]}"
    return summary