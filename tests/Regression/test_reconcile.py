"""
Bug D regression — periodic reconcile safety net + SL check-before-replace.

Two behaviours are pinned here:

1. SL check-before-replace (_reconcile_with_exchange):
   The OLD reconcile blanked every Position.sl_oid and then re-placed an SL for
   ALL positions — opening a window where a position had no hard SL on the
   exchange. The fix queries the LIVE resting reduce-only trigger (SL) orders via
   frontend_open_orders and, per managed position:
     * adopts the live SL's oid if one already rests (NO cancel, NO re-place), or
     * places a fresh SL ONLY when none is live.
   sl_oid is never blanked first → no blank-then-replace gap.

2. Periodic invocation (main.Solvira._maybe_periodic_reconcile):
   The same reconcile runs every RECONCILE_INTERVAL_MIN from inside the 60s
   position_management_loop (under state_lock). The gate must not fire early, must
   fire once the interval elapses, and a reconcile failure must never crash the
   loop.

Plus orphan adoption (exchange-only → state) and ghost cleanup (state-only →
removed, NO DB close row, alert only).

Harness: object.__new__(OrderManager) / object.__new__(Solvira), the established
pattern in this repo.
"""
import time
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.regression]


# ---------------------------------------------------------------------------
# user_state / open-orders builders
# ---------------------------------------------------------------------------

def _open_state(asset="BTC", szi="0.4", entry="100.0"):
    """user_state where `asset` is open on the exchange (szi != 0)."""
    return {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [
            {"position": {"coin": asset, "szi": szi, "entryPx": entry}}
        ],
    }


def _empty_state():
    """user_state with no open positions on the exchange."""
    return {"marginSummary": {"accountValue": "1000.0"}, "assetPositions": []}


def _sl_order(coin="BTC", oid=888):
    """A live resting reduce-only trigger (stop-loss) order, frontend shape."""
    return {
        "coin": coin, "oid": oid, "reduceOnly": True, "isTrigger": True,
        "orderType": "Stop Market", "side": "A", "sz": "0.4",
        "triggerPx": "95.0", "limitPx": "95.0",
    }


@pytest.fixture
def make_om(tmp_path, make_position):
    """Factory: a minimally-wired OrderManager.

    Pass user_state= (what the exchange reports), open_orders= (live resting
    orders for the SL adopt/place decision), and with_btc=True/False to seed a
    BTC position in local state. _place_stop_loss is stubbed so we can assert it
    is / is not called and that the returned oid is stored.
    """
    from execution.order_manager import OrderManager, Position

    def _make(*, user_state, open_orders=None, with_btc=True, **pos_overrides):
        om = object.__new__(OrderManager)
        om.exchange = MagicMock()
        om.exchange.cancel = MagicMock(return_value={"status": "ok"})
        om.info = MagicMock()
        om.info.user_state = MagicMock(return_value=user_state)
        om.info.frontend_open_orders = MagicMock(return_value=open_orders or [])
        om.info.all_mids = MagicMock(return_value={"BTC": "100.0"})
        om._szDecimals_cache = {"BTC": 5}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"
        om._place_stop_loss = MagicMock(return_value=4242)
        om.positions = {}
        if with_btc:
            overrides = dict(
                asset="BTC", entry_price=100.0, entry_size_coin=0.4,
                remaining_size_coin=0.4, initial_sl_price=95.0,
                current_sl_price=95.0, sl_oid=None,
                tp_levels_remaining=[[3.0, 0.5], [5.0, 1.0]],
            )
            overrides.update(pos_overrides)
            pos = Position.from_dict(make_position(**overrides))
            om.positions = {pos.asset: pos}
        return om

    return _make


# ---------------------------------------------------------------------------
# Step 1 — SL check-before-replace
# ---------------------------------------------------------------------------

def test_live_sl_adopted_not_replaced(monkeypatch, make_om):
    """A live reduce-only trigger already rests for BTC → reconcile adopts its
    oid; it does NOT cancel and does NOT re-place. No window without SL."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_open_state(), open_orders=[_sl_order(oid=888)])

    om._reconcile_with_exchange(periodic=True)

    assert om.positions["BTC"].sl_oid == 888, "must adopt the live SL's oid"
    om._place_stop_loss.assert_not_called()      # core: no re-place
    om.exchange.cancel.assert_not_called()        # core: no blank/cancel


def test_no_live_sl_places_one(monkeypatch, make_om):
    """No live SL on the exchange → reconcile places exactly one and stores the
    returned oid."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_open_state(), open_orders=[])

    om._reconcile_with_exchange(periodic=True)

    om._place_stop_loss.assert_called_once()
    args = om._place_stop_loss.call_args[0]
    assert args[0] == "BTC"
    assert om.positions["BTC"].sl_oid == 4242, "stored placed SL oid"
    om.exchange.cancel.assert_not_called()        # still never blanks first


