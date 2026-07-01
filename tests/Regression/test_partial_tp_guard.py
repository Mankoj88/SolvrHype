"""
Partial-TP guard regression — a failed/None market_close on a PARTIAL take-profit
must NOT (a) crash-log, (b) advance tp_levels_remaining / tp_hit_count, or
(c) move SL to breakeven when nothing was actually sold.

Before the fix, _execute_partial_tp assumed market_close returned a dict and did
`result.get("status")`. The Hyperliquid SDK returns None when the coin is no
longer in assetPositions (already-gone / race), so that call raised
AttributeError — swallowed by a broad except as a crash-log — while
manage_open_positions had ALREADY decided to advance pos.tp_levels_remaining and
set breakeven UNCONDITIONALLY (the return value was ignored). Net effect: a TP
that never filled was marked done and the SL was pulled to entry on a phantom
fill.

The fix mirrors the Bug C discipline (never advance state on an unconfirmed
exchange action, see test_close_verify.py):
  * _execute_partial_tp returns a success bool. None / non-dict / status != "ok"
    / exception → False, no state mutation, no raise.
  * manage_open_positions advances tp_levels_remaining and sets breakeven ONLY
    when the partial TP returned True; otherwise it leaves both UNCHANGED so the
    TP is retried next cycle.

STEP 3 note (verify-fill guard — SKIPPED, deliberately):
  _close_full_position confirms Bug C by re-querying szi until flat and retrying
  up to CLOSE_MAX_ATTEMPTS. That mechanism is safe ONLY because a full close is
  idempotent (reduce_only; re-closing a flat position is a no-op). A partial TP
  sell is NOT idempotent: a false-negative from a lagged user_state re-read would
  make the next management cycle sell an ADDITIONAL fraction (over-sell). So the
  szi-drop re-query is intentionally NOT ported here — the faithful, safe port of
  Bug C's discipline for partials is to validate the SDK ack (this file) and let
  the caller retry only when nothing was acknowledged.

Uses object.__new__(OrderManager) — the established harness pattern in this repo.
"""
import time
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.regression]


# tp_levels: TP1 sells 50% with an EXPLICIT "breakeven" post-action (this is the
# path that would move SL regardless of tp_hit_count — the exact (c) hazard), TP2
# closes the remainder. Entry 100, SL 95, price 104 → +4% ≥ TP1 (3%), below TP2
# (5%), above SL → only TP1 fires.
_TP_LEVELS = [[3.0, 0.5, "breakeven"], [5.0, 1.0]]


@pytest.fixture
def make_om(tmp_path, make_position):
    """Factory: a minimally-wired OrderManager with one BTC spot-long position
    sitting at +4% (TP1 armed). Pass market_close_return / market_close_side_effect
    to control the sell result. Internal side-effecting collaborators are stubbed
    so we can assert exactly what fired.
    """
    from execution.order_manager import OrderManager, Position

    def _make(*, market_close_return=None, market_close_side_effect=None, **pos_overrides):
        om = object.__new__(OrderManager)
        om.exchange = MagicMock()
        if market_close_side_effect is not None:
            om.exchange.market_close = MagicMock(side_effect=market_close_side_effect)
        else:
            om.exchange.market_close = MagicMock(return_value=market_close_return)
        om.info = MagicMock()
        om._szDecimals_cache = {"BTC": 5}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"
        # Stub side-effecting collaborators so we can observe intent without I/O.
        om._save_state = MagicMock()
        om._update_stop_loss = MagicMock()          # spy: breakeven move
        om._on_position_close_partial = MagicMock()  # spy: partial DB write
        om._on_position_close_full = MagicMock()

        overrides = dict(
            asset="BTC", entry_price=100.0, entry_size_coin=1.0,
            remaining_size_coin=0.4, initial_sl_price=95.0, current_sl_price=95.0,
            sl_oid=12345, tp_hit_count=0, tp_levels_remaining=[list(l) for l in _TP_LEVELS],
            entry_time_ms=int(time.time() * 1000) - 60_000,  # 1 min ago → no max_hold
        )
        overrides.update(pos_overrides)
        pos = Position.from_dict(make_position(**overrides))
        om.positions = {pos.asset: pos}
        return om

    return _make


