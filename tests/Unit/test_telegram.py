"""
Unit tests — notifications/telegram.py

Test IDs: T1–T9 from solvira_stress_test_master.md §3.7.
T7: Bug #15 regression — special chars in asset names (Markdown→HTML fix).
"""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def reset_telegram_state():
    """Clear module-level throttle state between tests."""
    from notifications import telegram as tg_mod
    tg_mod._last_sent.clear()
    yield
    tg_mod._last_sent.clear()


@pytest.fixture
def mock_post(monkeypatch, reset_telegram_state):
    """Replace _post with a recorder; return the captured call list."""
    captured = []

    def _fake_post(text, parse_mode="HTML"):
        captured.append({"text": text, "parse_mode": parse_mode})
        return True

    from notifications import telegram as tg_mod
    monkeypatch.setattr(tg_mod, "_post", _fake_post)
    monkeypatch.setattr(tg_mod, "TOKEN", "fake-token")
    monkeypatch.setattr(tg_mod, "CHAT_ID", "fake-chat")
    return captured


# -----------------------------------------------------------------------------
# T1 — send() happy path (POST sent)
# -----------------------------------------------------------------------------
class TestSendHappyPath:
    def test_send_posts_to_telegram(self, mock_post):
        from notifications.telegram import send
        ok = send("hello", force=True)
        assert ok is True
        assert len(mock_post) == 1
        assert "hello" in mock_post[0]["text"]


# -----------------------------------------------------------------------------
# T2 — Throttling active → message skipped
# -----------------------------------------------------------------------------
class TestThrottlingActive:
    def test_throttle_skips_message(self, mock_post):
        from notifications.telegram import send
        # general window = 3600s; second send within that window is throttled
        assert send("a", "general") is True
        assert send("b", "general") is False
        assert len(mock_post) == 1


# -----------------------------------------------------------------------------
# T3 — force=True bypasses throttle
# -----------------------------------------------------------------------------
class TestForceBypassThrottle:
    def test_force_always_sends(self, mock_post):
        from notifications.telegram import send
        send("first", "general")
        send("urgent", "general", force=True)
        assert len(mock_post) == 2


# -----------------------------------------------------------------------------
# T4 — TOKEN/CHAT_ID missing → return False (no POST)
# -----------------------------------------------------------------------------
class TestMissingCredentials:
    def test_missing_token_returns_false(self, monkeypatch, reset_telegram_state):
        from notifications import telegram as tg_mod
        monkeypatch.setattr(tg_mod, "TOKEN", None)
        monkeypatch.setattr(tg_mod, "CHAT_ID", "fake")
        assert tg_mod._post("anything") is False


# -----------------------------------------------------------------------------
# T5 — API 429 rate limit handled (no crash, returns False)
# -----------------------------------------------------------------------------
class TestRateLimit:
    def test_429_handled_no_crash(self, monkeypatch, reset_telegram_state):
        from unittest.mock import MagicMock
        from notifications import telegram as tg_mod
        monkeypatch.setattr(tg_mod, "TOKEN", "fake-token")
        monkeypatch.setattr(tg_mod, "CHAT_ID", "fake-chat")
        fake_response = MagicMock(status_code=429, text="too many requests")
        monkeypatch.setattr(
            tg_mod.requests, "post", MagicMock(return_value=fake_response)
        )
        assert tg_mod._post("x") is False


# -----------------------------------------------------------------------------
# T6 — API timeout → no crash
# -----------------------------------------------------------------------------
class TestApiTimeout:
    def test_timeout_no_crash(self, monkeypatch, reset_telegram_state):
        import requests
        from notifications import telegram as tg_mod
        monkeypatch.setattr(tg_mod, "TOKEN", "fake-token")
        monkeypatch.setattr(tg_mod, "CHAT_ID", "fake-chat")

        def _raise(*args, **kwargs):
            raise requests.exceptions.Timeout("timeout")

        monkeypatch.setattr(tg_mod.requests, "post", _raise)
        assert tg_mod._post("x") is False


# -----------------------------------------------------------------------------
# T7 — 🔴 Bug #15 regression: special chars in asset names must not break
# -----------------------------------------------------------------------------
class TestBug15TelegramMarkdown:
    pytestmark = [pytest.mark.blocker, pytest.mark.regression]

    def test_special_chars_in_asset_name(self, mock_post):
        from notifications.telegram import notify_daily_summary
        stats = {
            "date": "2025-01-01", "total_trades": 1, "wins": 1, "losses": 0,
            "win_rate": 1.0, "pnl_usd": 5.0, "pnl_pct": 1.0,
            "capital": 500, "usdt_wallet": 0,
            "top_trade": {"asset": "k_PEPE*test", "pnl_pct": 5.0},
        }
        # Must not raise — HTML mode (Bug #15 fix) doesn't choke on _ * [ ]
        notify_daily_summary(stats)
        assert len(mock_post) == 1
        sent = mock_post[0]
        assert sent["parse_mode"] == "HTML"
        # The literal asset chars survive HTML escaping
        assert "k_PEPE" in sent["text"]


# -----------------------------------------------------------------------------
# T8 — Daily summary renders cleanly
# -----------------------------------------------------------------------------
class TestDailySummaryFormat:
    def test_daily_summary_renders(self, mock_post):
        from notifications.telegram import notify_daily_summary
        stats = {
            "date": "2025-05-08", "total_trades": 3, "wins": 2, "losses": 1,
            "win_rate": 0.667, "pnl_usd": 12.50, "pnl_pct": 2.5,
            "capital": 500.0, "usdt_wallet": 25.0,
        }
        notify_daily_summary(stats)
        assert len(mock_post) == 1
        text = mock_post[0]["text"]
        assert "Daily Summary" in text
        assert "12.50" in text or "$12.50" in text


# -----------------------------------------------------------------------------
# T9 — notify_critical_error truncates the error payload to 500 chars
# -----------------------------------------------------------------------------
class TestCriticalErrorTruncation:
    def test_long_error_truncated(self, mock_post):
        from notifications.telegram import notify_critical_error
        notify_critical_error("x" * 5000, "scope")
        assert len(mock_post) == 1
        text = mock_post[0]["text"]
        # Source truncates the error body to 500 chars before wrapping
        assert text.count("x") <= 500
