"""
Bug C regression — full-close MUST verify the fill before recording it closed.

Before the fix, _close_full_position fired exchange.market_close() and IGNORED
the result, then unconditionally wrote a "closed" trade row and popped the
position from state. If the close didn't actually fill (error status, None
return, zero/partial fill, or exception) the position stayed open on the
exchange while the bot believed it was closed → "phantom close" (this orphaned
a live LIT position).

The fix:
  * _close_full_position returns a success bool.
  * It records the close (_on_position_close_full = DB/tax/health) ONLY when the
    exchange re-query confirms the position is flat (szi == 0 / absent).
  * On an unconfirmed close it retries up to CLOSE_MAX_ATTEMPTS (3) total, then
    keeps the position in state, fires a Telegram alert, and returns False.
  * manage_open_positions removes the position ONLY on a confirmed close.

These tests use object.__new__(OrderManager) (the established harness pattern in
this repo) and wire `om.info` so the definitive flat-check path is exercised.
"""
import time
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.regression]


def _flat_user_state():
    """user_state with no open positions → asset is flat."""
    return {"marginSummary": {"accountValue": "1000.0"}, "assetPositions": []}


def _open_user_state(asset="BTC", szi="0.4"):
    """user_state where `asset` is still open (szi != 0) → NOT flat."""
    return {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [{"position": {"coin": asset, "szi": szi,
                                         "entryPx": "100.0"}}],
    }


@pytest.fixture
def make_om(tmp_path, make_position):
    """Factory: a minimally-wired OrderManager with one BTC position.

    Pass user_state= to control what the flat-check re-query sees, and
    market_close_return / market_close_side_effect to control the close result.
    Sets CLOSE_RETRY_SLEEP_SEC=0 so retries don't actually sleep.
    """
    from execution.order_manager import OrderManager, Position

    def _make(*, user_state, market_close_return=None, market_close_side_effect=None,
              recent_entry=True, **pos_overrides):
        om = object.__new__(OrderManager)
        om.exchange = MagicMock()
        if market_close_side_effect is not None:
            om.exchange.market_close = MagicMock(side_effect=market_close_side_effect)
        else:
            om.exchange.market_close = MagicMock(return_value=market_close_return)
        om.exchange.cancel = MagicMock(return_value={"status": "ok"})
        om.info = MagicMock()
        om.info.user_state = MagicMock(return_value=user_state)
        om._szDecimals_cache = {"BTC": 5}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"
        om.CLOSE_RETRY_SLEEP_SEC = 0  # don't sleep in tests
        # Stub the DB/tax/health write so we can assert it fires exactly once.
        om._on_position_close_full = MagicMock()

        overrides = dict(
            asset="BTC", entry_price=100.0, entry_size_coin=1.0,
            remaining_size_coin=0.4, initial_sl_price=95.0, current_sl_price=95.0,
            sl_oid=12345, tp_hit_count=1, tp_levels_remaining=[[20.0, 1.0]],
        )
        if recent_entry:
            overrides["entry_time_ms"] = int(time.time() * 1000) - 60_000  # 1 min ago
        overrides.update(pos_overrides)
        pos = Position.from_dict(make_position(**overrides))
        om.positions = {pos.asset: pos}
        return om

    return _make


# ---------------------------------------------------------------------------
# _close_full_position — direct unit behavior
# ---------------------------------------------------------------------------

def test_dry_run_returns_true_without_calling_exchange(monkeypatch, make_om):
    monkeypatch.setattr("execution.order_manager.DRY_RUN", True)
    om = make_om(user_state=_flat_user_state())

    assert om._close_full_position("BTC", "sl", 94.0) is True
    om.exchange.market_close.assert_not_called()
    om._on_position_close_full.assert_called_once()


def test_confirmed_flat_records_and_returns_true(monkeypatch, make_om):
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_flat_user_state(), market_close_return={"status": "ok"})

    assert om._close_full_position("BTC", "sl", 94.0) is True
    om.exchange.market_close.assert_called_once()
    om._on_position_close_full.assert_called_once()  # DB write fires exactly once


def test_status_ok_but_not_flat_retries_then_fails(monkeypatch, make_om):
    """status='ok' but szi never reaches 0 (partial/no fill) → False after 3
    attempts, NO DB write, alert fired."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(user_state=_open_user_state(), market_close_return={"status": "ok"})

    assert om._close_full_position("BTC", "sl", 94.0) is False
    assert om.exchange.market_close.call_count == om.CLOSE_MAX_ATTEMPTS == 3
    om._on_position_close_full.assert_not_called()  # core regression: no phantom DB close
    alert.assert_called_once()


def test_none_return_retries_then_fails(monkeypatch, make_om):
    """market_close returns None and the position stays open → False, retried,
    no DB write, alert fired."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(user_state=_open_user_state(), market_close_return=None)

    assert om._close_full_position("BTC", "sl", 94.0) is False
    assert om.exchange.market_close.call_count == 3
    om._on_position_close_full.assert_not_called()
    alert.assert_called_once()


def test_exception_is_caught_returns_false(monkeypatch, make_om):
    """market_close raises every attempt → caught, no crash, False, no DB write."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(
        user_state=_open_user_state(),
        market_close_side_effect=RuntimeError("network down"),
    )

    assert om._close_full_position("BTC", "sl", 94.0) is False
    assert om.exchange.market_close.call_count == 3
    om._on_position_close_full.assert_not_called()
    alert.assert_called_once()


def test_confirmed_flat_despite_none_result(monkeypatch, make_om):
    """If the exchange re-query says flat, a None/odd result still counts as a
    confirmed close (e.g. SDK returned None because the position was already
    gone). DB write fires, returns True."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_flat_user_state(), market_close_return=None)

    assert om._close_full_position("BTC", "sl", 94.0) is True
    om._on_position_close_full.assert_called_once()


# ---------------------------------------------------------------------------
# manage_open_positions — state removal must be conditional on confirmed close
# ---------------------------------------------------------------------------

def test_manage_retains_position_on_failed_close(monkeypatch, make_om):
    """SL fires, close not confirmed flat → position KEPT in state, no DB write,
    alert fired (so soft-SL retries it next cycle). This is the LIT phantom fix."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om = make_om(user_state=_open_user_state(), market_close_return={"status": "ok"})

    om.manage_open_positions(current_prices={"BTC": 94.0})  # below SL (95) → trigger

    assert "BTC" in om.positions, "position must be retained when close unconfirmed"
    om._on_position_close_full.assert_not_called()
    alert.assert_called_once()


def test_manage_removes_position_on_confirmed_close(monkeypatch, make_om):
    """SL fires, close confirmed flat → position removed + DB write once."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(user_state=_flat_user_state(), market_close_return={"status": "ok"})

    om.manage_open_positions(current_prices={"BTC": 94.0})  # below SL (95) → trigger

    assert "BTC" not in om.positions, "position must be removed on confirmed close"
    om._on_position_close_full.assert_called_once()
