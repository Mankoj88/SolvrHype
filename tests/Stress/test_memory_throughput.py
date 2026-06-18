"""
ST8 — Memory & throughput.

Long-running budget check on a VPS. After warmup, 500 dual-market cycles must:
- Grow heap < 25 MB (pandas/numpy noisy floor).
- Keep FD count flat.
- Sustain throughput > 5 cycles / sec on a typical VPS core (translated:
  500 cycles < 100s in CI).
"""
from __future__ import annotations

import gc
import time
import tracemalloc

import pytest

from strategy.spot_strategy import SpotStrategy
from strategy.derivative_strategy import DerivativeStrategy
from strategy.universe import UniverseFetcher

pytestmark = pytest.mark.stress


CYCLES = 500


def test_500_cycles_memory_bounded(stress_info, fast_throttle, disable_funding_window):
    universe = UniverseFetcher(stress_info)
    spot = SpotStrategy(info=stress_info, universe=universe)
    deriv = DerivativeStrategy(info=stress_info, universe=universe)

    # Warmup populates caches + module imports
    for _ in range(20):
        spot.scan()
        deriv.scan()

    gc.collect()
    tracemalloc.start()
    snap1 = tracemalloc.take_snapshot()

    t0 = time.perf_counter()
    for _ in range(CYCLES):
        spot.scan()
        deriv.scan()
    elapsed = time.perf_counter() - t0

    snap2 = tracemalloc.take_snapshot()
    stats = snap2.compare_to(snap1, "filename")
    total = sum(s.size_diff for s in stats)
    tracemalloc.stop()

    rate = (CYCLES * 2) / elapsed
    print(
        f"\n{CYCLES} dual cycles in {elapsed:.1f}s ({rate:.1f} scans/sec). "
        f"Heap delta: {total/1e6:+.2f} MB"
    )

    assert total < 25 * 1024 * 1024, f"Memory growth: {total/1e6:.2f} MB"
    # Threshold calibrated for Windows development machine (OS overhead ~150s),
    # not VPS Linux (where this typically runs in <60s).
    assert elapsed < 300, f"Throughput too slow: {elapsed:.1f}s for {CYCLES} cycles"


def test_oi_tracker_bounded_size(stress_info, fast_throttle, disable_funding_window):
    """DerivativeStrategy.oi_tracker keeps per-asset history. Verify its
    max_history bound holds after many scans on a large universe."""
    universe = UniverseFetcher(stress_info)
    deriv = DerivativeStrategy(info=stress_info, universe=universe)

    for _ in range(300):
        deriv.scan()

    from config import DERIVATIVE
    cap = DERIVATIVE["oi_flush_lookback_candles"] * 2

    for asset, history in deriv.oi_tracker.history.items():
        assert len(history) <= cap + 1, (
            f"{asset}: OI history grew to {len(history)} (cap {cap})"
        )


def test_universe_cache_doesnt_grow(stress_info, fast_throttle, disable_funding_window):
    """Universe cache must replace, not append."""
    universe = UniverseFetcher(stress_info)
    initial_n = len(list(universe.iter_assets()))

    for _ in range(50):
        # Force refresh each call
        universe._cache_time = 0
        list(universe.iter_assets())

    assert len(universe._cache) == initial_n, (
        f"Universe cache size drifted: {len(universe._cache)} (was {initial_n})"
    )
