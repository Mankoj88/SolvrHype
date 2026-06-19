"""
Bug A regression — equity double-count in WalletReader.get_unified_balance.

Before the fix, total_equity = perp_accountValue + spot_usdc_total. In a
Hyperliquid unified account the spot USDC `total` already contains the margin
locked for open perps (reported as `hold`), and that same margin is ALSO inside
marginSummary.accountValue. So the two terms double-count the margin:

    accountValue=108.8, spot_total=122.9 (hold=108.8)
    old (buggy):  108.8 + 122.9            = 231.7   <-- inflated
    new (fixed):  108.8 + (122.9 - 108.8)  = 122.9   <-- real equity

Fixed formula (c): total_equity = accountValue + FREE spot USDC
                                = accountValue + (spot_total - hold).

These tests drive WalletReader with a fake Info (no network) and assert the
corrected number, plus the Part-2 liquidation-price logging helper.
"""
import pytest
from loguru import logger

from execution.wallet import WalletReader

pytestmark = [pytest.mark.regression]


class FakeInfo:
    """Minimal Info double — returns canned user_state / spot_user_state."""

    def __init__(self, perp_state, spot_state):
        self._perp = perp_state
        self._spot = spot_state

    def user_state(self, account):
        return self._perp

    def spot_user_state(self, account):
        return self._spot


def _reader(perp_state, spot_state):
    return WalletReader(info=FakeInfo(perp_state, spot_state), account="0xTEST")


# --------------------------------------------------------------- 0 positions

def test_zero_positions_equity_equals_spot_usdc():
    """No open positions: accountValue=0, hold=0 → total_equity == spot_usdc.
    (No double count possible because accountValue is 0.)"""
    perp_state = {
        "marginSummary": {"accountValue": "0.0", "totalMarginUsed": "0.0"},
        "withdrawable": "123.0",
        "assetPositions": [],
    }
    spot_state = {"balances": [{"coin": "USDC", "total": "123.0", "hold": "0.0"}]}

    bal = _reader(perp_state, spot_state).get_unified_balance(force_refresh=True)

    assert bal.spot_usdc == pytest.approx(123.0)
    assert bal.total_equity == pytest.approx(123.0)
    # perp_equity is propagated = total_equity (what get_total_capital reads).
    assert bal.perp_equity == pytest.approx(123.0)


# --------------------------------------------------------------- positions open

def test_positions_open_no_double_count():
    """Logged scenario: accountValue=108.8, spot hold=108.8, spot total=122.9.
    Real equity is ~123, NOT the double-counted 231.7."""
    perp_state = {
        "marginSummary": {"accountValue": "108.8", "totalMarginUsed": "108.8"},
        "withdrawable": "14.1",
        "assetPositions": [
            {"position": {
                "coin": "BTC",
                "szi": "0.001",
                "entryPx": "100000.0",
                "positionValue": "108.8",
                "unrealizedPnl": "0.0",
                "marginUsed": "108.8",
                "liquidationPx": "50000.0",
            }},
        ],
    }
    spot_state = {"balances": [{"coin": "USDC", "total": "122.9", "hold": "108.8"}]}

    bal = _reader(perp_state, spot_state).get_unified_balance(force_refresh=True)

    # Pinned expected from formula (c): 108.8 + (122.9 - 108.8) = 122.9
    assert bal.total_equity == pytest.approx(122.9, abs=1e-6)
    assert bal.perp_equity == pytest.approx(122.9, abs=1e-6)   # flows to get_total_capital
    assert bal.spot_usdc_hold == pytest.approx(108.8)
    # The OLD buggy value would have been 231.7 — make sure we are nowhere near it.
    assert bal.total_equity != pytest.approx(231.7, abs=1.0)
    assert bal.total_equity < 200.0


def test_positions_open_with_unrealized_pnl():
    """accountValue already carries uPnL, so subtracting hold still yields the
    true equity. accountValue=110.8 (=margin 108.8 + uPnL 2.0), hold=108.8,
    spot total=122.9 → 110.8 + (122.9 - 108.8) = 124.9."""
    perp_state = {
        "marginSummary": {"accountValue": "110.8", "totalMarginUsed": "108.8"},
        "withdrawable": "14.1",
        "assetPositions": [
            {"position": {
                "coin": "ETH", "szi": "0.05", "entryPx": "2000.0",
                "positionValue": "110.8", "unrealizedPnl": "2.0",
                "marginUsed": "108.8", "liquidationPx": "1000.0",
            }},
        ],
    }
    spot_state = {"balances": [{"coin": "USDC", "total": "122.9", "hold": "108.8"}]}

    bal = _reader(perp_state, spot_state).get_unified_balance(force_refresh=True)

    assert bal.total_equity == pytest.approx(124.9, abs=1e-6)


# --------------------------------------------------------------- liq logging

def test_liq_metrics_present():
    """liquidationPx present → distance % computed from mark and liq."""
    pos = {
        "coin": "BTC", "szi": "0.001", "entryPx": "100000.0",
        "positionValue": "108.8", "liquidationPx": "50000.0",
    }
    m = WalletReader._liq_metrics(pos)

    assert m["coin"] == "BTC"
    # mark = positionValue / |szi| = 108.8 / 0.001 = 108800
    assert m["mark"] == pytest.approx(108800.0)
    assert m["liq"] == pytest.approx(50000.0)
    # distance = (108800 - 50000) / 108800 * 100 = 54.0441...%
    assert m["distance_pct"] == pytest.approx((108800 - 50000) / 108800 * 100)


def test_liq_metrics_null_is_na():
    """liquidationPx null/absent → liq and distance are None (rendered n/a)."""
    pos_null = {"coin": "BTC", "szi": "0.001", "positionValue": "108.8",
                "liquidationPx": None}
    pos_absent = {"coin": "ETH", "szi": "0.05", "positionValue": "100.0"}

    for pos in (pos_null, pos_absent):
        m = WalletReader._liq_metrics(pos)
        assert m["liq"] is None
        assert m["distance_pct"] is None


def test_log_liquidation_no_exception_and_emits():
    """_log_liquidation handles present + null liq without raising, logs n/a.
    Captures via a loguru sink (the project logs through loguru, not stdlib)."""
    perp_state = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.001", "entryPx": "100000.0",
                          "positionValue": "108.8", "liquidationPx": "50000.0"}},
            {"position": {"coin": "ETH", "szi": "0.05", "entryPx": "2000.0",
                          "positionValue": "100.0", "liquidationPx": None}},
        ],
    }
    reader = _reader(perp_state, {"balances": []})

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        reader._log_liquidation(perp_state)   # must not raise
    finally:
        logger.remove(sink_id)

    text = "".join(messages)
    assert "[LIQ_DIAG]" in text
    assert "liq=n/a" in text          # the null-liq position rendered gracefully
    assert "distance=" in text        # the present-liq position computed a distance
