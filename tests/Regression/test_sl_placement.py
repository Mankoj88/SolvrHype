"""
Stop-loss placement regressions.

Bug B (format): _place_stop_loss must pass triggerPx/limit_px as FLOATS. The SDK's
signing.float_to_wire formats every price with f"{x:.8f}" and raises
    ValueError: Unknown format code 'f' for object of type 'str'
on a string, so a str()-wrapped price made every hard-SL placement fail silently.

Bug (precision): exchange.order does NOT auto-round prices the way the SDK's
market_open does (via _slippage_price). Hyperliquid rejects any price with > 5
significant figures or > (6 - szDecimals) decimals for perps, IN-BAND
(status="ok" + statuses=[{"error": "...invalid price..."}]) — which used to be
swallowed → returned None → every entry-path SL failed (ETH 1663.452, SOL
67.54062 both rejected). _place_stop_loss now rounds via _round_price before
sending (covering entry, reconcile, and BE-stop callers), always surfaces the
rejection reason, and retries ONCE in a way that can actually succeed.

These tests use object.__new__(OrderManager) (the established harness pattern in
this repo) and a mock exchange.
"""
import time
from unittest.mock import MagicMock

import pytest

from execution.order_manager import OrderManager
from strategy.base_strategy import TradeSignal

pytestmark = [pytest.mark.regression]


# --- response builders ------------------------------------------------------

def _resting(oid=999):
    """exchange.order() success → one resting SL order carrying an oid."""
    return {"status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _inband_error(msg="Order has invalid price."):
    """exchange.order() in-band rejection: status ok, but an {"error": ...} status.
    This is exactly how Hyperliquid reports a precision/tick rejection."""
    return {"status": "ok",
            "response": {"data": {"statuses": [{"error": msg}]}}}


def _good_fill(avg_px="100.0", sz="1.0"):
    """exchange.market_open() success fill (price == signal.price → no slippage)."""
    return {"status": "ok",
            "response": {"data": {"statuses": [
                {"filled": {"avgPx": avg_px, "totalSz": sz, "oid": 1}}]}}}


def _signal(price=100.0, is_long=True):
    return TradeSignal(
        asset="BTC",
        price=price,
        timestamp_ms=int(time.time() * 1000),
        reason="test",
        indicators_snapshot={},
        strategy_type="spot",
        leverage=1,
        is_long=is_long,
        sl_mode="pct",
    )


# --- precision invariants (what Hyperliquid actually enforces) --------------

def _decimal_places(x: float) -> int:
    s = f"{float(x):.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s and s.split(".")[1] else 0


def _within_5_sig_figs(x: float) -> bool:
    """A value with <= 5 significant figures is unchanged by a :.5g round-trip."""
    return float(x) == float(f"{float(x):.5g}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries pause ~1s on transient errors — neutralise it so tests stay fast."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


@pytest.fixture
def om(tmp_path):
    """Minimally-wired OrderManager with a mock exchange and no open positions."""
    o = object.__new__(OrderManager)
    o.exchange = MagicMock()
    o.info = MagicMock()
    o.positions = {}
    # szDecimals pre-seeded → no meta() round-trip in _sz_decimals/_round_price.
    o._szDecimals_cache = {"BTC": 5, "ETH": 4, "SOL": 2, "DOGE": 0}
    o._cooldown_until = {}
    o.STATE_FILE = tmp_path / "positions.json"
    o.exchange.update_leverage = MagicMock(return_value={"status": "ok"})
    return o


# ---------------------------------------------------------------------------
# _round_price — the precision rule (perp: 5 sig figs AND 6 - szDecimals decimals)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset,raw,expected", [
    ("ETH", 1663.452, 1663.5),     # 7 sig figs / 3 dec → 5 sig figs, 2 dec
    ("SOL", 67.54062, 67.541),     # 7 sig figs / 5 dec → 5 sig figs, 4 dec
    ("BTC", 63700.0, 63700.0),     # already valid → unchanged
    ("SOL", 67.5, 67.5),           # already valid → unchanged
    ("DOGE", 0.12345678, 0.12346),  # high sig figs, small price → 5 sig figs
])
def test_round_price_matches_hyperliquid_rule(om, asset, raw, expected):
    rounded = om._round_price(asset, raw)
    assert rounded == expected
    assert _within_5_sig_figs(rounded), f"{rounded} exceeds 5 significant figures"
    assert _decimal_places(rounded) <= 6 - om._szDecimals_cache[asset]


