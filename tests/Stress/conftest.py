"""
Stress test fixtures for Solvira V2 (spot + derivative dual-market).

Goals:
- Fully offline: no network, no real Hyperliquid calls.
- Reproducible: seeded RNG, deterministic candle generators.
- Configurable failure injection (rate limit, 5xx, latency, partial outages).
- Tunable universe size (default 50, large stress 200).

All fixtures here are stress-only. They do not replace tests/conftest.py —
they only add knobs the stress suite needs.
"""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Synthetic universe + candles
# ---------------------------------------------------------------------------

DEFAULT_UNIVERSE_SIZE = 50
LARGE_UNIVERSE_SIZE = 200

# Realistic-looking HL asset names (mix from CRYPTO_WHITELIST + filler).
BASE_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "HYPE", "ARB", "OP", "AVAX", "DOGE",
    "LINK", "ATOM", "NEAR", "INJ", "TIA", "SUI", "APT", "SEI", "DOT",
    "PENDLE", "ENA", "TAO",
]


def make_asset_names(n: int) -> list[str]:
    out = list(BASE_ASSETS)
    i = 0
    while len(out) < n:
        out.append(f"SYN{i:03d}")
        i += 1
    return out[:n]


def make_ctx(
    *,
    mark_px: float,
    prev_day_px: float,
    day_ntl_vlm: float,
    funding: float = 0.0,
    open_interest: float = 0.0,
) -> dict:
    return {
        "markPx": str(mark_px),
        "prevDayPx": str(prev_day_px),
        "dayNtlVlm": str(day_ntl_vlm),
        "funding": str(funding),
        "openInterest": str(open_interest),
        "premium": "0.0",
        "midPx": str(mark_px),
        "impactPxs": [str(mark_px * 0.9995), str(mark_px * 1.0005)],
    }


def make_universe(
    size: int = DEFAULT_UNIVERSE_SIZE,
    *,
    seed: int = 42,
    droppers_pct: float = 0.4,
    deriv_setup_pct: float = 0.2,
) -> tuple[dict, list[dict]]:
    """
    Build (meta, contexts) tuple matching HL `meta_and_asset_ctxs()` shape.

    - `droppers_pct` of assets get drop_pct ≤ -2% (triggers spot ctx pre-filter).
    - `deriv_setup_pct` of assets get funding far from 0 (triggers deriv setup).
    """
    rng = np.random.default_rng(seed)
    names = make_asset_names(size)
    universe = []
    contexts = []
    for i, name in enumerate(names):
        prev = float(rng.uniform(0.5, 70_000.0))
        is_dropper = (i / size) < droppers_pct
        is_deriv = (i / size) >= (1.0 - deriv_setup_pct)
        # Spot pre-filter wants mark < prev_day → drop_pct <= -2%
        if is_dropper:
            mark = prev * float(rng.uniform(0.94, 0.97))
        else:
            mark = prev * float(rng.uniform(0.99, 1.03))
        vol = float(rng.uniform(1.5e5, 5e7))      # > spot min 100k
        funding = 0.0
        if is_deriv:
            # half negative (long setup), half positive (short setup)
            funding = float(rng.uniform(-0.001, -0.0006)) if i % 2 == 0 \
                else float(rng.uniform(0.0006, 0.001))
        oi = float(rng.uniform(1_000, 1_000_000))
        universe.append({"name": name, "szDecimals": 4, "maxLeverage": 20})
        contexts.append(make_ctx(
            mark_px=mark, prev_day_px=prev, day_ntl_vlm=vol,
            funding=funding, open_interest=oi,
        ))
    return {"universe": universe}, contexts


def make_candles(
    *,
    n: int = 120,
    start_price: float = 100.0,
    seed: int = 0,
    oversold: bool = False,
    timeframe_ms: int = 5 * 60 * 1000,
) -> list[dict]:
    """
    Generate HL candles_snapshot-shaped data.

    - `oversold=True` injects a sharp drop+bounce in the last ~30 candles so the
      spot/derivative indicators can fire on it.
    """
    rng = np.random.default_rng(seed)
    closes = start_price + np.cumsum(rng.normal(0.0, start_price * 0.005, n))
    closes = np.clip(closes, start_price * 0.5, start_price * 1.5)
    if oversold:
        closes[-30:-3] *= 0.88
        closes[-3:] = closes[-4] * np.array([1.012, 1.022, 1.030])
    highs = closes + np.abs(rng.normal(0, start_price * 0.001, n))
    lows = closes - np.abs(rng.normal(0, start_price * 0.001, n))
    opens = np.concatenate([[start_price], closes[:-1]])
    volumes = rng.uniform(800, 4000, n)
    if oversold:
        volumes[-1] = volumes[-30:-1].mean() * 4.0

    now_ms = int(time.time() * 1000)
    candles = []
    for i in range(n):
        t = now_ms - (n - i - 1) * timeframe_ms
        candles.append({
            "t": t, "T": t + timeframe_ms,
            "o": str(float(opens[i])), "h": str(float(highs[i])),
            "l": str(float(lows[i])), "c": str(float(closes[i])),
            "v": str(float(volumes[i])), "n": 50,
        })
    return candles


