"""
Endurance — Memory & file-descriptor leak detection.

Test IDs: EN4, EN5, EN6 from solvira_stress_test_master.md §6.1.
Source: docs/test_long_running.py + memory/fd classes from
docs/test_fault_injection.py.

Run separately:
  pytest tests/Endurance/test_memory_leak.py -v -m endurance
"""

import gc
import tracemalloc

import pytest

pytestmark = [pytest.mark.endurance, pytest.mark.slow]


# =============================================================================
# EN4 — tracemalloc diff under 10MB after 5000 cycles
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
# EN4b — 1000-cycle bounded growth (chaos memory leak smoke)
# =============================================================================
class TestMemoryLeakShort:
    def test_1000_scan_cycles_memory_stable(self, mock_hl_info, sample_candles_df):
        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()

        for _ in range(1000):
            # scanner.scan(["BTC", "ETH", "SOL", "ARB"])
            pass

        snapshot2 = tracemalloc.take_snapshot()
        stats = snapshot2.compare_to(snapshot1, "lineno")
        top_growth = sum(s.size_diff for s in stats[:10])
        # Allow 10MB growth max
        assert top_growth < 10 * 1024 * 1024, \
            f"Memory grew {top_growth/1e6:.1f}MB"
        pytest.skip("Wire scanner")


# =============================================================================
# EN5 — RSS curve via mprof (manual run)
# =============================================================================
# Run manually:
#   mprof run pytest tests/Endurance/test_memory_leak.py::TestRssCurveManual -v
#   mprof plot
#
# Expected: flat RSS curve (no upward drift)


class TestRssCurveManual:
    @pytest.mark.skip_ci
    def test_rss_curve_flat_manual(self):
        pytest.skip("Manual run: mprof run python main.py")


# =============================================================================
# EN6 — File descriptor leak monitor
# =============================================================================
class TestFdLeakMonitor:
    def test_fd_count_stable_over_2000_cycles(self, mock_hl_info):
        import psutil

        proc = psutil.Process()
        gc.collect()
        try:
            fds_start = len(proc.open_files()) + len(proc.connections())
        except (psutil.AccessDenied, AttributeError):
            pytest.skip("psutil cannot enumerate fds in this environment")

        for _ in range(2000):
            # scanner.scan(...)
            pass

        gc.collect()
        fds_end = len(proc.open_files()) + len(proc.connections())

        delta = fds_end - fds_start
        print(f"\nFD delta after 2000 cycles: {delta}")
        assert delta < 10, f"File descriptor leak: {delta} new fds"
        pytest.skip("Wire scanner")


# =============================================================================
# Chaos-flavor short fd leak check (paired with chaos suite)
# =============================================================================
class TestFdLeakShort:
    def test_no_fd_leak_after_500_cycles(self):
        import psutil
        proc = psutil.Process()
        try:
            fds_before = (
                proc.num_fds() if hasattr(proc, "num_fds")
                else len(proc.open_files())
            )
            # for _ in range(500): scanner.scan(...)
            fds_after = (
                proc.num_fds() if hasattr(proc, "num_fds")
                else len(proc.open_files())
            )
            assert fds_after - fds_before < 5
        except (psutil.AccessDenied, AttributeError):
            pytest.skip("psutil cannot enumerate fds in this environment")
        pytest.skip("Wire scanner")