def test_round_price_coerces_string_and_falls_back_safely(om):
    # string coerced (defensive)
    assert om._round_price("BTC", "63700.0") == 63700.0
    # uncached asset with a MagicMock info.meta() → lookup fails → 5sf/6dec fallback,
    # no crash.
    om._szDecimals_cache = {}
    om.info.meta = MagicMock(side_effect=RuntimeError("meta down"))
    assert om._round_price("BTC", 67.54062) == 67.541  # round(_, 6) keeps 5 sig figs


# ---------------------------------------------------------------------------
# _place_stop_loss — float + wire-valid precision (Bug B + precision)
# ---------------------------------------------------------------------------

def test_triggerpx_passed_as_float_not_str(om):
    """Core Bug B assertion: triggerPx (and limit_px) reach the SDK as floats."""
    om.exchange.order = MagicMock(return_value=_resting(oid=777))

    oid = om._place_stop_loss("BTC", 0.01, 63700.0, is_long=True)

    assert oid == 777
    om.exchange.order.assert_called_once()
    args, kwargs = om.exchange.order.call_args
    limit_px = args[3]               # 4th positional → SDK limit_px
    order_type = args[4]            # 5th positional → trigger order_type dict
    trigger_px = order_type["trigger"]["triggerPx"]

    assert not isinstance(trigger_px, str), "triggerPx must NOT be str-wrapped (Bug B)"
    assert isinstance(trigger_px, float), f"triggerPx must be float, got {type(trigger_px)}"
    assert trigger_px == 63700.0
    assert not isinstance(limit_px, str), "limit_px must NOT be str-wrapped"
    assert isinstance(limit_px, float), f"limit_px must be float, got {type(limit_px)}"
    assert limit_px == 63700.0
    assert kwargs.get("reduce_only") is True


def test_float_triggerpx_survives_real_sdk_signer(om):
    """The float triggerPx must NOT raise the 'Unknown format code f for str'
    ValueError. Pin the regression: the OLD str()-wrapped value DOES raise."""
    from hyperliquid.utils.signing import float_to_wire

    om.exchange.order = MagicMock(return_value=_resting())
    om._place_stop_loss("BTC", 0.01, 63700.0, is_long=True)
    args, _ = om.exchange.order.call_args
    trigger_px = args[4]["trigger"]["triggerPx"]

    # New behaviour: a float is wired without error.
    assert isinstance(float_to_wire(trigger_px), str)
    # Old behaviour (the bug): a str blows up exactly as reported.
    with pytest.raises(ValueError):
        float_to_wire(str(trigger_px))


def test_placement_rounds_triggerpx_and_limitpx_to_wire_precision(om):
    """The Bug: an over-precise SL trigger (SOL 67.54062, 7 sig figs / 5 dec) must be
    rounded to a wire-valid value BEFORE exchange.order — both triggerPx and limit_px."""
    om.exchange.order = MagicMock(return_value=_resting(oid=111))

    oid = om._place_stop_loss("SOL", 0.27, 67.54062, is_long=True)

    assert oid == 111
    args, _ = om.exchange.order.call_args
    limit_px = args[3]
    trigger_px = args[4]["trigger"]["triggerPx"]
    assert trigger_px == 67.541
    assert limit_px == 67.541, "limit_px must be rounded the same as triggerPx"
    # Generic precision invariants — what the exchange actually enforces.
    assert _within_5_sig_figs(trigger_px)
    assert _decimal_places(trigger_px) <= 6 - 2  # SOL szDecimals=2


