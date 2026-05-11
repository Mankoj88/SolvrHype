"""
Health monitoring & circuit breakers + HTTP endpoint untuk UptimeRobot.
"""
import time
import json
from datetime import datetime, timezone
from threading import Thread, Lock
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from loguru import logger

from notifications.telegram import notify_circuit_breaker
from config import (
    MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS_USD, MAX_DAILY_LOSS_PCT
)


class HealthMonitor:
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
        return cls._instance
    
    def _init(self):
        self.start_time = time.time()
        self.last_loop_time = None
        self.consecutive_errors = 0
        self.consecutive_losses = 0
        self.daily_pnl_usd = 0.0
        self.daily_pnl_date = datetime.now(timezone.utc).date()
        self.is_halted = False
        self.halt_reason = None
        self.open_positions_count = 0
        self.last_signal_time = None
    
    def heartbeat(self):
        self.last_loop_time = time.time()
        today = datetime.now(timezone.utc).date()
        if today != self.daily_pnl_date:
            self.daily_pnl_usd = 0.0
            self.daily_pnl_date = today
            self.consecutive_losses = 0
            logger.info("Daily counters reset")
    
    def on_success(self):
        self.consecutive_errors = 0
    
    def on_error(self, error_type: str = "unknown"):
        self.consecutive_errors += 1
        logger.warning(f"Error counter: {self.consecutive_errors}/5")
        if self.consecutive_errors >= 5:
            self._halt(f"5 consecutive errors ({error_type})")
    
    def on_trade_close(self, pnl_usd: float, capital_at_time: float):
        today = datetime.now(timezone.utc).date()
        if today != self.daily_pnl_date:
            self.daily_pnl_usd = 0.0
            self.daily_pnl_date = today
            self.consecutive_losses = 0
        
        self.daily_pnl_usd += pnl_usd
        
        if pnl_usd < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                self._halt(f"{MAX_CONSECUTIVE_LOSSES} consecutive losses")
        else:
            self.consecutive_losses = 0
        
        daily_loss_pct = abs(self.daily_pnl_usd / capital_at_time * 100) if capital_at_time > 0 else 0
        if (self.daily_pnl_usd <= -MAX_DAILY_LOSS_USD or 
            (self.daily_pnl_usd < 0 and daily_loss_pct >= MAX_DAILY_LOSS_PCT)):
            self._halt(f"Daily loss limit hit: ${self.daily_pnl_usd:.2f}")
    
    def _halt(self, reason: str):
        if self.is_halted:
            return
        self.is_halted = True
        self.halt_reason = reason
        logger.critical(f"CIRCUIT BREAKER: {reason}")
        notify_circuit_breaker(reason)
    
    def can_trade(self) -> tuple[bool, Optional[str]]:
        if self.is_halted:
            return False, self.halt_reason
        return True, None
    
    def manual_resume(self):
        self.is_halted = False
        self.halt_reason = None
        self.consecutive_errors = 0
        self.consecutive_losses = 0
        logger.warning("HealthMonitor manually resumed")
    
    def to_dict(self) -> dict:
        uptime = time.time() - self.start_time
        seconds_since_loop = (time.time() - self.last_loop_time) if self.last_loop_time else None
        return {
            "status": "halted" if self.is_halted else "running",
            "halt_reason": self.halt_reason,
            "uptime_seconds": int(uptime),
            "seconds_since_last_loop": seconds_since_loop,
            "consecutive_errors": self.consecutive_errors,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl_usd": round(self.daily_pnl_usd, 2),
            "open_positions_count": self.open_positions_count,
            # Bug #18: expose last_signal_time for /health endpoint monitoring
            "last_signal_time": self.last_signal_time,
            "seconds_since_last_signal": (
                int(time.time() - self.last_signal_time)
                if self.last_signal_time else None
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            monitor = HealthMonitor()
            data = monitor.to_dict()
            
            seconds_since_loop = data.get("seconds_since_last_loop")
            is_alive = seconds_since_loop is None or seconds_since_loop < 300
            
            self.send_response(200 if is_alive else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass


def start_health_server(port: int = 8080):
    def _run():
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"Health endpoint listening on :{port}/health")
        server.serve_forever()
    
    Thread(target=_run, daemon=True).start()