import json
import pytest

pytestmark = [pytest.mark.regression, pytest.mark.blocker]


# =============================================================================
# Bug #1 — Scanner uses iloc[-1] (current candle) instead of iloc[-2] (closed)
# Severity: 🔴 BLOCKER — look-ahead bias inflates backtest, wrong live signals
# =============================================================================
class TestBug01ScannerLookahead:
    def test_scan_source_does_not_index_forming_candle(self):
        """Source-level guard: MarketScanner.scan must use iloc[-2], never iloc[-1]
        as the signal row. Strip Python line comments before matching so the
        bug-documenting comment in scanner.py doesn't false-positive.
        """
        import inspect

        from strategy import scanner as scanner_mod

        raw = inspect.getsource(scanner_mod.MarketScanner.scan)
        code = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())

        assert "iloc[-2]" in code, (
            "Bug #1 REGRESSION: scan() must read the closed candle via iloc[-2]"
        )
        assert "iloc[-1]" not in code, (
            "Bug #1 REGRESSION: scan() must not index the forming candle (iloc[-1])"
        )

    def test_scanner_uses_closed_candle_only(
        self, oversold_setup_df, mock_hl_info, monkeypatch
    ):
        """Behavioral guard: poison iloc[-1] with a sentinel and force
        is_entry_signal to True. The emitted signal must reflect iloc[-2].close,
        proving scan() never read the forming candle.
        """
        from strategy import scanner as scanner_mod

        df = oversold_setup_df.copy()
        sentinel_price = 1.0
        df.iloc[-1, df.columns.get_loc("close")] = sentinel_price
        expected_price = float(df["close"].iloc[-2])

        monkeypatch.setattr(scanner_mod, "is_entry_signal", lambda row: True)

        sc = scanner_mod.MarketScanner.__new__(scanner_mod.MarketScanner)
        sc.info = mock_hl_info
        sc._meta_cache = None
        sc._meta_cache_time = 0
        sc._daily_candles_cache = {}

        monkeypatch.setattr(sc, "_passes_volume_filter", lambda ctx: True)
        monkeypatch.setattr(sc, "_passes_drop_filter", lambda asset: (True, -10.0))
        monkeypatch.setattr(sc, "_passes_funding_filter", lambda ctx: True)
        monkeypatch.setattr(sc, "_fetch_candles_df", lambda asset: df)

        signals = sc.scan()

        assert signals, "Expected scanner to emit at least one signal"
        assert signals[0].price != sentinel_price, (
            f"Bug #1 REGRESSION: signal.price={signals[0].price} matches the "
            f"forming-candle sentinel — scanner used iloc[-1]"
        )
        assert signals[0].price == pytest.approx(expected_price), (
            f"signal.price={signals[0].price} should equal "
            f"iloc[-2].close={expected_price}"
        )


# =============================================================================
# Bug #2 — TP2 sizing multiplies entry_size_coin × sell_pct (wrong)
# Severity: 🔴 BLOCKER — partial close size incorrect, leaves dust positions
# =============================================================================
class TestBug02Tp2Sizing:
    def test_tp2_closes_remaining_size_after_tp1(
        self, monkeypatch, mock_hl_exchange, make_position
    ):
        """Bug #2: TP2 must close pos.remaining_size_coin × sell_pct, NOT
        entry_size_coin × sell_pct. After TP1 sold 60% of entry, remaining = 40%.
        TP2 with sell_pct=1.0 must close that 40%, never the original 100%
        (which would over-sell by 2.5x).

        Use a market_close → err short-circuit so the function bails after the
        size argument is committed to the exchange call, sidestepping the
        downstream HealthMonitor/WithdrawManager imports.
        """
        monkeypatch.setattr("execution.order_manager.DRY_RUN", False)

        from execution.order_manager import OrderManager, Position

        mock_hl_exchange.market_close.return_value = {
            "status": "err", "response": "rejected"
        }

        om = object.__new__(OrderManager)
        om.exchange = mock_hl_exchange
        om._szDecimals_cache = {"BTC": 5}

        entry_size = 0.003076
        tp1_sold = entry_size * 0.60
        remaining = entry_size - tp1_sold

        pos = Position.from_dict(make_position(
            entry_size_coin=entry_size,
            remaining_size_coin=remaining,
            tp_hit_count=1,
            tp_levels_remaining=[[20.0, 1.0]],  # only TP2 left, sell_pct=1.0
        ))
        om.positions = {pos.asset: pos}

        om._execute_partial_tp(
            pos.asset, sell_pct=1.0, tp_label=20.0,
            current_price=pos.entry_price * 1.20, is_last_tp=True,
        )

        mock_hl_exchange.market_close.assert_called_once()
        call = mock_hl_exchange.market_close.call_args
        actual_sz = call.kwargs.get("sz")

        expected = round(remaining, 5)
        assert actual_sz == pytest.approx(expected, abs=1e-6), (
            f"Bug #2 REGRESSION: TP2 closed sz={actual_sz}, expected {expected}. "
            f"Buggy code would close entry_size×sell_pct={entry_size}."
        )
        assert actual_sz != pytest.approx(entry_size, abs=1e-6), (
            "Bug #2 REGRESSION: TP2 closed the full original entry size"
        )


