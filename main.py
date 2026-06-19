"""
Solvira Phase A — Main Entry Point
"""
import time
import asyncio
import threading
import schedule
from datetime import datetime, timezone
from loguru import logger
from hyperliquid.info import Info

from config import (
    DRY_RUN, USE_TESTNET, LOGS_DIR, MAX_OPEN_POSITIONS, get_api_url,
    HYPERLIQUID_ACCOUNT, INITIAL_CAPITAL_USD,
    MAX_OPEN_POSITIONS_PER_STRATEGY, ENABLE_DERIVATIVE_STRATEGY,
    SCAN_CYCLE_SECONDS, POSITION_MANAGE_INTERVAL_SECONDS,
    DERIVATIVE_SCAN_EVERY_N_CYCLES, RECONCILE_INTERVAL_MIN,
)
from strategy.spot_strategy import SpotStrategy
from strategy.universe import UniverseFetcher
from execution.order_manager import OrderManager
from execution.allocation_manager import AllocationManager
from execution.withdraw_manager import WithdrawManager
from execution.wallet import WalletReader
from monitoring.trade_logger import init_db, log_daily_snapshot, get_daily_stats
from monitoring.health import HealthMonitor, start_health_server
from monitoring.stop_loss_enforcer import StopLossEnforcer
from notifications.telegram import (
    notify_startup, notify_shutdown, notify_daily_summary, notify_critical_error
)
from self_review.claude_review import run_weekly_review


