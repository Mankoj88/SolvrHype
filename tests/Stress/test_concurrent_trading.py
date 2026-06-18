"""
ST5 — Concurrent thread/asyncio interplay.

main.py runs:
- An asyncio main loop calling trading_cycle.
- A separate daemon thread (schedule_thread) running schedule.run_pending,
  which calls _daily_summary_job, _daily_snapshot_job, _weekly_review_job.

These two execution contexts mutate shared state (order_manager.positions,
wallet cache). Verify there is no race condition that corrupts state when
stressed. (Sweep job removed: unified account mode, no spot↔perp transfer.)
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from execution.wallet import WalletReader

pytestmark = pytest.mark.stress


def test_wallet_cache_consistent_under_concurrent_reads(stress_info, stress_exchange):
    """Multiple threads read get_unified_balance simultaneously; cache TTL
    must keep results coherent (no torn writes)."""
    reader = WalletReader(info=stress_info, exchange=stress_exchange,
                          account="0x" + "aa" * 20)

    results = []
    errors = []

    def worker():
        try:
            for _ in range(200):
                bal = reader.get_unified_balance()
                # All fields populated and total_equity is the sum
                got_total = bal.perp_equity + bal.spot_usdc + bal.spot_tokens_value_usd
                if abs(got_total - bal.total_equity) > 1e-6:
                    errors.append(("torn", bal.to_dict()))
                results.append(bal.total_equity)
        except Exception as e:
            errors.append(("exc", type(e).__name__, str(e)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Wallet race detected: {errors[:5]}"
    assert len(results) == 8 * 200


def test_sweep_under_concurrent_balance_reads(stress_info, stress_exchange):
    """Sweep mutates cache (sets to None). Concurrent readers must not crash."""
    reader = WalletReader(info=stress_info, exchange=stress_exchange,
                          account="0x" + "aa" * 20)

    stop = threading.Event()
    errors = []

    def reader_loop():
        try:
            while not stop.is_set():
                reader.get_unified_balance()
        except Exception as e:
            errors.append((type(e).__name__, str(e)))

    def sweeper_loop():
        try:
            for _ in range(50):
                reader.auto_sweep_spot_to_perp(min_amount=1.0)
                time.sleep(0.001)
        except Exception as e:
            errors.append((type(e).__name__, str(e)))

    readers = [threading.Thread(target=reader_loop) for _ in range(4)]
    sweeper = threading.Thread(target=sweeper_loop)

    for t in readers:
        t.start()
    sweeper.start()

    sweeper.join()
    stop.set()
    for t in readers:
        t.join(timeout=2)

    assert not errors, f"Concurrent sweep/read errors: {errors[:5]}"


@pytest.mark.asyncio
async def test_schedule_thread_vs_asyncio_loop(stress_info, stress_exchange):
    """Simulate the actual main.py structure: asyncio scan loop + threading
    schedule loop both poking the wallet. Verify both make progress."""
    reader = WalletReader(info=stress_info, exchange=stress_exchange,
                          account="0x" + "aa" * 20)

    bg_done = threading.Event()
    bg_count = [0]
    bg_errors = []

    def schedule_loop():
        try:
            for _ in range(100):
                if bg_done.is_set():
                    return
                reader.auto_sweep_spot_to_perp(min_amount=1.0)
                bg_count[0] += 1
                time.sleep(0.002)
        except Exception as e:
            bg_errors.append((type(e).__name__, str(e)))

    t = threading.Thread(target=schedule_loop, daemon=True)
    t.start()

    fg_count = 0
    for _ in range(100):
        await asyncio.to_thread(reader.get_unified_balance)
        fg_count += 1

    bg_done.set()
    t.join(timeout=2)

    assert not bg_errors, f"schedule loop errors: {bg_errors[:3]}"
    assert fg_count == 100
    assert bg_count[0] > 8, f"schedule loop starved: {bg_count[0]} sweeps"
