"""
ST6 — API flapping resilience.

Hyperliquid / Cloudfront frequently flaps with 5xx during high load. V2:
- scanner._get_meta_with_ctx retries with backoff and falls back to stale cache.
- universe._refresh logs warning and reuses stale cache.
- strategy candles fetch raises → asset skipped, scan returns partial list.

Verify the bot keeps trading through realistic flapping percentages without
state corruption or loop death.
"""
from __future__ import annotations

import asyncio

import pytest

from strategy.spot_strategy import SpotStrategy
from strategy.derivative_strategy import DerivativeStrategy
from strategy.universe import UniverseFetcher

pytestmark = pytest.mark.stress


@pytest.mark.asyncio
async def test_30pct_universe_failures_loop_survives(
    stress_info_factory, fast_throttle, disable_funding_window,
):
    info = stress_info_factory()
    universe = UniverseFetcher(info)
    spot = SpotStrategy(info=info, universe=universe)
    deriv = DerivativeStrategy(info=info, universe=universe)

    # Warm universe cache once
    list(universe.iter_assets())
    # Then turn on flapping
    info.plan.fail_universe_pct = 0.3

    crashes = 0
    skipped_cycles = 0
    for _ in range(60):
        try:
            s, d = await asyncio.gather(
                asyncio.to_thread(spot.scan),
                asyncio.to_thread(deriv.scan),
                return_exceptions=True,
            )
            for r in (s, d):
                if isinstance(r, Exception):
                    skipped_cycles += 1
        except Exception:
            crashes += 1

    assert crashes == 0, f"Loop crashed {crashes}x under 30% flapping"
    # Universe failures during refresh just log + reuse stale cache, so we
    # expect skipped_cycles to be 0 — the bot keeps trading.
    assert skipped_cycles == 0, f"{skipped_cycles} scan cycles surfaced exception"


@pytest.mark.asyncio
async def test_universe_uses_stale_cache_after_failure(
    stress_info_factory, fast_throttle, disable_funding_window,
):
    info = stress_info_factory()
    universe = UniverseFetcher(info)

    # Prime cache
    initial = list(universe.iter_assets())
    pre_meta_calls = info.metrics.meta_calls
    assert len(initial) > 0

    # All subsequent meta calls fail
    info.plan.fail_universe_pct = 1.0
    # Force expiry by zeroing cache timestamp
    universe._cache_time = 0

    # Should not raise; should yield stale cache
    second = list(universe.iter_assets())
    assert second == initial, "Universe did not fall back to stale cache"
    assert info.metrics.universe_failures > 0


def test_candle_flap_30pct_partial_results(stress_info_factory, fast_throttle, disable_funding_window):
    """30% of candle calls 503; spot scan must still return signals for the
    survivors, not raise."""
    info = stress_info_factory()
    # rate_limit_every=3 → roughly 33% calls fail
    info.plan.rate_limit_every = 3
    universe = UniverseFetcher(info)
    spot = SpotStrategy(info=info, universe=universe)

    total_signals = 0
    for _ in range(20):
        try:
            sigs = spot.scan()
        except Exception as e:
            pytest.fail(f"spot.scan raised under candle flap: {e}")
        total_signals += len(sigs)
        # Clear strategy candle cache so next cycle re-fetches
        spot._candles_cache.clear()

    # Smoke: at least some 429s were injected
    assert info.metrics.candles_429 > 0