def test_eth_overprecise_trigger_is_rounded(om):
    om.exchange.order = MagicMock(return_value=_resting(oid=222))
    oid = om._place_stop_loss("ETH", 0.0146, 1663.452, is_long=True)
    assert oid == 222
    args, _ = om.exchange.order.call_args
    assert args[4]["trigger"]["triggerPx"] == 1663.5
    assert _decimal_places(args[4]["trigger"]["triggerPx"]) <= 6 - 4  # ETH szDecimals=4


def test_string_trigger_price_coerced_no_crash(om):
    """Defensive coercion: a string trigger_price from any caller is float()'d, so
    it still reaches the SDK as a float and never crashes float_to_wire."""
    om.exchange.order = MagicMock(return_value=_resting(oid=555))

    oid = om._place_stop_loss("BTC", 0.01, "1.5", is_long=True)

    assert oid == 555
    args, _ = om.exchange.order.call_args
    trigger_px = args[4]["trigger"]["triggerPx"]
    assert trigger_px == 1.5
    assert isinstance(trigger_px, float)


def test_short_sl_places_opposite_side_float(om):
    """A short SL buys (opposite side) and still passes a float triggerPx."""
    om.exchange.order = MagicMock(return_value=_resting(oid=321))

    oid = om._place_stop_loss("BTC", 0.01, 105.0, is_long=False)

    assert oid == 321
    args, _ = om.exchange.order.call_args
    assert args[1] is True, "short SL must submit the opposite (buy) side"
    assert isinstance(args[4]["trigger"]["triggerPx"], float)


# ---------------------------------------------------------------------------
# In-band rejection visibility + conditional retry
# ---------------------------------------------------------------------------

def test_inband_price_rejection_is_logged_not_swallowed(monkeypatch, om):
    """status ok + {"error": "...invalid price..."} → reason logged, returns None
    (the previously-swallowed case)."""
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    om.exchange.order = MagicMock(return_value=_inband_error("Order has invalid price."))

    from loguru import logger
    logged = []
    sink_id = logger.add(lambda m: logged.append(m.record["message"]), level="ERROR")
    try:
        oid = om._place_stop_loss("ETH", 0.0146, 1663.452, is_long=True)
    finally:
        logger.remove(sink_id)

    assert oid is None
    assert any("invalid price" in m for m in logged), \
        "the exact in-band rejection reason must be logged (no more flying blind)"


def test_price_error_retry_uses_more_aggressive_rounding(monkeypatch, om):
    """First attempt rejected for a PRICE reason → retry with a rounder price
    (NOT the identical value), which then succeeds."""
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    seen = []

    def fake_order(asset, is_buy, sz, px, order_type, reduce_only=False):
        seen.append(px)
        return _inband_error("Order has invalid price.") if len(seen) == 1 else _resting(oid=888)

    om.exchange.order = MagicMock(side_effect=fake_order)

    oid = om._place_stop_loss("ETH", 0.0146, 1663.452, is_long=True)

    assert oid == 888
    assert len(seen) == 2
    first_px, second_px = seen
    assert first_px == 1663.5                                  # standard round
    assert second_px == om._round_price_aggressive("ETH", 1663.5)
    assert second_px != first_px, "must NOT re-send the price that just failed"


def test_transient_error_retries_same_value(monkeypatch, om):
    """A transient (non-price) error → retry the SAME rounded value after a pause."""
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    seen = []

    def fake_order(asset, is_buy, sz, px, order_type, reduce_only=False):
        seen.append(px)
        if len(seen) == 1:
            raise RuntimeError("network timeout")
        return _resting(oid=4242)

    om.exchange.order = MagicMock(side_effect=fake_order)

    oid = om._place_stop_loss("BTC", 0.01, 63700.0, is_long=True)

    assert oid == 4242
    assert seen == [63700.0, 63700.0], "transient retry must reuse the same value"


