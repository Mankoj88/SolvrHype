"""
3-month stop-loss commitment enforcer.
Halt bot kalau loss >$200 dalam 90 hari.
"""
import json
from datetime import datetime, timezone, timedelta
from loguru import logger

from notifications.telegram import notify_circuit_breaker
from monitoring.trade_logger import get_total_pnl_since
from config import (
    EVALUATION_PERIOD_DAYS, EVALUATION_LOSS_THRESHOLD_USD, DATA_DIR
)


STATE_FILE = DATA_DIR / "stop_loss_state.json"


class StopLossEnforcer:
    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.state = self._load()
    
    def _load(self) -> dict:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
        state = {
            "start_date": datetime.now(timezone.utc).isoformat(),
            "is_halted": False,
            "halt_reason": None,
            "halt_date": None,
        }
        self._save(state)
        return state
    
    def _save(self, state: dict = None):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state or self.state, f, indent=2)
    
    def check(self) -> tuple[bool, str]:
        if self.state["is_halted"]:
            return False, self.state["halt_reason"]
        
        start = datetime.fromisoformat(self.state["start_date"])
        now = datetime.now(timezone.utc)
        elapsed = now - start
        
        if elapsed < timedelta(days=EVALUATION_PERIOD_DAYS):
            return True, None
        
        cumulative_pnl = get_total_pnl_since(self.state["start_date"])
        
        if cumulative_pnl <= -EVALUATION_LOSS_THRESHOLD_USD:
            reason = (
                f"3-month evaluation: cumulative loss ${cumulative_pnl:.2f} "
                f"exceeded threshold -${EVALUATION_LOSS_THRESHOLD_USD}. "
                f"Period: {start.date()} to {now.date()}. "
                f"Manual review required."
            )
            self.state["is_halted"] = True
            self.state["halt_reason"] = reason
            self.state["halt_date"] = now.isoformat()
            self._save()
            
            notify_circuit_breaker(reason)
            logger.critical(reason)
            return False, reason
        
        return True, None
    
    def manual_resume(self, reset_period: bool = True):
        self.state["is_halted"] = False
        self.state["halt_reason"] = None
        self.state["halt_date"] = None
        if reset_period:
            self.state["start_date"] = datetime.now(timezone.utc).isoformat()
        self._save()
        logger.warning(f"Stop-loss enforcer manually resumed (reset_period={reset_period})")