# ---------------------------------------------------------------------------
# StressInfo — instrumented mock of hyperliquid.info.Info
# ---------------------------------------------------------------------------

@dataclass
class FailurePlan:
    """Tunable failure injection for stress runs."""
    rate_limit_every: int = 0          # raise 429 every Nth candles_snapshot
    server_error_every: int = 0        # raise 503 every Nth meta_and_asset_ctxs
    candle_latency_ms: float = 0.0     # synthetic sleep per candle call
    meta_latency_ms: float = 0.0       # synthetic sleep per meta call
    fail_universe_pct: float = 0.0     # probability meta_and_asset_ctxs throws


@dataclass
class StressMetrics:
    candles_calls: int = 0
    meta_calls: int = 0
    all_mids_calls: int = 0
    candles_429: int = 0
    meta_5xx: int = 0
    universe_failures: int = 0
    per_asset_candle_calls: dict[str, int] = field(default_factory=dict)


class StressInfo:
    """
    Drop-in replacement for hyperliquid.info.Info — implements just the
    methods the V2 code actually calls during scan/trade cycles.

    Exposes `metrics` for assertions and `plan` for failure injection.
    """

    def __init__(
        self,
        universe_size: int = DEFAULT_UNIVERSE_SIZE,
        seed: int = 42,
        plan: Optional[FailurePlan] = None,
        oversold_assets: Optional[set[str]] = None,
    ):
        self._seed = seed
        self._meta, self._ctxs = make_universe(universe_size, seed=seed)
        self._asset_names = [a["name"] for a in self._meta["universe"]]
        self.plan = plan or FailurePlan()
        self.metrics = StressMetrics()
        self._call_counter = 0
        self._oversold = oversold_assets or {"BTC", "ETH", "SOL"}
        self._rand = random.Random(seed)
        # Replicate hl SDK surface that V2 code touches
        self._meta_cache = self._meta

    # ------------------------------------------------------------------ meta
    def meta(self) -> dict:
        return self._meta

    def meta_and_asset_ctxs(self):
        self.metrics.meta_calls += 1
        if self.plan.meta_latency_ms:
            time.sleep(self.plan.meta_latency_ms / 1000)
        if self.plan.fail_universe_pct and self._rand.random() < self.plan.fail_universe_pct:
            self.metrics.universe_failures += 1
            raise ConnectionError("synthetic universe outage")
        if self.plan.server_error_every and self.metrics.meta_calls % self.plan.server_error_every == 0:
            self.metrics.meta_5xx += 1
            raise RuntimeError("HTTP 503 service unavailable (synthetic)")
        return [self._meta, self._ctxs]

    # ------------------------------------------------------------------ candles
    def candles_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int):
        self.metrics.candles_calls += 1
        self.metrics.per_asset_candle_calls[coin] = \
            self.metrics.per_asset_candle_calls.get(coin, 0) + 1
        if self.plan.candle_latency_ms:
            time.sleep(self.plan.candle_latency_ms / 1000)
        if self.plan.rate_limit_every and self.metrics.candles_calls % self.plan.rate_limit_every == 0:
            self.metrics.candles_429 += 1
            raise RuntimeError("429 rate limit exceeded (synthetic)")

        seed = hash((coin, interval)) & 0xFFFF
        start_price = 100.0 + (seed % 5000)
        return make_candles(
            n=120, start_price=start_price, seed=seed,
            oversold=(coin in self._oversold),
            timeframe_ms=5 * 60 * 1000,
        )

    # ------------------------------------------------------------------ prices
    def all_mids(self) -> dict[str, str]:
        self.metrics.all_mids_calls += 1
        out = {}
        for asset, ctx in zip(self._asset_names, self._ctxs):
            out[asset] = ctx["markPx"]
        return out

    # ------------------------------------------------------------------ wallet
    def user_state(self, address: str) -> dict:
        return {
            "marginSummary": {
                "accountValue": "1000.0",
                "totalRawUsd": "1000.0",
                "totalNtlPos": "0.0",
                "totalMarginUsed": "0.0",
            },
            "assetPositions": [],
            "withdrawable": "950.0",
        }

    def spot_user_state(self, address: str) -> dict:
        return {
            "balances": [
                {"coin": "USDC", "total": "120.5", "hold": "0", "entryNtl": "120.5"},
                {"coin": "HYPE", "total": "5.0", "hold": "0", "entryNtl": "200.0"},
            ]
        }

    def spot_meta(self) -> dict:
        return {
            "tokens": [
                {"name": "USDC", "index": 0},
                {"name": "HYPE", "index": 1},
            ],
            "universe": [
                {"name": "HYPE/USDC", "tokens": [1, 0]},
            ],
        }


# ---------------------------------------------------------------------------
# StressExchange — minimal mock of hyperliquid.exchange.Exchange
# ---------------------------------------------------------------------------

