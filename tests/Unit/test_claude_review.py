"""
Unit tests — self_review/claude_review.py

Test IDs: CR1–CR6 from solvira_stress_test_master.md §3.12.
CR1/CR2: Bug #16 (model name) and Bug #17 (JSON fence parsing) regressions.
"""

import inspect
import json

import pytest

pytestmark = pytest.mark.unit


# -----------------------------------------------------------------------------
# CR1 — 🟢 Bug #16 regression: latest Claude model string
# -----------------------------------------------------------------------------
class TestBug16LatestClaudeModel:
    pytestmark = pytest.mark.regression

    def test_uses_latest_claude_model(self):
        try:
            from self_review import claude_review
        except ImportError:
            pytest.skip("claude_review not importable in test env")
        src = inspect.getsource(claude_review)
        # Reject deprecated/old models, require a current one
        assert (
            "claude-opus-4-7" in src
            or "claude-sonnet-4-6" in src
            or "claude-haiku-4-5-20251001" in src
        ), "Update model string to a current Claude 4.x model"


# -----------------------------------------------------------------------------
# CR2 — 🟡 Bug #17 regression: JSON fence stripping
# -----------------------------------------------------------------------------
class TestBug17JsonFenceStripping:
    pytestmark = pytest.mark.regression

    @pytest.mark.parametrize("raw", [
        '```json\n{"verdict":"ok"}\n```',
        '```\n{"verdict":"ok"}\n```',
        '{"verdict":"ok"}',
        'Here is the analysis:\n```json\n{"verdict":"ok"}\n```\n',
    ])
    def test_strips_markdown_fences(self, raw):
        # from self_review.claude_review import parse_review_response
        # parsed = json.loads(parse_review_response(raw))
        # assert parsed["verdict"] == "ok"
        pytest.skip("Wire parse_review_response()")


# -----------------------------------------------------------------------------
# CR3 — Hard-limit suggestion is filtered out (sanity check)
# -----------------------------------------------------------------------------
class TestHardLimitFiltered:
    pytestmark = pytest.mark.blocker

    def test_review_cannot_change_hard_limits(self):
        # from self_review.claude_review import _sanity_check
        # suggestion = {"set_max_position_size_usd": 9999}
        # assert _sanity_check(suggestion) is False
        pytest.skip("Wire claude_review._sanity_check()")


# -----------------------------------------------------------------------------
# CR4 — "annualized return" warning surfaced as risk_alert
# -----------------------------------------------------------------------------
class TestAnnualizedReturnWarning:
    def test_annualized_return_flagged(self, mock_anthropic):
        pytest.skip("Wire risk_alert detection")


# -----------------------------------------------------------------------------
# CR5 — Anthropic API timeout → log + Telegram, no crash
# -----------------------------------------------------------------------------
class TestApiTimeoutHandling:
    def test_api_timeout_logged_and_skipped(self, mock_anthropic, mock_telegram):
        from anthropic import APITimeoutError
        from unittest.mock import MagicMock
        mock_anthropic.messages.create.side_effect = APITimeoutError(
            request=MagicMock()
        )
        # run_weekly_review() should NOT raise
        pytest.skip("Wire claude_review.run_weekly_review() error handling")


# -----------------------------------------------------------------------------
# CR6 — <10 trades: review still runs (edge case)
# -----------------------------------------------------------------------------
class TestSmallSampleEdge:
    def test_review_runs_with_few_trades(self, mock_anthropic):
        pytest.skip("Wire claude_review with sparse data")
