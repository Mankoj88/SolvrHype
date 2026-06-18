"""
ST2 — Universe scale.

Large 200-asset universe with realistic distribution of drop/funding triggers.
V2 must:
- Honor MAX_CANDIDATES_PER_CYCLE cap (≤ 20 candle calls / scan).
- Complete a full dual-market scan well under cycle interval (60s).
- Keep memory flat across repeated scans of the large universe.
"""
from __future__ import annotations

import time
import tracemalloc

import pytest

from strategy.spot_strategy import SpotStrategy
from strategy.derivative_strategy import DerivativeStrategy
from strategy.universe import UniverseFetcher
from config import MAX_CANDIDATES_PER_CYCLE

pytestmark = pytest.mark.stress


SCAN_BUDGET_SEC = 5.0


def test_200_asset_scan_respects_candidate_cap(
    large_stress_info, fast_throttle, disable_funding_window,
):
    universe = UniverseFetcher(large_stress_info)
    spot = SpotStrategy(info=large_stress_info, universe=universe)

    t0 = time.perf_counter()
    spot.scan()
    spot_dur = time.perf_counter() - t0

    candle_calls = large_stress_info.metrics.candles_calls
    print(
        f"\nSpot scan 200u: {spot_dur*1000:.1f}ms, "
        f"{candle_calls} candle calls (cap={MAX_CANDIDATES_PER_CYCLE})"
    )

    assert candle_calls <= MAX_CANDIDATES_PER_CYCLE, (
        f"Spot exceeded MAX_CANDIDATES_PER_CYCLE: {candle_calls}"
    )
    assert spot_dur < SCAN_BUDGET_SEC, f"Spot scan too slow on 200u: {spot_dur:.2f}s"


def test_200_asset_deriv_scan_respects_cap(
    large_stress_info, fast_throttle, disable_funding_window,
):
    universe = UniverseFetcher(large_stress_info)
    deriv = DerivativeStrategy(info=large_stress_info, universe=universe)

    # Prime OI tracker over a few cycles so flush-detection has history.
    for _ in range(3):
        deriv.scan()
    pre_calls = large_stress_info.metrics.candles_calls

    t0 = time.perf_counter()
    deriv.scan()
    dur = time.perf_counter() - t0
    calls_this_scan = large_stress_info.metrics.candles_calls - pre_calls

    print(
        f"\nDeriv scan 200u: {dur*1000:.1f}ms, "
        f"{calls_this_scan} candle calls (cap={MAX_CANDIDATES_PER_CYCLE})"
    )
    assert calls_this_scan <= MAX_CANDIDATES_PER_CYCLE, (
        f"Deriv exceeded MAX_CANDIDATES_PER_CYCLE: {calls_this_scan}"
    )
    assert dur < SCAN_BUDGET_SEC


def test_200_asset_memory_flat_over_100_scans(
    large_stress_info, fast_throttle, disable_funding_window,
):
    universe = UniverseFetcher(large_stress_info)
    spot = SpotStrategy(info=large_stress_info, universe=universe)
    deriv = DerivativeStrategy(info=large_stress_info, universe=universe)

    # Warmup
    for _ in range(20):
        spot.scan()
        deriv.scan()

    tracemalloc.start()
    snap1 = tracemalloc.take_snapshot()

    for _ in range(100):
        spot.scan()
        deriv.scan()

    snap2 = tracemalloc.take_snapshot()
    stats = snap2.compare_to(snap1, "filename")
    total = sum(s.size_diff for s in stats)
    tracemalloc.stop()

    print(f"\n200u memory diff over 100 scans: {total/1e6:+.2f} MB")
    # Generous: allow 15MB drift (pandas/numpy allocators are noisy).
    assert total < 15 * 1024 * 1024, f"Memory growth: {total/1e6:.2f} MB"