def test_open_orders_query_failure_skips_sl_sync(monkeypatch, make_om):
    """If the open-orders query raises, reconcile skips the SL sync this cycle:
    it neither blanks nor re-places (keeps the existing sl_oid + soft SL)."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_open_state(), sl_oid=777)
    om.info.frontend_open_orders = MagicMock(side_effect=RuntimeError("api down"))

    om._reconcile_with_exchange(periodic=True)

    om._place_stop_loss.assert_not_called()
    om.exchange.cancel.assert_not_called()
    assert om.positions["BTC"].sl_oid == 777, "existing sl_oid retained on query failure"


# ---------------------------------------------------------------------------
# Step 3 — orphan adoption & ghost cleanup
# ---------------------------------------------------------------------------

def test_exchange_orphan_adopted_and_alerts(monkeypatch, make_om):
    """A position on the exchange but absent from state is adopted as spot, with
    a pct SL placed (none live), and an alert fires."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(user_state=_open_state("SOL", "10.0", "150.0"),
                 open_orders=[], with_btc=False)

    om._reconcile_with_exchange(periodic=True)

    assert "SOL" in om.positions, "exchange orphan must be adopted into state"
    pos = om.positions["SOL"]
    assert pos.strategy_type == "spot"
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.current_sl_price > 0
    om._place_stop_loss.assert_called_once()       # no live SL → places
    assert pos.sl_oid == 4242
    assert alert.called, "orphan adoption must fire a Telegram alert"
    assert any("orphan" in str(c.args[0]).lower() for c in alert.call_args_list)


def test_state_ghost_removed_no_db_row(monkeypatch, make_om):
    """A position in state but NOT on the exchange is removed + alerted, and NO DB
    close row is written (real exit unknown)."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    log_trade = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    monkeypatch.setattr("monitoring.trade_logger.log_trade", log_trade)
    om = make_om(user_state=_empty_state())  # BTC in state, nothing on exchange

    om._reconcile_with_exchange(periodic=True)

    assert "BTC" not in om.positions, "ghost must be removed from state"
    log_trade.assert_not_called()                  # core: NO fabricated DB close row
    assert alert.called, "ghost cleanup must fire a Telegram alert"
    assert any("ghost" in str(c.args[0]).lower() for c in alert.call_args_list)


# ---------------------------------------------------------------------------
# Reconcile robustness — API failure can't crash, alerts instead
# ---------------------------------------------------------------------------

def test_reconcile_user_state_failure_caught(monkeypatch, make_om):
    """user_state raising is swallowed (no propagation), state is left intact, and
    a failure alert fires."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(user_state=_open_state())
    om.info.user_state = MagicMock(side_effect=RuntimeError("network down"))

    om._reconcile_with_exchange(periodic=True)     # must NOT raise

    assert "BTC" in om.positions, "state preserved when reconcile fails"
    assert alert.called
    assert any(c.args[1] == "reconcile_failed" for c in alert.call_args_list)


# ---------------------------------------------------------------------------
# Step 2 — periodic trigger gating in main.Solvira
# ---------------------------------------------------------------------------

def _make_bot():
    """object.__new__(Solvira) with just the fields _maybe_periodic_reconcile uses."""
    from main import Solvira
    bot = object.__new__(Solvira)
    bot._last_reconcile_time = time.monotonic()
    bot.order_manager = MagicMock()
    return bot


def test_periodic_does_not_run_before_interval():
    bot = _make_bot()  # last_reconcile = now → interval not elapsed
    bot._maybe_periodic_reconcile()
    bot.order_manager._reconcile_with_exchange.assert_not_called()


def test_periodic_runs_after_interval():
    from config import RECONCILE_INTERVAL_MIN
    bot = _make_bot()
    bot._last_reconcile_time = time.monotonic() - (RECONCILE_INTERVAL_MIN * 60 + 1)

    bot._maybe_periodic_reconcile()

    bot.order_manager._reconcile_with_exchange.assert_called_once_with(periodic=True)
    # clock advanced → a second immediate call does NOT run again
    bot.order_manager._reconcile_with_exchange.reset_mock()
    bot._maybe_periodic_reconcile()
    bot.order_manager._reconcile_with_exchange.assert_not_called()


def test_periodic_failure_does_not_crash_loop(monkeypatch):
    from config import RECONCILE_INTERVAL_MIN
    monkeypatch.setattr("main.notify_critical_error", MagicMock())
    bot = _make_bot()
    bot.order_manager._reconcile_with_exchange = MagicMock(
        side_effect=RuntimeError("api down")
    )
    bot._last_reconcile_time = time.monotonic() - (RECONCILE_INTERVAL_MIN * 60 + 1)

    # Must NOT raise — the loop has to survive a reconcile failure.
    bot._maybe_periodic_reconcile()

    bot.order_manager._reconcile_with_exchange.assert_called_once()