# Setup logging
logger.add(
    LOGS_DIR / "bot.log",
    rotation="00:00",
    retention="30 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)
logger.add(
    LOGS_DIR / "errors.log",
    rotation="00:00",
    retention="60 days",
    level="ERROR",
)


class Solvira:
    def __init__(self):
        self.info = Info(get_api_url(), skip_ws=True)
        self.universe = UniverseFetcher(self.info)
        # OrderManager first: SpotStrategy needs it for the anti-duplicate gate.
        self.order_manager = OrderManager()
        self.spot_strategy = SpotStrategy(self.info, self.universe, self.order_manager)
        self.derivative_strategy = None
        if ENABLE_DERIVATIVE_STRATEGY:
            try:
                from strategy.derivative_strategy import DerivativeStrategy
                self.derivative_strategy = DerivativeStrategy(self.info, self.universe)
                logger.info("Derivative strategy ENABLED")
            except ImportError as e:
                logger.warning(f"Derivative strategy not available yet: {e}")
        # Serializes capital reads, scans, and position management between the
        # two concurrent async loops.
        self.state_lock = asyncio.Lock()
        # Pass info so spot sizing can resolve the exchange-min order notional.
        # Pass order_manager so pool usage is DERIVED from live open positions
        # (no separate reservation state to leak — see AllocationManager.pool_used).
        self.allocation_manager = AllocationManager(self.info, self.order_manager)
        self.withdraw_manager = WithdrawManager()
        self.wallet = WalletReader(
            info=self.info, exchange=self.order_manager.exchange,
            account=HYPERLIQUID_ACCOUNT,
        )
        self.health = HealthMonitor()
        self.stop_loss_enforcer = StopLossEnforcer(initial_capital=INITIAL_CAPITAL_USD)
        self._schedule_stop = threading.Event()  # Bug #13
        # Bug D: periodic reconcile clock. Startup reconcile already ran inside
        # OrderManager.__init__, so seed this to "now" — the first PERIODIC run
        # fires RECONCILE_INTERVAL_MIN later. monotonic() is immune to wall-clock
        # adjustments.
        self._last_reconcile_time = time.monotonic()

        # No reservation sync needed: allocation_manager.pool_used derives pool
        # usage directly from order_manager.positions (loaded + reconciled in
        # OrderManager.__init__), so existing positions are accounted for live.

    def get_total_capital(self) -> float:
        """Total tradeable equity dari marginSummary.accountValue.
        Unified account: spot+perp balance sudah merged, jadi perp_equity
        adalah sumber tunggal kebenaran (USDC spot sudah include di sini)."""
        try:
            bal = self.wallet.get_unified_balance()
            return bal.perp_equity
        except Exception as e:
            logger.warning(f"Failed to fetch capital: {e}")
            return 0
    
    def get_current_prices(self, assets: list[str]) -> dict[str, float]:
        try:
            mids = self.info.all_mids()
            return {a: float(mids[a]) for a in assets if a in mids}
        except Exception as e:
            logger.warning(f"Failed to fetch prices: {e}")
            return {}
    
    async def position_management_loop(self):
        """Runs every POSITION_MANAGE_INTERVAL_SECONDS. Manages open positions only."""
        while True:
            try:
                async with self.state_lock:
                    if self.order_manager.positions:
                        assets = list(self.order_manager.positions.keys())
                        current_prices = self.get_current_prices(assets)
                        self.order_manager.manage_open_positions(current_prices)
                    # Bug D: periodic reconcile runs INSIDE the same state_lock so
                    # it can never overlap an in-flight entry/close (which would
                    # corrupt state). It is gated to RECONCILE_INTERVAL_MIN.
                    self._maybe_periodic_reconcile()
                self.health.heartbeat()
            except Exception as e:
                logger.exception(f"Position management error: {e}")
                self.health.on_error(error_type=type(e).__name__)
            await asyncio.sleep(POSITION_MANAGE_INTERVAL_SECONDS)

    def _maybe_periodic_reconcile(self):
        """Bug D: fire _reconcile_with_exchange every RECONCILE_INTERVAL_MIN as a
        drift safety net (orphan/ghost + SL resync).

        Locking: this is called from inside position_management_loop's
        `async with self.state_lock` block, so it is already serialized against
        entries and closes. _reconcile_with_exchange is a SYNCHRONOUS OrderManager
        method that does NOT touch state_lock, so there is no second acquire and no
        deadlock on the non-reentrant asyncio.Lock — the lock is taken exactly once,
        by the loop.

        The interval clock is advanced BEFORE running so a persistent API outage
        can't make reconcile spam every 60s; on failure it logs + alerts and the
        next scheduled cycle retries. A failure here can never crash the loop:
        _reconcile_with_exchange already swallows its own exceptions, and this
        wrapper catches anything else as a belt-and-suspenders guard.
        """
        now = time.monotonic()
        if now - self._last_reconcile_time < RECONCILE_INTERVAL_MIN * 60:
            return
        self._last_reconcile_time = now
        logger.info(
            f"Periodic reconcile: running exchange/state drift check "
            f"(every {RECONCILE_INTERVAL_MIN} min, Bug D)"
        )
        try:
            self.order_manager._reconcile_with_exchange(periodic=True)
        except Exception as e:
            logger.exception(f"Periodic reconcile crashed (loop continues): {e}")
            try:
                notify_critical_error(str(e), error_type="periodic_reconcile")
            except Exception:
                pass

    async def scan_and_entry_loop(self):
        """Runs every SCAN_CYCLE_SECONDS. Spot every cycle, derivative every DERIVATIVE_SCAN_EVERY_N_CYCLES cycles."""
        cycle_counter = 0
        while True:
            try:
                # Existing pre-trade checks (3-month stop-loss enforcer, health circuit breakers)
                can_trade, reason = self.stop_loss_enforcer.check()
                if not can_trade:
                    logger.warning(f"3-month stop-loss enforcer: {reason}")
                    await asyncio.sleep(SCAN_CYCLE_SECONDS)
                    continue
                can_trade, reason = self.health.can_trade()
                if not can_trade:
                    logger.warning(f"Health monitor: {reason}")
                    await asyncio.sleep(SCAN_CYCLE_SECONDS)
                    continue

                async with self.state_lock:
                    capital = self.get_total_capital()
                    if capital == 0 and not DRY_RUN:
                        logger.warning("Capital is 0, skip cycle")
                        await asyncio.sleep(SCAN_CYCLE_SECONDS)
                        continue
                    elif capital == 0 and DRY_RUN:
                        capital = INITIAL_CAPITAL_USD

                    self.health.open_positions_count = self.order_manager.open_position_count()
                    open_by_strat = self.order_manager.open_position_count_by_strategy()

                    scan_tasks = []
                    if open_by_strat.get("spot", 0) < MAX_OPEN_POSITIONS_PER_STRATEGY["spot"]:
                        scan_tasks.append(("spot", asyncio.to_thread(self.spot_strategy.scan)))

                    run_derivative = (cycle_counter % DERIVATIVE_SCAN_EVERY_N_CYCLES == 0)
                    if (run_derivative
                            and self.derivative_strategy is not None
                            and open_by_strat.get("derivative", 0) < MAX_OPEN_POSITIONS_PER_STRATEGY["derivative"]):
                        scan_tasks.append(("derivative", asyncio.to_thread(self.derivative_strategy.scan)))

                    all_signals = []
                    if scan_tasks:
                        results = await asyncio.gather(*(t for _, t in scan_tasks), return_exceptions=True)
                        for (label, _), result in zip(scan_tasks, results):
                            if isinstance(result, RuntimeError):
                                logger.warning(f"{label} scan cycle skipped (API unavailable): {result}")
                                continue
                            if isinstance(result, Exception):
                                logger.exception(f"{label} scan cycle unexpected error: {result}", exc_info=result)
                                continue
                            all_signals.extend(result)

                    for signal in all_signals:
                        if self.health.open_positions_count >= MAX_OPEN_POSITIONS:
                            break
                        size_usd = self.allocation_manager.calculate_position_size(
                            asset=signal.asset,
                            total_capital=capital,
                            strategy_type=signal.strategy_type,
                            signal=signal,
                        )
                        if size_usd <= 0:
                            continue
                        success = self.order_manager.execute_entry(signal, size_usd)
                        if success:
                            # No reserve() call: the new position is now in
                            # order_manager.positions, so pool_used picks it up
                            # on the next calculate_position_size automatically.
                            self.health.open_positions_count += 1
                            self.health.last_signal_time = time.time()

                    if self.withdraw_manager.should_withdraw():
                        self.withdraw_manager.execute_withdraw()
                    self.health.on_success()

                cycle_counter += 1
            except Exception as e:
                logger.exception(f"Scan loop error: {e}")
                self.health.on_error(error_type=type(e).__name__)
                notify_critical_error(str(e), error_type=type(e).__name__)
            await asyncio.sleep(SCAN_CYCLE_SECONDS)

    def _run_schedule_loop(self):
        """Bug #13: run schedule in a daemon thread so it never blocks the asyncio loop."""
        while not self._schedule_stop.is_set():
            schedule.run_pending()
            self._schedule_stop.wait(timeout=30)

    async def main_loop(self):
        notify_startup()
        logger.info(f"Solvira started. TESTNET={USE_TESTNET}, DRY_RUN={DRY_RUN}, "
                    f"scan_interval={SCAN_CYCLE_SECONDS}s, "
                    f"position_interval={POSITION_MANAGE_INTERVAL_SECONDS}s, "
                    f"deriv_every={DERIVATIVE_SCAN_EVERY_N_CYCLES}")
        schedule.every().day.at("23:59").do(self._daily_summary_job)
        schedule.every().monday.at("09:00").do(self._weekly_review_job)
        schedule.every().day.at("00:05").do(self._daily_snapshot_job)
        schedule_thread = threading.Thread(target=self._run_schedule_loop, daemon=True, name="schedule-loop")
        schedule_thread.start()

        try:
            await asyncio.gather(
                self.position_management_loop(),
                self.scan_and_entry_loop(),
            )
        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
            self._schedule_stop.set()
            notify_shutdown("manual")
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            self._schedule_stop.set()
            notify_shutdown(f"fatal: {e}")
            raise
    
    def _daily_summary_job(self):
        try:
            stats = get_daily_stats()
            bal = self.wallet.get_unified_balance(force_refresh=True)
            # Unified account: perp_equity = total equity (spot+perp merged)
            stats["capital"] = bal.perp_equity
            stats["balance"] = bal.to_dict()
            stats["pending_withdraw"] = self.withdraw_manager.state.get(
                "cumulative_profit_pending", 0
            )
            notify_daily_summary(stats)
        except Exception as e:
            logger.exception(f"Daily summary failed: {e}")

    def _weekly_review_job(self):
        try:
            run_weekly_review()
        except Exception as e:
            logger.exception(f"Weekly review failed: {e}")
    
    def _daily_snapshot_job(self):
        try:
            capital = self.get_total_capital()
            stats = get_daily_stats()
            log_daily_snapshot(
                capital_usd=capital,
                open_positions_count=self.order_manager.open_position_count(),
                open_positions_value_usd=self.order_manager.get_open_positions_value_usd(
                    self.get_current_prices(list(self.order_manager.positions.keys()))
                ),
                cumulative_usdt_wallet=self.withdraw_manager.state.get("cumulative_profit_pending", 0),
                daily_pnl_usd=stats["pnl_usd"],
                daily_trades_count=stats["total_trades"],
            )
        except Exception as e:
            logger.exception(f"Daily snapshot failed: {e}")


def main():
    init_db()
    start_health_server(port=8080)
    
    bot = Solvira()
    asyncio.run(bot.main_loop())


if __name__ == "__main__":
    main()
    