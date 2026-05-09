import json
import pytest

pytestmark = [pytest.mark.regression, pytest.mark.blocker]


# =============================================================================
# Bug #1 — Scanner uses iloc[-1] (current candle) instead of iloc[-2] (closed)
# Severity: 🔴 BLOCKER — look-ahead bias inflates backtest, wrong live signals
# =============================================================================
class TestBug01ScannerLookahead:
    def test_scanner_uses_closed_candle_only(self, oversold_setup_df):
        """Scanner must read indicators from iloc[-2], not iloc[-1]."""
        # TODO: import your scanner module
        # from strategy.scanner import Scanner
        # scanner = Scanner(...)
        # signal = scanner.evaluate(oversold_setup_df)
        # assert signal is not None
        # # The signal should be based on iloc[-2] indicators, not iloc[-1]
        pytest.skip("Implement after wiring real scanner module")


# =============================================================================
# Bug #2 — TP2 sizing multiplies entry_size_coin × sell_pct (wrong)
# Severity: 🔴 BLOCKER — partial close size incorrect, leaves dust positions
# =============================================================================
class TestBug02Tp2Sizing:
    def test_tp2_closes_remaining_size_after_tp1(self, make_position):
        """TP2 must close (entry_size_coin - tp1_close_size), not size × sell_pct."""
        pos = make_position(entry_size_coin=0.003076, tp1_hit=True)
        # After TP1 closed 50%, TP2 should close the remaining 50%.
        # Wrong implementation: 0.003076 * 0.5 = 0.001538 (only 25% of original!)
        # Correct: track what was closed at TP1, close the rest.
        pytest.skip("Implement after wiring order_manager.handle_tp2()")


# =============================================================================
# Bug #3 — SL handler doesn't fully close position
# Severity: 🔴 BLOCKER — risk management broken
# =============================================================================
class TestBug03SlFullClose:
    def test_sl_closes_entire_remaining_size(self, mock_hl_exchange, make_position):
        pos = make_position(tp1_hit=True)  # TP1 already hit, half remaining
        # On SL, must close ALL remaining size, not the original size
        pytest.skip("Implement after wiring order_manager.handle_sl()")


# =============================================================================
# Bug #4 — Funding window check uses 8h, but Hyperliquid uses 1h
# Severity: 🟡 MAJOR — pays funding when it shouldn't open
# =============================================================================
class TestBug04FundingWindow:
    def test_funding_window_is_1h(self, funding_window):
        """At HH:00:30, must detect we're in funding window."""
        # from strategy.scanner import is_in_funding_window
        # assert is_in_funding_window() is True
        pytest.skip("Implement after wiring funding window check")

    def test_funding_window_clear_at_5min_past(self, freeze_clock):
        from freezegun import freeze_time
        with freeze_time("2026-05-07 12:05:00"):
            # assert is_in_funding_window() is False
            pass
        pytest.skip("Implement after wiring funding window check")


# =============================================================================
# Bug #5 — No startup reconciliation with HL state
# Severity: 🔴 BLOCKER — position lost on restart = no SL = unbounded loss
# =============================================================================
class TestBug05StartupReconciliation:
    def test_startup_loads_open_positions_from_hl(self, mock_hl_info):
        """On startup, fetch user_state and rebuild Position objects."""
        mock_hl_info.user_state.return_value = {
            "marginSummary": {"accountValue": "1000.0"},
            "assetPositions": [{
                "position": {
                    "coin": "BTC", "szi": "0.003", "entryPx": "65000.0",
                    "leverage": {"value": 10, "type": "cross"},
                    "unrealizedPnl": "5.0", "marginUsed": "20.0",
                }
            }],
            "withdrawable": "950.0",
        }
        # from execution.order_manager import OrderManager
        # om = OrderManager(...)
        # om.reconcile_on_startup()
        # assert "BTC" in om.positions
        pytest.skip("Implement after wiring OrderManager.reconcile_on_startup()")


# =============================================================================
# Bug #6 — _on_position_close doesn't distinguish partial vs full close
# Severity: 🟡 MAJOR — DB rows duplicated or PnL miscalculated
# =============================================================================
class TestBug06PartialVsFullClose:
    def test_partial_close_does_not_log_trade_row(self):
        pytest.skip("Implement after wiring _on_position_close")

    def test_full_close_logs_exactly_one_trade_row(self):
        pytest.skip("Implement after wiring _on_position_close")


# =============================================================================
# Bugs #7–#12 — Add tests for each as they are documented
# =============================================================================


# =============================================================================
# Bug #13 — schedule.run_pending() blocks the main async loop
# Severity: 🟡 MAJOR — bot misses scan cycles when weekly review runs
# =============================================================================
class TestBug13ScheduleNonBlocking:
    @pytest.mark.asyncio
    async def test_schedule_runs_in_thread_or_async(self):
        """Scheduled jobs must not block the main asyncio loop > 100ms."""
        pytest.skip("Implement after wiring main loop")


# =============================================================================
# Bug #14 — Position dataclass not tolerant to extra/missing fields
# Severity: 🟡 MAJOR — rolling upgrade breaks state file
# =============================================================================
class TestBug14PositionFieldTolerance:
    def test_position_loads_with_extra_fields(self, make_position):
        data = make_position(unknown_future_field="xyz")
        from execution.order_manager import Position
        pos = Position.from_dict(data)
        assert pos.asset == "BTC"

    def test_position_loads_with_missing_optional_field(self, make_position):
        data = make_position()
        del data["tp_hit_count"]
        from execution.order_manager import Position
        pos = Position.from_dict(data)
        assert pos.tp_hit_count == 0


