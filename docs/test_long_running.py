"""
Endurance / Long-running tests — Tier 4.

Test IDs: EN1–EN6 from master guide.
These tests are SLOW (minutes to hours). Run separately:
  pytest tests/endurance/ -v -m endurance --timeout=86400

Schedule:
  • EN1: 1000 scan cycles    (~5 min)
  • EN2: 24h main loop       (run overnight)
  • EN3: 7-day testnet DRY   (manual, see Phase 1)
  • EN4: tracemalloc leak    (~10 min)
  • EN5: mprof RSS curve     (~1 hour)
  • EN6: fd leak monitor     (~30 min)
"""

import time
import tracemalloc
from pathlib import Path

import pytest

pytestmark = [pytest.mark.endurance, pytest.mark.slow]


# =============================================================================
# EN1 — 1000 scan cycles, count exceptions
# =============================================================================
class TestScanCycles:
    def test_1000_cycles_no_exceptions(self, mock_hl_info):
        errors = []
        # from strategy.scanner import Scanner
        # scanner = Scanner(info=mock_hl_info, ...)
        for i in range(1000):
            try:
                # scanner.scan(["BTC", "ETH", "SOL", "ARB"])
                pass
            except Exception as e:
                errors.append((i, type(e).__name__, str(e)))
        assert len(errors) == 0, f"Got {len(errors)} errors in 1000 cycles: {errors[:3]}"
        pytest.skip("Wire scanner")


# =============================================================================
# EN2 — 24h main loop simulation (run overnight)
# =============================================================================
class TestMainLoop24h:
    @pytest.mark.skip_ci
    def test_24h_loop_simulated(self):
        """Run the actual main.py for 24h with mocked HL. Verify:
        - No memory growth >50MB
        - No fd growth >10
        - No unhandled exceptions in log
        - All scheduled jobs (snapshot, eval, withdraw) fired
        """
        pytest.skip("Run via: timeout 86400 python main.py --test-mode")


# =============================================================================
# EN4 — Memory leak detection with tracemalloc
# =============================================================================
class TestMemoryLeakLong:
    def test_tracemalloc_diff_under_10mb_after_5000_cycles(self, mock_hl_info):
        tracemalloc.start()
        # warmup
        for _ in range(100):
            # scanner.scan(...)
            pass
        snap1 = tracemalloc.take_snapshot()

        for _ in range(5000):
            # scanner.scan(...)
            pass

        snap2 = tracemalloc.take_snapshot()
        stats = snap2.compare_to(snap1, "filename")
        total_diff = sum(s.size_diff for s in stats)

        print(f"\nMemory growth: {total_diff/1e6:.2f} MB")
        for s in stats[:5]:
            print(f"  {s}")

        assert total_diff < 10 * 1024 * 1024, "Memory leak detected"
        pytest.skip("Wire scanner")


# =============================================================================
# EN5 — RSS curve via mprof (manual run)
# =============================================================================
# Run manually:
#   mprof run pytest tests/endurance/test_long_running.py::TestRssCurve -v
#   mprof plot
#
# Expected: flat RSS curve (no upward drift)


# =============================================================================
# EN6 — File descriptor leak monitor
# =============================================================================
class TestFdLeakMonitor:
    def test_fd_count_stable_over_2000_cycles(self, mock_hl_info):
        import psutil
        import gc

        proc = psutil.Process()
        gc.collect()
        fds_start = len(proc.open_files()) + len(proc.connections())

        for _ in range(2000):
            # scanner.scan(...)
            pass

        gc.collect()
        fds_end = len(proc.open_files()) + len(proc.connections())

        delta = fds_end - fds_start
        print(f"\nFD delta after 2000 cycles: {delta}")
        assert delta < 10, f"File descriptor leak: {delta} new fds"
        pytest.skip("Wire scanner")
