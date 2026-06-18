"""
ST1 — Dual-market scan load.

Replicates main.trading_cycle's parallel spot+derivative scan via
asyncio.gather + asyncio.to_thread, running it N cycles back-to-back.

Asserts:
- No unhandled exceptions across N cycles.
- p95 cycle latency < threshold (VPS feasibility).
- Both strategies actually get exercised (scan called each cycle).
- Universe cache eliminates redundant meta calls within TTL.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from strategy.spot_strategy import SpotStrategy
from strategy.derivative_strategy import DerivativeStrategy
from strategy.universe import UniverseFetcher

pytestmark = pytest.mark.stress


CYCLES = 200
LATENCY_P95_BUDGET_SEC = 1.5


async def _one_cycle(spot: SpotStrategy, deriv: DerivativeStrategy) -> tuple[int, int]:
    spot_task = asyncio.to_thread(spot.scan)
    deriv_task = asyncio.to_thread(deriv.scan)
    spot_sigs, deriv_sigs = await asyncio.gather(spot_task, deriv_task,
                                                  return_exceptions=False)
    return len(spot_sigs), len(deriv_sigs)


@pytest.mark.asyncio
async def test_dual_market_load_200_cycles(
    stress_info, stress_exchange, fast_throttle, disable_funding_window,
):
    universe = UniverseFetcher(stress_info)
    spot = SpotStrategy(info=stress_info, universe=universe)
    deriv = DerivativeStrategy(info=stress_info, universe=universe)

    durations = []
    spot_sig_total = 0
    deriv_sig_total = 0
    exceptions = []

    for i in range(CYCLES):
        t0 = time.perf_counter()
        try:
            s, d = await _one_cycle(spot, deriv)
            spot_sig_total += s
            deriv_sig_total += d
        except Exception as e:
            exceptions.append((i, type(e).__name__, str(e)))
        durations.append(time.perf_counter() - t0)

    assert not exceptions, f"Unhandled exceptions in dual-market loop: {exceptions[:3]}"

    p50 = statistics.median(durations)
    p95 = sorted(durations)[int(len(durations) * 0.95)]
    p99 = sorted(durations)[int(len(durations) * 0.99)]

    print(
        f"\nDual-market {CYCLES}c: p50={p50*1000:.1f}ms p95={p95*1000:.1f}ms "
        f"p99={p99*1000:.1f}ms spot_sigs={spot_sig_total} "
        f"deriv_sigs={deriv_sig_total} "
        f"meta_calls={stress_info.metrics.meta_calls} "
        f"candle_calls={stress_info.metrics.candles_calls}"
    )

    assert p95 < LATENCY_P95_BUDGET_SEC, f"p95 too slow: {p95:.2f}s"
    # Universe ctx cached 60s — over 200 fast cycles we should hit meta sparingly.
    # Allow up to ceil(elapsed / 60) + slack.
    elapsed = sum(durations)
    expected_meta_max = int(elapsed / 60) + 4
    assert stress_info.metrics.meta_calls <= expected_meta_max, (
        f"Universe cache leak: {stress_info.metrics.meta_calls} meta calls "
        f"in {elapsed:.1f}s (expected ≤ {expected_meta_max})"
    )


@pytest.mark.asyncio
async def test_scanner_exceptions_dont_kill_loop(
    stress_info_factory, fast_throttle, disable_funding_window,
):
    """
    main.py wraps each strategy result in try/except (RuntimeError → skip cycle,
    other → log + skip). Verify this resilience holds when universe API
    intermittently 503s.
    """
    info = stress_info_factory()
    # Make universe meta blow up 100% of the time AFTER the warmup cycle.
    universe = UniverseFetcher(info)
    spot = SpotStrategy(info=info, universe=universe)
    deriv = DerivativeStrategy(info=info, universe=universe)
    # Prime cache with one good call
    list(universe.iter_assets())
    # Now inject failures
    info.plan.fail_universe_pct = 1.0

    crashes = 0
    for i in range(50):
        try:
            tasks = [asyncio.to_thread(spot.scan), asyncio.to_thread(deriv.scan)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # main.py's pattern: count RuntimeError as skip, log others
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, RuntimeError):
                    crashes += 1
        except Exception:
            crashes += 1

    # Stale cache used by UniverseFetcher means scan completes without surfacing
    # the failure. Either way: no unexpected exceptions.
    assert crashes == 0, f"{crashes} unexpected crashes during universe outage"
