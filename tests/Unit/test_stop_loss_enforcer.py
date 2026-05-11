"""
Unit tests — monitoring/stop_loss_enforcer.py

Test IDs: SL1–SL8 from solvira_stress_test_master.md §3.10.
Boundary tests around 90-day window and -$200 EVALUATION_LOSS_THRESHOLD_USD.
"""

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.blocker]


@pytest.fixture
def enforcer_env(monkeypatch, tmp_path):
    """Redirect the module-level STATE_FILE to tmp and stub out side effects."""
    from monitoring import stop_loss_enforcer as sle_mod

    state_file = tmp_path / "stop_loss_enforcer.json"
    monkeypatch.setattr(sle_mod, "STATE_FILE", state_file)
    monkeypatch.setattr(sle_mod, "notify_circuit_breaker", lambda r: None)
    # Default: assume zero realized PnL. Individual tests can override.
    monkeypatch.setattr(sle_mod, "get_total_pnl_since", lambda _start_iso: 0.0)
    return sle_mod, state_file


def _set_pnl(monkeypatch, sle_mod, value):
    monkeypatch.setattr(sle_mod, "get_total_pnl_since", lambda _s: value)


# -----------------------------------------------------------------------------
# SL1 — First call creates a persisted state file
# -----------------------------------------------------------------------------
class TestStateInit:
    def test_first_call_creates_state(self, enforcer_env):
        sle_mod, state_file = enforcer_env
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)
        assert state_file.exists(), "State file was not created on init"
        assert sle.state["is_halted"] is False
        assert sle.state["start_date"]  # populated


# -----------------------------------------------------------------------------
# SL2 — <90 days elapsed → no halt regardless of loss
# -----------------------------------------------------------------------------
class TestUnder90DaysNoHalt:
    def test_no_halt_before_90d(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        # Massive loss, but only 30 days in — must NOT halt yet
        freeze_clock.move_to("2026-01-31 00:00:00")
        _set_pnl(monkeypatch, sle_mod, -500.0)

        ok, reason = sle.check()
        assert ok is True
        assert reason is None
        assert sle.state["is_halted"] is False


# -----------------------------------------------------------------------------
# SL3 — 90 days, -$199 → no halt (loss below threshold)
# -----------------------------------------------------------------------------
class TestBoundary199NoHalt:
    def test_minus_199_no_halt(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        freeze_clock.move_to("2026-04-02 00:00:00")  # 91 days later
        _set_pnl(monkeypatch, sle_mod, -199.0)

        ok, _ = sle.check()
        assert ok is True
        assert sle.state["is_halted"] is False


# -----------------------------------------------------------------------------
# SL4 — 90 days, exactly -$200 → halt (threshold inclusive)
# -----------------------------------------------------------------------------
class TestExactThresholdHalt:
    def test_minus_200_halts(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        freeze_clock.move_to("2026-04-02 00:00:00")
        _set_pnl(monkeypatch, sle_mod, -200.0)

        ok, reason = sle.check()
        assert ok is False
        assert reason is not None
        assert sle.state["is_halted"] is True


# -----------------------------------------------------------------------------
# SL5 — 90 days, -$201 → halt (over threshold)
# -----------------------------------------------------------------------------
class TestOverThresholdHalt:
    def test_minus_201_halts(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        freeze_clock.move_to("2026-04-02 00:00:00")
        _set_pnl(monkeypatch, sle_mod, -201.0)

        ok, _ = sle.check()
        assert ok is False
        assert sle.state["is_halted"] is True


# -----------------------------------------------------------------------------
# SL6 — Halt persists across subsequent check()s (no auto-resume)
# -----------------------------------------------------------------------------
class TestHaltPersists:
    def test_halt_does_not_auto_resume(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        # Trip the halt
        freeze_clock.move_to("2026-04-02 00:00:00")
        _set_pnl(monkeypatch, sle_mod, -250.0)
        sle.check()
        assert sle.state["is_halted"] is True

        # 10 days later, even with profit, halt must persist until manual_resume
        freeze_clock.move_to("2026-04-12 00:00:00")
        _set_pnl(monkeypatch, sle_mod, 100.0)
        ok, _ = sle.check()
        assert ok is False
        assert sle.state["is_halted"] is True


# -----------------------------------------------------------------------------
# SL7 — manual_resume() clears halt and (optionally) restarts the window
# -----------------------------------------------------------------------------
class TestManualResume:
    def test_manual_resume_starts_new_cycle(self, enforcer_env, freeze_clock, monkeypatch):
        sle_mod, _ = enforcer_env
        freeze_clock.move_to("2026-01-01 00:00:00")
        sle = sle_mod.StopLossEnforcer(initial_capital=500.0)

        freeze_clock.move_to("2026-04-02 00:00:00")
        _set_pnl(monkeypatch, sle_mod, -250.0)
        sle.check()
        assert sle.state["is_halted"] is True

        freeze_clock.move_to("2026-05-01 00:00:00")
        sle.manual_resume(reset_period=True)
        assert sle.state["is_halted"] is False
        # start_date should be reset to "now"
        assert "2026-05-01" in sle.state["start_date"]


# -----------------------------------------------------------------------------
# SL8 — Corrupt state file must NOT silently bypass a prior halt
# -----------------------------------------------------------------------------
class TestCorruptStateNoBypass:
    def test_corrupt_state_does_not_bypass_halt(self, enforcer_env):
        """Defensive recovery — if the JSON is unreadable, the enforcer should
        either raise (loud failure) or default to a non-trading state. It must
        never silently treat the bot as healthy and resume trading.
        """
        sle_mod, state_file = enforcer_env
        state_file.write_text("{not valid json")

        with pytest.raises((ValueError, Exception)):
            sle_mod.StopLossEnforcer(initial_capital=500.0)