# =============================================================================
# Bug #3 — SL handler doesn't fully close position
# Severity: 🔴 BLOCKER — risk management broken
# =============================================================================
class TestBug03SlFullClose:
    def test_sl_closes_entire_remaining_size(
        self, monkeypatch, mock_hl_exchange, make_position, tmp_path
    ):
        """Bug #3: When SL fires after TP1 has already trimmed the position, the
        handler must close the actual remaining size — never the original entry
        (over-sells) and never a hardcoded fraction. Two implementations are
        acceptable: (a) market_close(asset) with no sz so HL closes whatever
        remains, or (b) market_close(asset, sz=remaining). Failing case:
        market_close(asset, sz=entry_size_coin).
        """
        import time

        monkeypatch.setattr("execution.order_manager.DRY_RUN", False)

        from execution.order_manager import OrderManager, Position

        om = object.__new__(OrderManager)
        om.exchange = mock_hl_exchange
        om._szDecimals_cache = {"BTC": 5}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"
        # Sidestep HealthMonitor/WithdrawManager side effects on full close.
        monkeypatch.setattr(om, "_on_position_close_full", lambda *a, **kw: None)

        entry_size = 1.0
        remaining = 0.4  # post-TP1 (60% sold)
        pos = Position.from_dict(make_position(
            asset="BTC",
            entry_price=100.0,
            entry_size_coin=entry_size,
            remaining_size_coin=remaining,
            entry_time_ms=int(time.time() * 1000) - 60_000,  # 1 min ago — avoid max-hold
            tp_hit_count=1,
            tp_levels_remaining=[[20.0, 1.0]],
            initial_sl_price=95.0,
            current_sl_price=95.0,
            sl_oid=12345,
        ))
        om.positions = {pos.asset: pos}

        # Drop price below SL → SL path fires
        om.manage_open_positions(current_prices={"BTC": 94.0})

        # Resting SL order cancelled first
        mock_hl_exchange.cancel.assert_called_once_with("BTC", 12345)

        # market_close called exactly once
        mock_hl_exchange.market_close.assert_called_once()
        call = mock_hl_exchange.market_close.call_args
        sz = call.kwargs.get("sz")
        if sz is None and len(call.args) >= 2:
            sz = call.args[1]

        if sz is not None:
            assert sz == pytest.approx(remaining, abs=1e-5), (
                f"Bug #3 REGRESSION: SL closed sz={sz}, expected ~{remaining}. "
                f"Closing entry-size ({entry_size}) would over-sell by 2.5x."
            )
            assert sz != pytest.approx(entry_size, abs=1e-5), (
                "Bug #3 REGRESSION: SL closed the full original entry size"
            )

        # Position must be removed from local state
        assert "BTC" not in om.positions


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
    def test_reconcile_drops_stale_local_positions(
        self, monkeypatch, mock_hl_info, mock_hl_exchange, make_position, tmp_path
    ):
        """Direction 1 — stale local: a position recorded locally that was
        already closed on the exchange (closed manually while bot offline) must
        be removed during reconciliation so the bot stops managing a phantom.
        """
        monkeypatch.setattr("execution.order_manager.DRY_RUN", False)

        from execution.order_manager import OrderManager, Position

        om = object.__new__(OrderManager)
        om.info = mock_hl_info
        om.exchange = mock_hl_exchange
        om._szDecimals_cache = {}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"

        mock_hl_info.user_state.return_value = {
            "marginSummary": {"accountValue": "1000.0"},
            "assetPositions": [],
            "withdrawable": "1000.0",
        }
        om.positions = {"BTC": Position.from_dict(make_position())}

        om._reconcile_with_exchange()

        assert "BTC" not in om.positions, (
            "Bug #5 REGRESSION: stale local position not dropped during reconcile"
        )

    def test_startup_loads_open_positions_from_hl(
        self, monkeypatch, mock_hl_info, mock_hl_exchange, tmp_path
    ):
        """Direction 2 — exchange-only import: a position open on the exchange
        but absent from local state (bot crashed/restarted) must be imported so
        a stop-loss can be placed. Without this, the position is unmanaged →
        no SL → unbounded loss (original BLOCKER scenario).
        """
        monkeypatch.setattr("execution.order_manager.DRY_RUN", False)

        from execution.order_manager import OrderManager

        om = object.__new__(OrderManager)
        om.info = mock_hl_info
        om.exchange = mock_hl_exchange
        om._szDecimals_cache = {}
        om._cooldown_until = {}
        om.STATE_FILE = tmp_path / "positions.json"
        om.positions = {}

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

        om._reconcile_with_exchange()

        assert "BTC" in om.positions, (
            "Bug #5 REGRESSION: exchange position not imported into local state"
        )
        imported = om.positions["BTC"]
        assert imported.entry_price == pytest.approx(65000.0)
        assert imported.remaining_size_coin == pytest.approx(0.003)
        # SL must be set so the position is managed; placement attempt happens
        # in the SL re-placement loop downstream of import.
        assert imported.current_sl_price > 0, (
            "Bug #5 REGRESSION: imported position has no SL price configured"
        )
        # Exchange.order should have been called to place the SL
        assert mock_hl_exchange.order.called, (
            "Bug #5 REGRESSION: no SL placement attempted for imported position"
        )


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