class StressExchange:
    """Records calls; never touches the network. Order IDs auto-increment."""

    def __init__(self, *, fail_open_every: int = 0, slippage_pct: float = 0.0):
        self._oid = itertools.count(start=10_000)
        self.fail_open_every = fail_open_every
        self.slippage_pct = slippage_pct
        self.market_open_calls = 0
        self.market_close_calls = 0
        self.order_calls = 0
        self.cancel_calls = 0
        self.leverage_calls = 0
        self.transfer_calls = 0

    def market_open(self, asset, is_buy, sz, px, slippage):
        self.market_open_calls += 1
        if self.fail_open_every and self.market_open_calls % self.fail_open_every == 0:
            return {"status": "err", "response": "synthetic fail"}
        fill_px = (px or 100.0) * (1 + self.slippage_pct)
        return {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": str(sz), "avgPx": str(fill_px),
                            "oid": next(self._oid)}}
            ]}},
        }

    def market_close(self, asset, sz=None):
        self.market_close_calls += 1
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": str(sz or 0), "avgPx": "0", "oid": next(self._oid)}}
        ]}}}

    def order(self, asset, is_buy, sz, px, order_type, reduce_only=False):
        self.order_calls += 1
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": next(self._oid)}}
        ]}}}

    def cancel(self, asset, oid):
        self.cancel_calls += 1
        return {"status": "ok"}

    def update_leverage(self, leverage, asset, is_cross=False):
        self.leverage_calls += 1
        return {"status": "ok"}

    def usd_class_transfer(self, amount, to_perp=True):
        self.transfer_calls += 1
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stress_info_factory():
    """Returns a factory callable so each test can configure plan/size/oversold."""
    def _make(**kwargs) -> StressInfo:
        return StressInfo(**kwargs)
    return _make


@pytest.fixture
def stress_info(stress_info_factory) -> StressInfo:
    """Default stress Info with 50-asset universe and no failure injection."""
    return stress_info_factory()


@pytest.fixture
def large_stress_info(stress_info_factory) -> StressInfo:
    """200-asset universe for scale tests."""
    return stress_info_factory(universe_size=LARGE_UNIVERSE_SIZE)


@pytest.fixture
def stress_exchange() -> StressExchange:
    return StressExchange()


@pytest.fixture
def isolate_state_dir(tmp_path, monkeypatch):
    """
    Re-point config.DATA_DIR + DB_PATH + LOGS_DIR at a tmp dir so order_manager
    state files don't bleed between stress tests (or pollute real data/).
    """
    from pathlib import Path
    import config

    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "solvira.db")
    # OrderManager caches STATE_FILE as a class attr — patch it too
    from execution import order_manager as om
    monkeypatch.setattr(om.OrderManager, "STATE_FILE", data_dir / "positions_state.json")
    yield data_dir


@pytest.fixture
def patched_sdk(monkeypatch, stress_info, stress_exchange):
    """
    Monkeypatch the hyperliquid SDK + eth_account in modules that construct
    them directly (order_manager). Strategy modules accept `info=` so no patch
    needed there.
    """
    from execution import order_manager as om
    from execution import wallet as wallet_mod
    from strategy import scanner as scanner_mod

    monkeypatch.setattr(om, "Info", lambda *a, **k: stress_info)
    monkeypatch.setattr(om, "Exchange", lambda *a, **k: stress_exchange)
    monkeypatch.setattr(scanner_mod, "Info", lambda *a, **k: stress_info)
    # eth_account.Account.from_key is called with a hex key; .env provides one.
    # Just make sure it returns something harmless.

    class _FakeAccount:
        address = "0x" + "ab" * 20

    monkeypatch.setattr(om.Account, "from_key", lambda k: _FakeAccount())
    yield stress_info, stress_exchange


@pytest.fixture
def fast_throttle(monkeypatch):
    """Disable inter-call sleep so scan loops don't pace themselves in stress."""
    import config
    monkeypatch.setattr(config, "CANDLE_FETCH_INTER_CALL_SLEEP_SEC", 0.0)
    # Also patch the symbols already imported by strategy modules
    from strategy import spot_strategy, derivative_strategy
    monkeypatch.setattr(spot_strategy, "CANDLE_FETCH_INTER_CALL_SLEEP_SEC", 0.0)
    monkeypatch.setattr(derivative_strategy, "CANDLE_FETCH_INTER_CALL_SLEEP_SEC", 0.0)


@pytest.fixture
def disable_funding_window(monkeypatch):
    """Move funding window guard out of the way so synthetic scans can fire."""
    import config
    monkeypatch.setattr(config, "NEVER_TRADE_FUNDING_WINDOW_MINUTES", 0)
    from strategy import spot_strategy, scanner
    monkeypatch.setattr(spot_strategy, "NEVER_TRADE_FUNDING_WINDOW_MINUTES", 0)
    monkeypatch.setattr(scanner, "NEVER_TRADE_FUNDING_WINDOW_MINUTES", 0)