# =============================================================================
# Bug #15 — Telegram Markdown special chars cause send failure
# Severity: 🟢 MINOR — alerts silently dropped
# =============================================================================
class TestBug15TelegramEscaping:
    def test_telegram_escapes_underscores_and_brackets(self, monkeypatch):
        captured = []

        def fake_post(text, parse_mode="HTML"):
            captured.append({"text": text, "parse_mode": parse_mode})
            return True

        monkeypatch.setattr("notifications.telegram._post", fake_post)

        from notifications.telegram import send_alert
        send_alert("Symbol BTC_PERP hit *target* [TP1]")

        assert captured, "No message was sent"
        last = captured[-1]
        # HTML mode is used — underscores and brackets pass through safely
        assert last["parse_mode"] == "HTML"
        assert "BTC_PERP" in last["text"]
        assert "[TP1]" in last["text"]


# =============================================================================
# Bug #16 — Outdated Anthropic model name
# Severity: 🟢 MINOR — weekly review fails 404
# =============================================================================
class TestBug16AnthropicModel:
    def test_model_string_is_current(self):
        from self_review.claude_review import MODEL_NAME
        assert MODEL_NAME in {"claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}


# =============================================================================
# Bug #17 — JSON fence parsing breaks on ```json prefix
# Severity: 🟡 MAJOR — review verdict misparsed → may auto-pause incorrectly
# =============================================================================
class TestBug17JsonFenceParsing:
    @pytest.mark.parametrize("raw", [
        '```json\n{"verdict":"ok"}\n```',
        '```\n{"verdict":"ok"}\n```',
        '{"verdict":"ok"}',
        'Here is the analysis:\n```json\n{"verdict":"ok"}\n```\n',
    ])
    def test_fence_stripped(self, raw):
        from self_review.claude_review import parse_review_response
        result = json.loads(parse_review_response(raw))
        assert result["verdict"] == "ok"


# =============================================================================
# Bug #18 — last_signal_time not in Position.to_dict()
# Severity: 🟢 MINOR — anti-spam filter resets on restart
# =============================================================================
class TestBug18LastSignalTimePersisted:
    def test_to_dict_includes_last_signal_time(self, make_position):
        pos = make_position(last_signal_time="2026-05-07T11:00:00+00:00")
        from execution.order_manager import Position
        data = Position.from_dict(pos).to_dict()
        assert "last_signal_time" in data


# =============================================================================
# Bug #19 — Single FX source (USD/IDR) — no fallback
# Severity: 🟢 MINOR — tax CSV has missing rows when source down
# =============================================================================
class TestBug19FxFallback:
    def test_fx_falls_back_to_secondary_source(self, monkeypatch):
        import requests
        from unittest.mock import MagicMock

        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json.return_value = {"rates": {"IDR": 16000.0}}

        mock_get = MagicMock(side_effect=[
            requests.exceptions.ConnectionError("primary down"),
            ok_resp,
        ])
        monkeypatch.setattr("monitoring.tax_logger.requests.get", mock_get)

        import monitoring.tax_logger as tl
        tl.USD_IDR_CACHE["fetched_at"] = 0  # force fresh fetch

        from monitoring.tax_logger import _fetch_usd_idr
        rate = _fetch_usd_idr()

        assert rate == 16000.0
        assert mock_get.call_count == 2  # primary failed, secondary succeeded


# =============================================================================
# Bug #20 — tp_hit_count timing: incremented before order confirmed
# Severity: 🟡 MAJOR — failed TP order → state thinks TP hit → SL never triggers
# =============================================================================
class TestBug20TpHitCountTiming:
    def test_tp_hit_count_only_after_fill_confirmed(self, monkeypatch, mock_hl_exchange, make_position):
        # Bug #20 only exists on the live-fill path; force DRY_RUN off so the
        # exchange-rejection branch actually runs.
        monkeypatch.setattr("execution.order_manager.DRY_RUN", False)

        from execution.order_manager import OrderManager, Position

        mock_hl_exchange.market_close.return_value = {"status": "err", "response": "rejected"}

        om = object.__new__(OrderManager)
        om.exchange = mock_hl_exchange
        om._szDecimals_cache = {"BTC": 4}

        pos = Position.from_dict(make_position())
        om.positions = {pos.asset: pos}

        om._execute_partial_tp(pos.asset, 0.5, 3.0, pos.entry_price * 1.03, is_last_tp=False)

        assert pos.tp_hit_count == 0  # fill rejected — count must not change


# =============================================================================
# Bug #21 — (placeholder)
# =============================================================================


# =============================================================================
# Bug #22 — No DB schema migration mechanism
# Severity: 🟡 MAJOR — adding a column on upgrade breaks startup
# =============================================================================
class TestBug22DbMigration:
    def test_migration_runs_on_old_schema(self, tmp_path):
        import sqlite3
        db = tmp_path / "old.db"
        # Create v0 schema (missing later columns)
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                entry_price REAL
            )
        """)
        conn.commit()
        conn.close()

        from monitoring.trade_logger import migrate_schema
        migrate_schema(str(db))
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        conn.close()
        assert "tp1_hit" in cols
        assert "close_reason" in cols