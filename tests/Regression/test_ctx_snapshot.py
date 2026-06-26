"""
Regression — ctx snapshot capture (offline backtest feed).

Proves the capture is (a) well-formed, (b) NEVER raises on a write failure, and
(c) FULLY isolated from the universe-refresh path — a snapshot failure must not
touch self._cache. Plus (d) the env flag truly gates writing.
"""
import json

import pytest
from loguru import logger

import config
from monitoring.ctx_snapshot import write_ctx_snapshot
from strategy.universe import UniverseFetcher

pytestmark = [pytest.mark.regression]


def _sample_assets():
    """list[(asset, ctx)] like UniverseFetcher caches — BTC, ETH + a few alts."""
    return [
        ("BTC", {"markPx": "65000.0", "prevDayPx": "64000.0", "dayNtlVlm": "1000000.0",
                 "openInterest": "1000.0", "funding": "0.00001"}),     # +1.56%
        ("ETH", {"markPx": "3400.0", "prevDayPx": "3500.0", "dayNtlVlm": "500000.0",
                 "openInterest": "500.0", "funding": "0.00002"}),       # -2.86%
        ("SOL", {"markPx": "140.0", "prevDayPx": "150.0", "dayNtlVlm": "200000.0",
                 "openInterest": "10000.0", "funding": "0.00003"}),     # -6.67% (down>5)
        ("ARB", {"markPx": "1.10", "prevDayPx": "1.20", "dayNtlVlm": "50000.0",
                 "openInterest": "50000.0", "funding": "0.00001"}),     # -8.33% (down>5)
    ]


# (a) well-formed JSONL line --------------------------------------------------

def test_writes_one_wellformed_jsonl_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CTX_SNAPSHOTS", True)
    monkeypatch.setattr(config, "CTX_SNAPSHOT_DIR", tmp_path)

    write_ctx_snapshot(_sample_assets())

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, "exactly one daily JSONL file"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "exactly one record per call"

    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "iso", "n_assets", "regime", "assets"}
    assert rec["n_assets"] == 4
    assert isinstance(rec["ts"], int)

    # assets block: numbers coerced to float, all five fields present for BTC.
    btc = rec["assets"]["BTC"]
    assert btc == {"markPx": 65000.0, "prevDayPx": 64000.0, "dayNtlVlm": 1000000.0,
                   "openInterest": 1000.0, "funding": 0.00001}

    # regime: BTC/ETH pulled from the same list; breadth computed over 4 assets.
    reg = rec["regime"]
    assert set(reg) == {"btc_px", "btc_chg_24h_pct", "eth_px", "eth_chg_24h_pct",
                        "pct_assets_down", "median_chg_24h_pct", "pct_assets_down_gt5"}
    assert reg["btc_px"] == 65000.0
    assert reg["btc_chg_24h_pct"] == pytest.approx((65000 / 64000 - 1) * 100)
    assert reg["eth_px"] == 3400.0
    assert reg["eth_chg_24h_pct"] == pytest.approx((3400 / 3500 - 1) * 100)
    # 3 of 4 down (ETH, SOL, ARB); 2 of 4 down >5% (SOL, ARB).
    assert reg["pct_assets_down"] == pytest.approx(75.0)
    assert reg["pct_assets_down_gt5"] == pytest.approx(50.0)
    assert reg["median_chg_24h_pct"] is not None


def test_skips_field_when_value_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CTX_SNAPSHOTS", True)
    monkeypatch.setattr(config, "CTX_SNAPSHOT_DIR", tmp_path)

    # openInterest missing, funding None → both omitted from the asset record.
    write_ctx_snapshot([("BTC", {"markPx": "65000.0", "prevDayPx": "64000.0",
                                 "dayNtlVlm": "1000000.0", "funding": None})])
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().strip())
    assert rec["assets"]["BTC"] == {"markPx": 65000.0, "prevDayPx": 64000.0,
                                    "dayNtlVlm": 1000000.0}


# (b) CRITICAL SAFETY: a write failure must NOT raise --------------------------

def test_write_failure_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CTX_SNAPSHOTS", True)
    monkeypatch.setattr(config, "CTX_SNAPSHOT_DIR", tmp_path)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)

    warnings: list = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        result = write_ctx_snapshot(_sample_assets())  # must not raise
    finally:
        logger.remove(sink)

    assert result is None
    assert any("non-fatal" in w for w in warnings), "failure must be logged"
    assert list(tmp_path.glob("*.jsonl")) == [], "nothing written on failure"


# (c) HOOK ISOLATION: snapshot failure must not touch self._cache -------------

class _FakeInfo:
    def __init__(self, payload):
        self._payload = payload

    def meta_and_asset_ctxs(self):
        return self._payload


def test_refresh_cache_survives_snapshot_failure(monkeypatch):
    meta = {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
    ctxs = [
        {"markPx": "65000.0", "prevDayPx": "64000.0", "dayNtlVlm": "1000000.0",
         "openInterest": "1000.0", "funding": "0.00001"},
        {"markPx": "3400.0", "prevDayPx": "3500.0", "dayNtlVlm": "500000.0",
         "openInterest": "500.0", "funding": "0.00002"},
    ]
    fetcher = UniverseFetcher(_FakeInfo((meta, ctxs)))

    def _boom(*a, **k):
        raise RuntimeError("snapshot exploded")

    # The hook does a local `from monitoring.ctx_snapshot import write_ctx_snapshot`,
    # so patching the module attribute is picked up at call time.
    monkeypatch.setattr("monitoring.ctx_snapshot.write_ctx_snapshot", _boom)

    fetcher._refresh()  # must not raise

    assert len(fetcher._cache) == 2, "cache populated despite snapshot failure"
    assert fetcher._cache[0][0] == "BTC"
    assert fetcher._cache[1][0] == "ETH"


# (d) flag off → no file -------------------------------------------------------

def test_flag_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CTX_SNAPSHOTS", False)
    monkeypatch.setattr(config, "CTX_SNAPSHOT_DIR", tmp_path)

    write_ctx_snapshot(_sample_assets())

    assert list(tmp_path.glob("*.jsonl")) == []
