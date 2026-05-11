"""
Unit tests — monitoring/health.py

Test IDs: H1–H8 from solvira_stress_test_master.md §3.9.
H8: Bug #18 regression — last_signal_time visible in health endpoint.
"""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def fresh_monitor(monkeypatch):
    """Reset the singleton and stub the Telegram side-effect inside _halt."""
    from monitoring import health as health_mod
    health_mod.HealthMonitor._instance = None
    monkeypatch.setattr(health_mod, "notify_circuit_breaker", lambda r: None)
    yield health_mod.HealthMonitor
    health_mod.HealthMonitor._instance = None


# -----------------------------------------------------------------------------
# H1 — Singleton pattern (same instance returned twice)
# -----------------------------------------------------------------------------
class TestSingleton:
    def test_two_constructions_same_instance(self, fresh_monitor):
        h1 = fresh_monitor()
        h2 = fresh_monitor()
        assert h1 is h2


# -----------------------------------------------------------------------------
# H2 — heartbeat() updates last_loop_time (UptimeRobot freshness signal)
# -----------------------------------------------------------------------------
class TestHeartbeat:
    pytestmark = pytest.mark.blocker

    def test_heartbeat_updates_last_alive(self, fresh_monitor, freeze_clock):
        h = fresh_monitor()
        assert h.last_loop_time is None
        h.heartbeat()
        assert h.last_loop_time is not None


# -----------------------------------------------------------------------------
# H3 — 5 consecutive errors trip the circuit breaker
# -----------------------------------------------------------------------------
class TestCircuitBreaker:
    pytestmark = pytest.mark.blocker

    def test_5_errors_triggers_halt(self, fresh_monitor):
        h = fresh_monitor()
        for _ in range(5):
            h.on_error("api")
        assert h.is_halted is True
        assert h.halt_reason and "api" in h.halt_reason

    def test_on_success_resets_error_counter(self, fresh_monitor):
        h = fresh_monitor()
        for _ in range(3):
            h.on_error("api")
        h.on_success()
        assert h.consecutive_errors == 0
        assert h.is_halted is False


# -----------------------------------------------------------------------------
# H4 — Daily counter resets at UTC midnight
# -----------------------------------------------------------------------------
class TestDailyCounterReset:
    def test_counters_reset_at_midnight_utc(self, fresh_monitor, freeze_clock):
        freeze_clock.move_to("2026-05-07 23:59:30")
        h = fresh_monitor()
        h.daily_pnl_usd = -10.0
        h.consecutive_losses = 3

        freeze_clock.move_to("2026-05-08 00:00:30")
        h.heartbeat()

        assert h.daily_pnl_usd == 0.0
        assert h.consecutive_losses == 0


# -----------------------------------------------------------------------------
# H7 — to_dict() output is JSON-serializable
# -----------------------------------------------------------------------------
class TestToDictSerializable:
    def test_to_dict_json_dumps_ok(self, fresh_monitor):
        import json
        h = fresh_monitor()
        json.dumps(h.to_dict())  # raises if not serializable


# -----------------------------------------------------------------------------
# H8 — 🟢 Bug #18 regression: last_signal_time visible in health payload
# -----------------------------------------------------------------------------
class TestBug18LastSignalTimeVisible:
    pytestmark = pytest.mark.regression

    def test_to_dict_includes_last_signal_time(self, fresh_monitor):
        h = fresh_monitor()
        d = h.to_dict()
        assert "last_signal_time" in d or "seconds_since_last_signal" in d


# -----------------------------------------------------------------------------
# H5 / H6 — HTTP endpoint freshness + concurrency tests
# Deferred: require a live HTTPServer thread, out of scope for this unit file.
# -----------------------------------------------------------------------------
class TestHttp503OnStale:
    pytestmark = pytest.mark.blocker

    def test_stale_heartbeat_returns_503(self, freeze_clock):
        pytest.skip("Requires live HTTP server — deferred to integration tier")


class TestHttpThreadSafe:
    def test_concurrent_requests_no_corruption(self):
        pytest.skip("Requires live HTTP server — deferred to integration tier")