def test_both_attempts_fail_alerts_with_reason_and_returns_none(monkeypatch, om):
    """Retry also fails → ERROR logged with reason, Telegram alert fired (with the
    exchange reason), returns None. Entry is NOT rolled back elsewhere."""
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)
    om.exchange.order = MagicMock(return_value=_inband_error("Order has invalid price."))

    from loguru import logger
    logged = []
    sink_id = logger.add(lambda m: logged.append(m.record["message"]), level="ERROR")
    try:
        oid = om._place_stop_loss("ETH", 0.0146, 1663.452, is_long=True)
    finally:
        logger.remove(sink_id)

    assert oid is None
    assert any("Hard SL placement failed" in m for m in logged)
    alert.assert_called_once()
    msg = alert.call_args[0][0]
    assert "ETH" in msg and "soft SL" in msg and "invalid price" in msg


def test_place_stop_loss_returns_none_on_error_status(monkeypatch, om):
    """Genuine API rejection (status != ok) → retried, then returns None, no crash."""
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    om.exchange.order = MagicMock(return_value={"status": "err", "response": "rejected"})
    assert om._place_stop_loss("BTC", 0.01, 95.0, is_long=True) is None


def test_place_stop_loss_returns_none_on_exception(monkeypatch, om):
    """exchange.order raising → caught, retried, returns None (no propagation)."""
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    om.exchange.order = MagicMock(side_effect=RuntimeError("api down"))
    assert om._place_stop_loss("BTC", 0.01, 95.0, is_long=True) is None


# ---------------------------------------------------------------------------
# execute_entry — sl_oid storage + failure visibility (no rollback)
# ---------------------------------------------------------------------------

def test_entry_success_stores_sl_oid(monkeypatch, om):
    """A successful SL placement stores the real oid on the Position (not null)."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    om.exchange.market_open = MagicMock(return_value=_good_fill())
    om.exchange.order = MagicMock(return_value=_resting(oid=4242))

    ok = om.execute_entry(_signal(price=100.0), size_usd=100.0)

    assert ok is True
    assert "BTC" in om.positions
    assert om.positions["BTC"].sl_oid == 4242, \
        "successful SL placement must populate Position.sl_oid"
    # And it went out as a float, wire-valid triggerPx.
    args, _ = om.exchange.order.call_args
    trigger_px = args[4]["trigger"]["triggerPx"]
    assert isinstance(trigger_px, float)
    assert _within_5_sig_figs(trigger_px)


def test_entry_sl_failure_no_rollback_and_alerts(monkeypatch, om):
    """SL placement fails after entry fills → entry is KEPT (soft SL covers it),
    sl_oid is None, an error is logged, and a single Telegram alert fires."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    alert = MagicMock()
    monkeypatch.setattr("notifications.telegram.notify_critical_error", alert)

    from loguru import logger
    logged = []
    sink_id = logger.add(lambda m: logged.append(m.record["message"]), level="WARNING")
    try:
        om.exchange.market_open = MagicMock(return_value=_good_fill())
        om.exchange.order = MagicMock(side_effect=RuntimeError("api rejected SL"))

        ok = om.execute_entry(_signal(price=100.0), size_usd=100.0)
    finally:
        logger.remove(sink_id)

    # Entry NOT rolled back — soft SL still protects the position.
    assert ok is True
    assert "BTC" in om.positions, "entry must NOT be rolled back on SL-placement failure"
    assert om.positions["BTC"].sl_oid is None
    # Visibility: failure logged + exactly one Telegram alert (centralized, not double).
    assert any("Hard SL placement failed" in m for m in logged), \
        "a clear failure message must be logged on SL-placement failure"
    alert.assert_called_once()
    msg = alert.call_args[0][0]
    assert "BTC" in msg and "soft SL" in msg


def test_entry_no_position_means_no_market_close(monkeypatch, om):
    """Sanity: the SL-failure path does not trigger a position close/rollback."""
    monkeypatch.setattr("execution.order_manager.DRY_RUN", False)
    monkeypatch.setattr("notifications.telegram.notify_critical_error", MagicMock())
    om.exchange.market_open = MagicMock(return_value=_good_fill())
    om.exchange.order = MagicMock(side_effect=RuntimeError("api rejected SL"))
    om.exchange.market_close = MagicMock(return_value={"status": "ok"})

    om.execute_entry(_signal(price=100.0), size_usd=100.0)

    om.exchange.market_close.assert_not_called()