# ---------------------------------------------------------------------------
# _execute_partial_tp — direct return contract
# ---------------------------------------------------------------------------

def test_none_return_is_false_and_does_not_raise(monkeypatch, make_om):
    """SDK returns None (coin gone / race) → False, no crash, no state mutation."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return=None)
    pos = om.positions["BTC"]

    # Must not raise (previously AttributeError on None.get(...)).
    ok = om._execute_partial_tp("BTC", 0.5, 3.0, 104.0, is_last_tp=False)

    assert ok is False
    om.exchange.market_close.assert_called_once()
    assert pos.tp_hit_count == 0                 # not advanced
    assert pos.remaining_size_coin == 0.4        # nothing sold
    om._on_position_close_partial.assert_not_called()


def test_err_status_is_false(monkeypatch, make_om):
    """status='err' → False, no state mutation."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return={"status": "err", "response": "rejected"})
    pos = om.positions["BTC"]

    assert om._execute_partial_tp("BTC", 0.5, 3.0, 104.0, is_last_tp=False) is False
    assert pos.tp_hit_count == 0
    assert pos.remaining_size_coin == 0.4
    om._on_position_close_partial.assert_not_called()


def test_exception_is_caught_and_returns_false(monkeypatch, make_om):
    """market_close raises → caught, no crash, False, no state mutation."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_side_effect=RuntimeError("network down"))
    pos = om.positions["BTC"]

    assert om._execute_partial_tp("BTC", 0.5, 3.0, 104.0, is_last_tp=False) is False
    assert pos.tp_hit_count == 0
    assert pos.remaining_size_coin == 0.4


def test_ok_result_is_true_and_mutates_state(monkeypatch, make_om):
    """status='ok' → True, tp_hit_count++ and remaining reduced by the sell."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return={"status": "ok"})
    pos = om.positions["BTC"]

    assert om._execute_partial_tp("BTC", 0.5, 3.0, 104.0, is_last_tp=False) is True
    assert pos.tp_hit_count == 1
    assert pos.remaining_size_coin == pytest.approx(0.2)  # sold 50% of 0.4
    om._on_position_close_partial.assert_called_once()


# ---------------------------------------------------------------------------
# manage_open_positions — TP state / breakeven must be gated on the sell
# ---------------------------------------------------------------------------

def test_manage_none_leaves_tp_and_sl_unchanged(monkeypatch, make_om):
    """(a)+(b)+(c): market_close None → tp_levels UNCHANGED, tp_hit_count 0,
    SL NOT moved to breakeven, position retained, no crash."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return=None)
    pos = om.positions["BTC"]

    om.manage_open_positions(current_prices={"BTC": 104.0})  # +4% → TP1 armed

    assert pos.tp_levels_remaining == _TP_LEVELS, "TP levels must NOT advance"
    assert pos.tp_hit_count == 0
    om._update_stop_loss.assert_not_called()      # breakeven must NOT fire
    assert pos.current_sl_price == 95.0           # SL untouched
    assert "BTC" in om.positions


def test_manage_err_leaves_tp_and_sl_unchanged(monkeypatch, make_om):
    """status='err' behaves the same as None: no advance, no breakeven."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return={"status": "err"})
    pos = om.positions["BTC"]

    om.manage_open_positions(current_prices={"BTC": 104.0})

    assert pos.tp_levels_remaining == _TP_LEVELS
    assert pos.tp_hit_count == 0
    om._update_stop_loss.assert_not_called()
    assert pos.current_sl_price == 95.0


def test_manage_ok_advances_tp_and_sets_breakeven(monkeypatch, make_om):
    """Happy path unchanged: status='ok' → TP1 consumed, tp_hit_count=1,
    breakeven SL move requested, position retained (TP2 remains)."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om = make_om(market_close_return={"status": "ok"})
    pos = om.positions["BTC"]

    om.manage_open_positions(current_prices={"BTC": 104.0})

    assert pos.tp_levels_remaining == [[5.0, 1.0]], "TP1 consumed, TP2 remains"
    assert pos.tp_hit_count == 1
    om._update_stop_loss.assert_called_once_with(pos, pos.entry_price)  # breakeven
    om._on_position_close_partial.assert_called_once()
    assert "BTC" in om.positions
