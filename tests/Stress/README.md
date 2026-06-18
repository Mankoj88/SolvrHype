# Solvira V2 Stress Suite

Offline stress tests for the dual-market (spot + derivative) revamp.
Verifies the bot survives realistic VPS conditions before going live:
parallel scans, universe scale, rate-limit bursts, API flapping,
concurrent threads, allocation saturation, state durability, and memory.

## Running

```bash
# Full stress suite
.venv/Scripts/python.exe -m pytest tests/Stress -v -m stress

# Single file
.venv/Scripts/python.exe -m pytest tests/Stress/test_dual_market_load.py -v

# Show printed metrics (latency, candle calls, memory)
.venv/Scripts/python.exe -m pytest tests/Stress -v -m stress -s
```

## What each file covers

| File | Surface area |
| --- | --- |
| `test_dual_market_load.py` | `asyncio.gather(spot, deriv)` 200 cycles, p95 latency, cache discipline |
| `test_universe_scale.py` | 200-asset universe; `MAX_CANDIDATES_PER_CYCLE` cap; flat memory |
| `test_rate_limit_burst.py` | 429 on every Nth candle; loop survives & recovers |
| `test_allocation_saturation.py` | Pool isolation, min/max bounds, spot slot exhaustion |
| `test_concurrent_trading.py` | Wallet cache under threads; scheduler thread vs asyncio |
| `test_api_flapping.py` | 30 % universe outages, stale cache fallback, partial candle 503s |
| `test_order_state_storm.py` | State-file JSON integrity under rapid open/close; cooldown |
| `test_memory_throughput.py` | 500-cycle heap diff, OI tracker bound, universe cache bound |

## Fixtures (`conftest.py`)

- `StressInfo` — drop-in Hyperliquid Info mock with failure injection
  (`rate_limit_every`, `server_error_every`, `fail_universe_pct`,
  `candle_latency_ms`).
- `StressExchange` — minimal Exchange mock recording all calls.
- `stress_info` / `large_stress_info` — 50 / 200 asset universes.
- `patched_sdk` + `isolate_state_dir` — used by order/state tests so the
  real `data/positions_state.json` is never touched.
- `fast_throttle` + `disable_funding_window` — remove inter-call sleeps
  and the funding-window guard so scans actually exercise the pipeline.

## VPS readiness rubric

A green run on a 1 vCPU / 1 GB box means:
- 200 dual-market cycles in well under 60 s (room for real network).
- < 25 MB heap drift across 500 cycles → safe for multi-day uptime.
- All known failure modes (429, 5xx, partial outage) end in graceful
  skip + retry next cycle, never bot termination.
