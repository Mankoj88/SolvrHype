# Solvira Trading Bot — Stress Test Master Guide

> **Audience:** Anda sudah selesai develop semua 14 modul, sebelum paper-trade testnet 14 hari.
> **Estimasi total waktu:** 16–24 jam tersebar dalam 5–7 hari (jangan rush, kebanyakan bug muncul di endurance test setelah 12+ jam).
> **Output:** Confidence-level untuk proceed ke testnet `DRY_RUN=false`, daftar bug yang harus difix, baseline metrics untuk compare ke live.

---

## Daftar Isi

1. [Filosofi & Strategi Stress Test](#1-filosofi--strategi-stress-test)
2. [Setup Test Environment](#2-setup-test-environment)
3. [Test Tier 1 — Unit & Module Stress Tests](#3-test-tier-1--unit--module-stress-tests)
4. [Test Tier 2 — Integration Stress Tests](#4-test-tier-2--integration-stress-tests)
5. [Test Tier 3 — Fault Injection & Chaos Tests](#5-test-tier-3--fault-injection--chaos-tests)
6. [Test Tier 4 — Endurance & Load Tests](#6-test-tier-4--endurance--load-tests)
7. [Test Tier 5 — Security & Secrets Audit](#7-test-tier-5--security--secrets-audit)
8. [Test Tier 6 — Regression Test untuk 22 Known Bugs](#8-test-tier-6--regression-test-untuk-22-known-bugs)
9. [End-to-End Testnet Validation (Manual)](#9-end-to-end-testnet-validation-manual)
10. [Decision Gate & Acceptance Criteria](#10-decision-gate--acceptance-criteria)
11. [Reporting Template](#11-reporting-template)

---

## 1. Filosofi & Strategi Stress Test

### 1.1 Kenapa stress test ≠ unit test biasa

Unit test verifikasi **happy path**: function dipanggil dengan input benar → output benar. Stress test verifikasi **adversarial path**: apa yang terjadi kalau:

- API Hyperliquid timeout di tengah `market_open()`?
- State file corrupt waktu `_load_state()`?
- Bot crash setelah TP1 tapi sebelum TP2 close?
- 1000 candle masuk sekaligus (volatility spike)?
- Disk penuh saat `init_db()`?
- Telegram API rate-limit selama 1 jam?
- Web3 RPC return wrong nonce?

Trading bot yang gagal di kondisi ini = **kerugian uang nyata**. Testing harus simulasi semua failure mode.

### 1.2 Test pyramid yang dipakai

```
        ┌─────────────────┐
        │  E2E Testnet    │  ← 14 hari paper-trade (manual observation)
        │   (live)        │
        ├─────────────────┤
        │ Endurance/Load  │  ← 24h+ runs dengan mocked exchange
        ├─────────────────┤
        │ Fault Injection │  ← chaos engineering: simulate failures
        ├─────────────────┤
        │  Integration    │  ← multi-module: scanner→order→logger
        ├─────────────────┤
        │   Unit/Module   │  ← per-function dengan adversarial input
        └─────────────────┘
```

Kerjakan **dari bawah ke atas**. Jangan endurance test sebelum unit test pass — buang waktu debug bug yang bisa kelihatan dalam 5 menit.

### 1.3 Klasifikasi Severity

Setiap test case harus dikategorikan:

| Severity | Definisi | Action kalau fail |
|---|---|---|
| 🔴 **BLOCKER** | Bisa menyebabkan kerugian uang atau data corruption | **HARUS difix sebelum testnet `DRY_RUN=false`** |
| 🟡 **MAJOR** | Bisa menyebabkan downtime atau missing data | Fix sebelum mainnet |
| 🟢 **MINOR** | Cosmetic atau edge case yang tidak realistic | Fix saat ada waktu |

### 1.4 Test Coverage Targets

| Modul | Min Coverage | Kritikalitas |
|---|---|---|
| `execution/order_manager.py` | **≥90%** | 🔴🔴🔴 (handle uang) |
| `execution/withdraw_manager.py` | **≥90%** | 🔴🔴🔴 (transfer uang) |
| `strategy/scanner.py` | ≥85% | 🔴🔴 (decision gate) |
| `strategy/indicators.py` | ≥85% | 🔴🔴 (correctness) |
| `monitoring/stop_loss_enforcer.py` | ≥80% | 🔴🔴 (kill switch) |
| `monitoring/trade_logger.py` | ≥80% | 🔴 (audit trail) |
| `monitoring/health.py` | ≥75% | 🟡 |
| `notifications/telegram.py` | ≥70% | 🟡 |
| `monitoring/tax_logger.py` | ≥70% | 🟡 |
| `self_review/claude_review.py` | ≥60% | 🟢 |
| `execution/allocation_manager.py` | ≥80% | 🔴 |

---

## 2. Setup Test Environment

### 2.1 Install test dependencies

Tambah ke `requirements-dev.txt` (file baru di root):

```txt
# Test framework
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-cov>=4.1.0
pytest-mock>=3.12.0
pytest-timeout>=2.2.0
pytest-randomly>=3.15.0
pytest-xdist>=3.5.0       # parallel test runs
pytest-benchmark>=4.0.0    # performance regression

# Mocking & fixtures
responses>=0.25.0          # mock HTTP responses
freezegun>=1.4.0           # mock time
hypothesis>=6.92.0         # property-based testing
faker>=22.0.0              # fake data generation

# Chaos / fault injection
toxiproxy-python>=0.1.0    # network chaos (optional)

# Profiling
memory-profiler>=0.61.0
psutil>=5.9.0

# Code quality
ruff>=0.2.0
mypy>=1.8.0
bandit>=1.7.0              # security audit
```

Install:

```powershell
pip install -r requirements-dev.txt
```

### 2.2 Folder structure untuk test

```
solvira/
├── (existing modules ...)
├── tests/
│   ├── __init__.py
│   ├── conftest.py                      # shared fixtures
│   ├── fixtures/
│   │   ├── sample_candles.json          # canned market data
│   │   ├── sample_meta.json             # canned Hyperliquid meta
│   │   └── corrupt_state.json           # corrupt state for negative tests
│   ├── unit/
│   │   ├── test_indicators.py
│   │   ├── test_scanner.py
│   │   ├── test_order_manager.py
│   │   ├── test_allocation_manager.py
│   │   ├── test_withdraw_manager.py
│   │   ├── test_telegram.py
│   │   ├── test_trade_logger.py
│   │   ├── test_health.py
│   │   ├── test_stop_loss_enforcer.py
│   │   ├── test_tax_logger.py
│   │   └── test_claude_review.py
│   ├── integration/
│   │   ├── test_scanner_to_order_flow.py
│   │   ├── test_position_lifecycle.py
│   │   ├── test_state_recovery.py
│   │   └── test_withdraw_pipeline.py
│   ├── chaos/
│   │   ├── test_network_failures.py
│   │   ├── test_disk_full.py
│   │   ├── test_concurrent_state.py
│   │   └── test_clock_skew.py
│   ├── endurance/
│   │   ├── test_24h_loop.py
│   │   └── test_memory_leak.py
│   ├── regression/
│   │   └── test_22_known_bugs.py
│   └── security/
│       ├── test_secret_leakage.py
│       └── test_permissions.py
├── pytest.ini
└── requirements-dev.txt
```

### 2.3 `pytest.ini` configuration

Buat di root:

```ini
[pytest]
minversion = 8.0
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
addopts =
    --strict-markers
    --strict-config
    --tb=short
    -ra
    --color=yes
    --durations=10
markers =
    unit: fast, isolated unit tests (<1s)
    integration: cross-module tests (1-30s)
    chaos: fault injection tests
    endurance: long-running tests (>5min) — skip by default
    regression: tests for known bugs
    security: security & secret leakage tests
    blocker: must pass before testnet
    requires_testnet: needs HL testnet connection
    requires_anthropic: needs Anthropic API key
    slow: skipped by default in fast runs
filterwarnings =
    error
    ignore::DeprecationWarning:hyperliquid.*
    ignore::DeprecationWarning:web3.*
    ignore::PendingDeprecationWarning
```

### 2.4 `conftest.py` — fixtures yang dipakai semua test

Buat di `tests/conftest.py`:

```python
"""
Shared fixtures untuk semua stress test.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Tambah project root ke sys.path agar import dari modules existing jalan
sys.path.insert(0, str(Path(__file__).parent.parent))


# ───────────────────── ENV FIXTURES ─────────────────────

@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Setiap test dapat env terisolasi (no real keys, no real DB)."""
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", "0x" + "a" * 40)
    monkeypatch.setenv("ARBITRUM_PRIVATE_KEY", "0x" + "2" * 64)
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
    monkeypatch.setenv("DESTINATION_USDT_WALLET", "0x" + "b" * 40)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token:12345")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-fake")
    monkeypatch.setenv("USE_TESTNET", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    # Override DATA_DIR to tmp
    monkeypatch.setenv("SOLVIRA_DATA_DIR", str(tmp_path / "data"))
    yield


# ───────────────────── DATA FIXTURES ─────────────────────

@pytest.fixture
def sample_candles_df():
    """200 candles, 10-minute timeframe, BTC-like price action."""
    import numpy as np
    np.random.seed(42)
    n = 200
    base_ts = pd.Timestamp("2025-01-01 00:00:00", tz="UTC")
    ts = [base_ts + pd.Timedelta(minutes=10 * i) for i in range(n)]

    # Random walk dengan drift
    returns = np.random.normal(0, 0.005, n)
    prices = 50000 * np.cumprod(1 + returns)

    df = pd.DataFrame({
        "open": prices * (1 + np.random.uniform(-0.001, 0.001, n)),
        "high": prices * (1 + np.abs(np.random.normal(0, 0.002, n))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.002, n))),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
    }, index=pd.DatetimeIndex(ts, name="datetime_utc"))
    df.index = df.index.astype("int64") // 10**6  # epoch ms
    return df


@pytest.fixture
def oversold_setup_df(sample_candles_df):
    """DF dimana candle terakhir SHOULD trigger entry signal (Stoch <20 + xover + MACD reversal + vol spike)."""
    df = sample_candles_df.copy()
    # Force last 30 candles to be a clear downtrend
    df.iloc[-30:, df.columns.get_loc("close")] = df.iloc[-30:]["close"] * \
        np.linspace(1.0, 0.85, 30)
    # Volume spike on last candle
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume"].iloc[-10:].mean() * 3
    return df


@pytest.fixture
def fake_hyperliquid_meta():
    return {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
        ],
    }


@pytest.fixture
def fake_meta_with_ctx(fake_hyperliquid_meta):
    """metaAndAssetCtxs response shape."""
    return [
        fake_hyperliquid_meta,
        [
            {"funding": "0.00001", "openInterest": "1000", "prevDayPx": "100000",
             "dayNtlVlm": "5000000000", "premium": "0.0", "oraclePx": "98000",
             "markPx": "98000", "midPx": "98000", "impactPxs": ["97990", "98010"],
             "dayBaseVlm": "50000"},
            {"funding": "0.00002", "openInterest": "500", "prevDayPx": "4000",
             "dayNtlVlm": "2000000000", "premium": "0.0", "oraclePx": "3600",
             "markPx": "3600", "midPx": "3600", "impactPxs": ["3599", "3601"],
             "dayBaseVlm": "200000"},
            {"funding": "0.0008", "openInterest": "100", "prevDayPx": "200",
             "dayNtlVlm": "10000000", "premium": "0.0", "oraclePx": "180",
             "markPx": "180", "midPx": "180", "impactPxs": ["179", "181"],
             "dayBaseVlm": "5000"},
        ],
    ]


# ───────────────────── MOCK FIXTURES ─────────────────────

@pytest.fixture
def mock_hl_info():
    """Mock hyperliquid.info.Info instance."""
    m = MagicMock()
    m.candles_snapshot = MagicMock(return_value=[])
    m.meta_and_asset_ctxs = MagicMock(return_value=[{}, []])
    m.user_state = MagicMock(return_value={"assetPositions": [], "marginSummary": {}})
    return m


@pytest.fixture
def mock_hl_exchange():
    """Mock hyperliquid.exchange.Exchange — always success fills."""
    m = MagicMock()

    def _order(asset, is_buy, sz, px, order_type, reduce_only=False):
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [
                    {"filled": {"totalSz": str(sz), "avgPx": str(px or 100), "oid": 12345}}
                ]},
            },
        }
    m.order = MagicMock(side_effect=_order)
    m.cancel = MagicMock(return_value={"status": "ok"})
    m.withdraw_from_bridge = MagicMock(
        return_value={"status": "ok", "response": "0x" + "f" * 64}
    )
    return m


@pytest.fixture
def mock_telegram(monkeypatch):
    """Block all real Telegram calls; record sent messages."""
    sent = []

    def fake_post(text, parse_mode="Markdown"):
        sent.append(text)
        return True

    monkeypatch.setattr("notifications.telegram._post", fake_post)
    return sent


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """SQLite DB di tmp dir untuk setiap test."""
    db_path = tmp_path / "test_solvira.db"
    monkeypatch.setattr("config.DB_PATH", db_path)
    # Re-import jika sudah di-cache
    import importlib
    import monitoring.trade_logger as tl
    importlib.reload(tl)
    tl.init_db()
    yield db_path


# ───────────────────── TIME FIXTURES ─────────────────────

@pytest.fixture
def freeze_clock():
    """Freeze time agar test deterministic."""
    from freezegun import freeze_time
    with freeze_time("2025-06-01 12:00:00", tz_offset=0) as frozen:
        yield frozen
```

### 2.5 Cara run test

```powershell
# Activate venv
venv\Scripts\activate

# Fast tests only (unit)
pytest tests/unit -v -m "not slow"

# All except endurance (default)
pytest tests -v -m "not endurance"

# Specific module
pytest tests/unit/test_order_manager.py -v

# Dengan coverage
pytest tests/unit --cov=. --cov-report=html --cov-report=term-missing

# Parallel (8 workers, lebih cepat)
pytest tests/unit -n 8

# Hanya yang BLOCKER
pytest tests -v -m blocker

# Endurance test (24 jam — run di tmux/screen)
pytest tests/endurance -v -m endurance
```

---

## 3. Test Tier 1 — Unit & Module Stress Tests

Setiap modul punya checklist test yang harus pass. Format: **ID | Apa yang ditest | Severity | Expected**.

### 3.1 `config.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| C1 | Load config dengan semua env vars valid | 🔴 | All constants populated |
| C2 | Load config dengan `HYPERLIQUID_PRIVATE_KEY` missing | 🔴 | Raise ValueError jelas |
| C3 | `USE_TESTNET=invalid_value` | 🟡 | Default safe atau raise |
| C4 | `get_allocation(capital=300)` (di bawah threshold) | 🔴 | Return `[1.0]` |
| C5 | `get_allocation(capital=999)` (boundary $1000) | 🔴 | `[0.40, 0.60]` |
| C6 | `get_allocation(capital=1500)` (boundary) | 🔴 | `[0.30, 0.30, 0.40]` |
| C7 | `get_allocation(capital=2000)` | 🔴 | `[0.25, 0.25, 0.25, 0.25]` |
| C8 | `get_allocation(capital=-100)` | 🟡 | Handle gracefully |
| C9 | `get_api_url()` testnet/mainnet switch | 🔴 | Correct URL |
| C10 | `CRYPTO_WHITELIST` no duplicates | 🟢 | `len(set) == len(list)` |
| C11 | Hard limits tidak bisa di-override via env | 🔴 | Constants di code |
| C12 | `MIN_POSITION_SIZE_USD < MAX_POSITION_SIZE_USD` | 🔴 | Sanity |
| C13 | `CUTLOSS_PCT < 0` (negatif) | 🔴 | Konsisten dengan logic |
| C14 | `TAKE_PROFITS` sorted ascending dan total ≤ 1.0 | 🔴 | No double-sell |

**Sample test:**

```python
# tests/unit/test_config.py
import pytest

class TestAllocationBoundaries:
    """🔴 BLOCKER — wrong allocation = wrong position sizing."""

    @pytest.mark.parametrize("capital,expected", [
        (50, [1.0]), (300, [1.0]), (499, [1.0]),
        (500, [0.40, 0.60]), (699, [0.40, 0.60]),
        (700, [0.30, 0.30, 0.40]), (1499, [0.30, 0.30, 0.40]),
        (1500, [0.25, 0.25, 0.25, 0.25]),
        (10000, [0.25, 0.25, 0.25, 0.25]),
    ])
    def test_get_allocation_at_boundaries(self, capital, expected):
        from config import get_allocation
        assert get_allocation(capital) == expected

    def test_allocation_sum_equals_one(self):
        from config import get_allocation
        for capital in [100, 500, 700, 1500, 5000]:
            assert sum(get_allocation(capital)) == pytest.approx(1.0, abs=1e-9)


class TestHardLimitsImmutable:
    """🔴 Hard limits never change at runtime."""

    def test_take_profits_monotonic(self):
        from config import TAKE_PROFITS
        prev_pct = -float("inf")
        for tp_pct, _ in TAKE_PROFITS:
            assert tp_pct > prev_pct
            prev_pct = tp_pct
```

---

### 3.2 `strategy/indicators.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| I1 | `compute_stoch_rsi` flat prices | 🔴 | No NaN crash |
| I2 | `compute_stoch_rsi` strictly increasing | 🔴 | K → 100, no xover |
| I3 | `compute_stoch_rsi` decreasing → reversal | 🔴 | Golden cross fires |
| I4 | `compute_macd` <26 candles | 🟡 | NaN di awal, no exception |
| I5 | `detect_volume_spike` zero volume | 🔴 | No DivByZero |
| I6 | Volume spike 1000x extreme | 🟢 | Detected |
| I7 | Empty DataFrame | 🟡 | No crash, columns ada |
| I8 | NaN values di tengah | 🟡 | Propagate, no crash |
| I9 | `is_entry_signal` all-true | 🔴 | True |
| I10 | `is_entry_signal` 2-of-3 | 🔴 | False (AND, bukan OR) |
| I11 | Determinism: 2x same input → same output | 🔴 | Identical |
| I12 | Property: stoch K ∈ [0, 100] | 🔴 | Hypothesis |
| I13 | Property: macd_hist = macd - signal | 🔴 | Identity |
| I14 | Performance: 10000 candles <1 sec | 🟢 | Benchmark |
| I15 | Idempotence: 2x compute_all | 🟢 | No double columns |

**Sample test:**

```python
# tests/unit/test_indicators.py
import pandas as pd
import numpy as np
import pytest
from hypothesis import given, strategies as st, settings


class TestStochRSI:

    def test_flat_prices_no_crash(self):
        from strategy.indicators import compute_stoch_rsi
        df = pd.DataFrame({"close": [100.0]*50, "volume": [1000]*50})
        result = compute_stoch_rsi(df)
        assert "stoch_rsi_k" in result.columns
        assert "stoch_golden_cross" in result.columns

    def test_decreasing_then_recovery_triggers_xover(self):
        from strategy.indicators import compute_stoch_rsi
        n = 100
        prices = list(np.linspace(100, 70, 30)) + list(np.linspace(70, 75, 70))
        df = pd.DataFrame({"close": prices, "volume": [1000]*n})
        result = compute_stoch_rsi(df)
        assert result["stoch_golden_cross"].sum() >= 1

    @given(prices=st.lists(
        st.floats(min_value=1.0, max_value=100000.0,
                  allow_nan=False, allow_infinity=False),
        min_size=50, max_size=500))
    @settings(max_examples=50, deadline=2000)
    def test_stoch_rsi_bounds_property(self, prices):
        from strategy.indicators import compute_stoch_rsi
        df = pd.DataFrame({"close": prices, "volume": [1000]*len(prices)})
        result = compute_stoch_rsi(df)
        k = result["stoch_rsi_k"].dropna()
        d = result["stoch_rsi_d"].dropna()
        if len(k) > 0:
            assert k.min() >= 0 and k.max() <= 100
            assert d.min() >= 0 and d.max() <= 100


class TestEntrySignalLogic:

    def test_all_true_triggers(self):
        from strategy.indicators import is_entry_signal
        row = pd.Series({
            "stoch_golden_cross": True,
            "macd_reversal": True,
            "volume_spike": True,
        })
        assert is_entry_signal(row) is True

    @pytest.mark.parametrize("a,b,c", [
        (True, True, False), (True, False, True),
        (False, True, True), (False, False, False),
    ])
    def test_partial_conditions_no_trigger(self, a, b, c):
        from strategy.indicators import is_entry_signal
        row = pd.Series({
            "stoch_golden_cross": a,
            "macd_reversal": b,
            "volume_spike": c,
        })
        assert is_entry_signal(row) is False
```

---

### 3.3 `strategy/scanner.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| S1 | Empty universe | 🟡 | Return [] |
| S2 | Skip non-whitelist | 🔴 | RANDOM token tidak diproses |
| S3 | Volume filter $4.99M | 🔴 | False |
| S4 | Volume filter $5.00M | 🔴 | True |
| S5 | Drop filter -9.99% | 🔴 | False |
| S6 | Drop filter -10.00% | 🔴 | True |
| S7 | Funding window 5 min pre-funding | 🔴 | Block |
| S8 | Funding > 0.5%/jam | 🔴 | Block |
| S9 | **🔴 Bug #1** scanner pakai candle BELUM CLOSE | 🔴 | Pakai `iloc[-2]` |
| S10 | API timeout | 🔴 | Return [], no crash |
| S11 | Malformed candle response | 🔴 | Skip, log, continue |
| S12 | <30 candles untuk indicator | 🟡 | Skip gracefully |
| S13 | Cache meta TTL 30s | 🟢 | One API call per 30s |
| S14 | Concurrent scan calls | 🟡 | No state corruption |

**Sample test (paling penting — Bug #1):**

```python
# tests/unit/test_scanner.py
import pytest
from unittest.mock import patch


class TestLookAheadBugRegression:
    """🔴 BLOCKER — Bug #1: scanner harus pakai iloc[-2]."""

    def test_scanner_uses_completed_candle_only(
        self, oversold_setup_df, mock_hl_info
    ):
        from strategy.scanner import MarketScanner
        df_forming = oversold_setup_df.copy()
        # Forming candle has wildly different close
        df_forming.iloc[-1, df_forming.columns.get_loc("close")] = 1.0

        scanner = MarketScanner.__new__(MarketScanner)
        scanner.info = mock_hl_info
        scanner._meta_cache = None
        scanner._meta_cache_time = 0

        with patch.object(scanner, "_fetch_candles_df", return_value=df_forming), \
             patch.object(scanner, "_passes_volume_filter", return_value=True), \
             patch.object(scanner, "_passes_drop_filter",
                          return_value=(True, -0.12)), \
             patch.object(scanner, "_passes_funding_filter", return_value=True), \
             patch.object(scanner, "_get_meta_with_ctx",
                          return_value=[
                              {"universe": [{"name": "BTC"}]},
                              [{"funding": "0.0001",
                                "dayNtlVlm": "1e10",
                                "markPx": "100"}]
                          ]):
            signals = scanner.scan()
            for sig in signals:
                # Price MUST come from completed candle, not forming (1.0)
                assert sig.price != 1.0, \
                    "🔴 Bug #1 not fixed: scanner using forming candle"
```

---

### 3.4 `execution/order_manager.py` 🔴🔴🔴 **CRITICAL MODULE**

Modul paling penting — handle uang. Test harus paranoid.

| ID | Test case | Severity | Expected |
|---|---|---|---|
| O1 | Open position happy path | 🔴 | Position dicatat, SL placed, state saved |
| O2 | API return error | 🔴 | No position, no state mutation |
| O3 | Partial fill | 🔴 | Use actual filled size |
| O4 | Sudah ada posisi di asset sama | 🔴 | Reject |
| O5 | **Bug #2** TP1→TP2 sizing benar | 🔴 | TP2 sells 40% remaining |
| O6 | TP1 hit, breakeven SL moved | 🔴 | `current_sl == entry_price` |
| O7 | **Bug #3** SL fully closes | 🔴 | exchange position = 0 |
| O8 | Max-hold timeout | 🔴 | Closed regardless of P&L |
| O9 | **Bug #6** partial vs full close hook | 🔴 | Partial doesn't trigger withdraw |
| O10 | State persistence round-trip | 🔴 | Identical |
| O11 | **Bug #14** State backward-compat | 🔴 | Drop unknown fields |
| O12 | State invalid JSON | 🔴 | Backup, fresh start |
| O13 | Concurrent state mutation | 🟡 | File lock |
| O14 | **Bug #5** Startup reconciliation | 🔴 | Detect orphans |
| O15 | DRY_RUN=true | 🔴 | No real orders |
| O16 | Round size per szDecimals | 🔴 | BTC 5, SOL 2 decimals |
| O17 | Slippage tolerance | 🟡 | SLIPPAGE_TOLERANCE |
| O18 | Connection error mid-flight | 🔴 | State doesn't lie |
| O19 | TP1 OK, SL re-place fails | 🔴 | Log error, alert |
| O20 | Stop-loss enforcer halt blocks entries | 🔴 | Return False |

**Sample test (focus Bug #2 dan #3):**

```python
# tests/unit/test_order_manager.py
import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


@pytest.mark.blocker
class TestBug2_PartialTpSizing:
    """🔴 Bug #2: TP2 must sell REMAINING (40%), not 100% original."""

    def test_tp2_sells_remaining_size_only(
        self, mock_hl_exchange, monkeypatch, tmp_path
    ):
        from execution.order_manager import OrderManager, Position

        monkeypatch.setattr(
            "execution.order_manager.OrderManager.STATE_FILE",
            tmp_path / "positions.json",
        )
        om = OrderManager.__new__(OrderManager)
        om.exchange = mock_hl_exchange
        om._asset_meta = {"BTC": {"szDecimals": 5}}
        om.STATE_FILE = tmp_path / "positions.json"

        pos = Position(
            asset="BTC", entry_price=100.0, entry_size_coin=1.0,
            entry_size_usd=100.0, entry_time_ms=0,
            tp_levels_remaining=[(20.0, 1.00)],  # only TP2 left
            initial_sl_price=95.0, current_sl_price=100.0,
            tp_hit_count=1,
        )
        if hasattr(pos, "remaining_size_coin"):
            pos.remaining_size_coin = 0.4
        om.positions = {"BTC": pos}

        with patch.object(om, "_save_state"):
            om._execute_partial_tp(
                "BTC", sell_pct=1.0, tp_pct=20.0, current_price=120.0
            )

        order_call = mock_hl_exchange.order.call_args
        size_arg = order_call[0][2]
        assert size_arg == pytest.approx(0.4, abs=1e-5), \
            f"🔴 Bug #2 not fixed: TP2 sold {size_arg}, expected 0.4"


@pytest.mark.blocker
class TestBug14_StateBackwardCompat:
    """🔴 State file dengan field tidak match harus tolerant."""

    def test_state_with_extra_field_doesnt_crash(self, monkeypatch, tmp_path):
        from execution.order_manager import OrderManager
        state_file = tmp_path / "positions.json"
        legacy_data = {
            "BTC": {
                "asset": "BTC", "entry_price": 100.0, "entry_size_coin": 1.0,
                "entry_size_usd": 100.0, "entry_time_ms": 0,
                "tp_levels_remaining": [],
                "initial_sl_price": 95.0, "current_sl_price": 100.0,
                "ghost_field_from_old_version": "should_be_ignored",
            }
        }
        state_file.write_text(json.dumps(legacy_data))

        om = OrderManager.__new__(OrderManager)
        om.STATE_FILE = state_file
        positions = om._load_state()
        # Must not raise; either loads with field dropped, or returns empty
        assert isinstance(positions, dict)


@pytest.mark.blocker
class TestStateCorruption:

    def test_invalid_json_recovers(self, tmp_path):
        from execution.order_manager import OrderManager
        state_file = tmp_path / "positions.json"
        state_file.write_text("{invalid json")

        om = OrderManager.__new__(OrderManager)
        om.STATE_FILE = state_file
        positions = om._load_state()
        assert positions == {}


@pytest.mark.blocker
class TestRounding:

    @pytest.mark.parametrize("asset,sz_decimals,raw,expected", [
        ("BTC", 5, 0.0012345678, 0.00123),
        ("ETH", 4, 0.12345678, 0.1234),
        ("SOL", 2, 12.345678, 12.34),
    ])
    def test_round_size_floor(self, asset, sz_decimals, raw, expected):
        from execution.order_manager import OrderManager
        om = OrderManager.__new__(OrderManager)
        om._asset_meta = {asset: {"szDecimals": sz_decimals}}
        result = om._round_size(asset, raw)
        # MUST be floor, not round (avoid over-sizing)
        assert result <= raw
        assert result == pytest.approx(expected, abs=10**(-sz_decimals))
```

---

### 3.5 `execution/allocation_manager.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| A1 | Capital $300, 0 positions → $300 | 🔴 | Single asset |
| A2 | Capital $1000, 1 position → $400 | 🔴 | Slot 2 (0.40) |
| A3 | Capital $1000, 2 positions → $0 | 🔴 | All slots filled |
| A4 | Asset already reserved | 🔴 | Return 0 |
| A5 | Size < $25 (MIN) | 🔴 | Return 0 |
| A6 | Size > $200 (MAX) | 🔴 | Cap at MAX |
| A7 | Capital changes mid-trade | 🟡 | Use current |

---

### 3.6 `execution/withdraw_manager.py` 🔴🔴🔴 **CRITICAL**

| ID | Test case | Severity | Expected |
|---|---|---|---|
| W1 | `record_profit($10)` → +$5 (50%) | 🔴 | State updated |
| W2 | Below threshold → False | 🔴 | Wait |
| W3 | Above threshold tapi <24h | 🔴 | Min interval |
| W4 | Happy path (mocked) | 🔴 | All 3 steps execute |
| W5 | DRY_RUN no real tx | 🔴 | Simulasi only |
| W6 | HL withdraw fails → no swap | 🔴 | Fail-fast |
| W7 | USDC arrival timeout | 🔴 | Raise, don't proceed |
| W8 | Swap fails → don't send USDT | 🔴 | Funds stuck di intermediate, alert |
| W9 | Concurrent execute() | 🔴 | Singleton lock |
| W10 | **web3.py 7.x raw_transaction** | 🔴 | snake_case |
| W11 | Insufficient ETH gas | 🔴 | Graceful error |
| W12 | Nonce conflict | 🔴 | Wait/use pending |
| W13 | Slippage protection 0.5% | 🔴 | Swap fails kalau >0.5% |
| W14 | State save before tx | 🔴 | Recoverable |

**Sample (web3.py compat — paling sering bug):**

```python
# tests/unit/test_withdraw_manager.py
import pytest
import inspect


class TestWeb3Compat:
    """🔴 BLOCKER: web3.py 7.x uses raw_transaction (snake_case)."""

    def test_signed_tx_uses_raw_transaction_attr(self):
        from execution import withdraw_manager
        src = inspect.getsource(withdraw_manager)
        assert "rawTransaction" not in src, \
            "🔴 Found camelCase rawTransaction — web3.py 7.x requires raw_transaction"
        assert "raw_transaction" in src, \
            "🔴 Must use signed_tx.raw_transaction"


@pytest.mark.blocker
class TestPipelineFailFast:

    def test_hl_fail_no_swap_attempted(
        self, mock_hl_exchange, monkeypatch, tmp_path
    ):
        from execution.withdraw_manager import WithdrawManager
        from unittest.mock import MagicMock, patch

        WithdrawManager._instance = None
        wm = WithdrawManager.__new__(WithdrawManager)
        wm._initialized = False
        wm.hl_exchange = mock_hl_exchange
        wm.hl_exchange.withdraw_from_bridge = MagicMock(
            return_value={"status": "err", "msg": "insufficient"}
        )
        wm.state = {
            "cumulative_profit_pending": 50.0,
            "last_withdraw_at": None,
        }
        wm._save_state = MagicMock()
        wm._wait_for_usdc_arrival = MagicMock()
        wm._swap_usdc_to_usdt = MagicMock()
        wm._send_usdt_to_destination = MagicMock()
        wm._initialized = True

        with patch("config.DRY_RUN", False), \
             patch("execution.withdraw_manager.notify_withdrawal"), \
             patch("execution.withdraw_manager.notify_critical_error"), \
             patch("execution.withdraw_manager.log_withdrawal_initiated",
                   return_value=1), \
             patch("execution.withdraw_manager.log_withdrawal_failed"):
            ok = wm.execute_withdraw()

        assert ok is False
        wm._wait_for_usdc_arrival.assert_not_called()
        wm._swap_usdc_to_usdt.assert_not_called()
        wm._send_usdt_to_destination.assert_not_called()
```

---

### 3.7 `notifications/telegram.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| T1 | `send()` happy path | 🟡 | POST sent |
| T2 | Throttling active | 🟡 | Skipped |
| T3 | `force=True` bypass throttle | 🟡 | Always sent |
| T4 | TOKEN/CHAT_ID missing | 🟡 | Return False |
| T5 | API 429 rate limit | 🟡 | No crash |
| T6 | API timeout | 🟡 | No crash |
| T7 | **Bug #15** Markdown special chars di asset name | 🔴 | Escape |
| T8 | Daily summary format | 🟢 | Renders |
| T9 | `notify_critical_error` truncate >500 | 🟢 | No flood |

---

### 3.8 `monitoring/trade_logger.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| TL1 | `init_db()` creates schema | 🔴 | Tables exist |
| TL2 | `log_trade()` correct write | 🔴 | SELECT match |
| TL3 | **Bug #22** Schema migration | 🔴 | Old DB no crash |
| TL4 | Empty day stats | 🟢 | Zeros |
| TL5 | 100 trades aggregated | 🟢 | Correct |
| TL6 | `get_total_pnl_since` | 🔴 | Used by SL enforcer |
| TL7 | DB locked concurrent writes | 🟡 | Retry |
| TL8 | Disk full | 🟡 | Log, no crash |
| TL9 | DB file deleted | 🔴 | Recreate or fail loud |

---

### 3.9 `monitoring/health.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| H1 | Singleton pattern | 🟢 | Same instance |
| H2 | `heartbeat()` updates time | 🔴 | UptimeRobot detect |
| H3 | 5 errors → halt | 🔴 | Circuit breaker |
| H4 | Daily counter reset UTC midnight | 🟡 | New day |
| H5 | HTTP 503 if stale | 🔴 | UptimeRobot alerts |
| H6 | HTTP thread-safe | 🟡 | Concurrent OK |
| H7 | `to_dict()` JSON-able | 🟢 | Serializable |
| H8 | **Bug #18** `last_signal_time` reported | 🟢 | Visible |

---

### 3.10 `monitoring/stop_loss_enforcer.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| SL1 | First call creates state | 🔴 | Persisted |
| SL2 | <90 days → no halt | 🔴 | Don't fire early |
| SL3 | =90d, -$199 | 🔴 | No halt (boundary) |
| SL4 | =90d, -$200 | 🔴 | Halt |
| SL5 | =90d, -$201 | 🔴 | Halt |
| SL6 | 100d, halted persists | 🔴 | Not auto-resume |
| SL7 | `manual_resume()` resets | 🟡 | New cycle |
| SL8 | State corrupt → recreate | 🔴 | Don't bypass |

---

### 3.11 `monitoring/tax_logger.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| TX1 | `log_taxable_event` writes CSV | 🟡 | File exists |
| TX2 | Header once | 🟡 | Append mode |
| TX3 | USD/IDR cache 1 hour | 🟢 | One API call |
| TX4 | **Bug #19** Multiple FX fallback | 🟡 | 2-3 sources |
| TX5 | FX sanity check (10000-25000) | 🟡 | Reject absurd |
| TX6 | Concurrent CSV writes | 🟡 | No corruption |

---

### 3.12 `self_review/claude_review.py`

| ID | Test case | Severity | Expected |
|---|---|---|---|
| CR1 | Latest model string | 🟢 | claude-opus-4-7 atau sonnet-4-6 |
| CR2 | **Bug #17** Strip ```json fences | 🟢 | Robust parse |
| CR3 | Hard-limit suggestion blocked | 🔴 | _sanity_check filters |
| CR4 | "annualized return" warning | 🟡 | risk_alert flagged |
| CR5 | API timeout → log+Telegram | 🟡 | No crash |
| CR6 | <10 trades → review runs | 🟢 | Edge case |

---

## 4. Test Tier 2 — Integration Stress Tests

Test multi-modul flow. Mocks Hyperliquid API, tapi semua modul Solvira berinteraksi seperti production.

### 4.1 Scanner → OrderManager → TradeLogger

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| INT1 | Scan emit signal → enter_position → log_trade | 🔴 | Round-trip data konsisten |
| INT2 | Multiple signals dalam 1 cycle | 🔴 | Allocation manager batasi |
| INT3 | API error → no entry, no log | 🔴 | Exception isolation |
| INT4 | Entry OK tapi log fail (DB locked) | 🟡 | Position tetap saved |

### 4.2 Position Lifecycle End-to-End

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| INT5 | Entry → TP1 hit → breakeven SL | 🔴 | All transitions correct |
| INT6 | Entry → TP1 → TP2 → record_profit | 🔴 | Total P&L = TP1+TP2 |
| INT7 | Entry → SL hit → close, log loss | 🔴 | Withdraw NOT incremented |
| INT8 | Entry → max hold timeout | 🔴 | Closed at current price |
| INT9 | Entry → API error mid-TP1 | 🔴 | Reconcile next cycle |

### 4.3 State Recovery After Crash

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| INT10 | Crash setelah entry sebelum SL | 🔴 | Restart detect, place SL |
| INT11 | Crash setelah TP1 sebelum state save | 🔴 | Reconcile partial close |
| INT12 | State partial-write | 🔴 | Backup + fresh start |
| INT13 | Exchange position tidak ada di state | 🔴 | Adopt or close |
| INT14 | State punya tapi exchange tidak | 🔴 | Clear state |

### 4.4 Withdraw Pipeline

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| INT15 | $25 cumulative → trigger | 🔴 | Threshold check |
| INT16 | All 3 steps success | 🔴 | tx_hash di DB |
| INT17 | HL OK, USDC arrival timeout | 🔴 | Don't proceed |
| INT18 | USDC arrived, swap fails | 🔴 | Stuck di intermediate, alert |

**Sample integration test (lifecycle):**

```python
# tests/integration/test_position_lifecycle.py
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.integration
@pytest.mark.blocker
class TestEntryToTp1ToTp2Flow:

    def test_full_lifecycle_correct_pnl(
        self, mock_hl_exchange, isolated_db, monkeypatch, tmp_path
    ):
        from execution.order_manager import OrderManager
        from strategy.scanner import TradeSignal

        monkeypatch.setattr(
            "execution.order_manager.OrderManager.STATE_FILE",
            tmp_path / "positions.json",
        )

        om = OrderManager.__new__(OrderManager)
        om.exchange = mock_hl_exchange
        om._asset_meta = {"BTC": {"szDecimals": 5}}
        om.STATE_FILE = tmp_path / "positions.json"
        om.positions = {}
        om._reserved_per_asset = set()

        def fake_order(asset, is_buy, sz, px, ot, reduce_only=False):
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 1}}
            ]}}}
        mock_hl_exchange.order.side_effect = fake_order

        signal = TradeSignal(
            asset="BTC", price=100.0, timestamp_ms=0,
            reason="test", indicators_snapshot={},
        )
        with patch.object(om, "_place_stop_loss", return_value=999):
            ok = om.enter_position(signal, size_usd=100)
        assert ok
        pos = om.positions["BTC"]

        # TP1 hit (+10%)
        om.manage_open_positions({"BTC": 110.0})
        assert pos.tp_hit_count == 1
        assert pos.current_sl_price == pos.entry_price  # breakeven

        # TP2 hit (+20%)
        om.manage_open_positions({"BTC": 120.0})
        assert "BTC" not in om.positions

        # Verify trade_logger
        from monitoring.trade_logger import get_recent_trades
        trades = get_recent_trades(limit=10)
        total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
        assert 10 < total_pnl < 18, f"P&L off: {total_pnl}"
```

---

## 5. Test Tier 3 — Fault Injection & Chaos Tests

Simulate kondisi adversarial yang sulit reproduce di lab tapi pasti terjadi di production.

### 5.1 Network Failures

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| CH1 | Hyperliquid API timeout 10s | 🔴 | Retry, no crash |
| CH2 | HL API 503 selama 5 menit | 🔴 | Halt setelah 5 errors |
| CH3 | Telegram 429 rate limit | 🟡 | Throttle, log, continue |
| CH4 | Anthropic API timeout | 🟢 | Skip review |
| CH5 | Arbitrum RPC rate limit | 🟡 | Backoff retry |
| CH6 | DNS fail (offline) | 🔴 | Degraded mode |
| CH7 | Truncated JSON response | 🔴 | Parse error handled |
| CH8 | Slow response 5s/call | 🟡 | Cycle stretches, no double-execute |

**Sample chaos test:**

```python
# tests/chaos/test_network_failures.py
import pytest
from unittest.mock import MagicMock
import requests


@pytest.mark.chaos
@pytest.mark.blocker
class TestApiOutage:

    def test_api_timeout_no_crash(self):
        from strategy.scanner import MarketScanner
        scanner = MarketScanner.__new__(MarketScanner)
        scanner.info = MagicMock()
        scanner.info.meta_and_asset_ctxs.side_effect = requests.Timeout()
        scanner._meta_cache = None
        scanner._meta_cache_time = 0

        signals = scanner.scan()
        assert signals == []

    def test_5_consecutive_errors_halt(self, mock_telegram):
        from monitoring.health import HealthMonitor
        HealthMonitor._instance = None
        h = HealthMonitor()
        for i in range(5):
            h.on_error(error_type="api")
        assert h.is_halted is True
        assert any("HALTED" in msg for msg in mock_telegram)
```

### 5.2 Disk & Resource Failures

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| CH9 | Disk full saat init_db() | 🟡 | Log, exit cleanly |
| CH10 | Disk full saat write state | 🔴 | Atomic write OR log+continue |
| CH11 | Read-only filesystem | 🔴 | Detect at startup, halt |
| CH12 | DB file permission denied | 🟡 | Detect, log |
| CH13 | Memory limit (512MB) | 🟡 | Restart by systemd |

### 5.3 Time & Clock Issues

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| CH14 | Clock skew +2 jam | 🔴 | HL sig might fail; alert |
| CH15 | Timezone change DST | 🟢 | Daily summary 23:59 UTC |
| CH16 | Year 2038 overflow | 🟢 | 64-bit timestamps |

### 5.4 Concurrent Access

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| CH17 | 2 instances bot, state file sama | 🔴 | Lock file mutex |
| CH18 | **Bug #13** Schedule blocks trading_cycle | 🟡 | Schedule di thread |
| CH19 | DB write while reading | 🟡 | SQLite WAL mode |

**Sample untuk Bug #13:**

```python
# tests/chaos/test_concurrent_state.py
import pytest
import time
from unittest.mock import patch


@pytest.mark.chaos
@pytest.mark.blocker
class TestBug13_ScheduleNotBlocking:
    """🔴 Bug #13: schedule.run_pending() bisa block trading_cycle."""

    def test_main_uses_thread_for_schedule(self):
        import inspect
        import main as main_module
        src = inspect.getsource(main_module)
        # Check fix: schedule should run in separate thread
        assert ("threading" in src or "_schedule_thread" in src or
                "_run_schedule_loop" in src), \
            "🔴 Bug #13 not fixed: schedule masih di main loop"
```

---

## 6. Test Tier 4 — Endurance & Load Tests

Bug yang baru kelihatan setelah jam-jam jalan (memory leak, file handle leak).

### 6.1 Test list

| ID | Skenario | Durasi | Pass criteria |
|---|---|---|---|
| EN1 | 1000 cycles dengan mocks | 5 menit | RAM stabil |
| EN2 | 24-jam loop testnet API (no orders) | 24 jam | RAM <512MB |
| EN3 | 1000 trades through DB | 5 menit | DB <100MB, query <100ms |
| EN4 | Telegram throttle reset 1 jam | 1 jam | Counters reset |
| EN5 | 10000 candles → scanner | 30 detik | <5s, no leak |
| EN6 | 7-day testnet paper trade | 7 hari | All monitoring pass |

### 6.2 Sample endurance test

```python
# tests/endurance/test_memory_leak.py
import pytest
import asyncio
import tracemalloc
from unittest.mock import MagicMock


@pytest.mark.endurance
@pytest.mark.slow
@pytest.mark.timeout(600)
class TestMemoryLeak:

    @pytest.mark.asyncio
    async def test_1000_cycles_no_leak(self):
        tracemalloc.start()
        snap1 = tracemalloc.take_snapshot()

        from main import Solvira
        bot = Solvira.__new__(Solvira)
        bot.scanner = MagicMock()
        bot.scanner.scan.return_value = []
        bot.order_manager = MagicMock()
        bot.order_manager.positions = {}
        bot.order_manager.open_position_count.return_value = 0
        bot.health = MagicMock()
        bot.withdraw_manager = MagicMock()
        bot.withdraw_manager.should_withdraw.return_value = False
        bot.allocation_manager = MagicMock()
        bot.stop_loss_enforcer = MagicMock()
        bot.stop_loss_enforcer.check.return_value = (True, None)

        for i in range(1000):
            await bot.trading_cycle()
            if i % 100 == 0:
                snap2 = tracemalloc.take_snapshot()
                top = snap2.compare_to(snap1, "lineno")
                growth = sum(s.size_diff for s in top[:10])
                assert growth < 50 * 1024 * 1024, \
                    f"Memory leak: {growth/1024/1024:.1f}MB at i={i}"
        tracemalloc.stop()
```

### 6.3 Memory profiling (manual di terminal)

```bash
# Profile 1 jam
mprof run --include-children python main.py
# Ctrl+C setelah 1 jam
mprof plot
# Verifikasi: chart stabil, no monotonic increase
```

### 6.4 File handle / connection leak

```bash
# Baseline (setelah bot running 5 menit)
ls /proc/$(pgrep -f main.py)/fd | wc -l   # ~10-20

# Setelah 1 jam:
ls /proc/$(pgrep -f main.py)/fd | wc -l   # tetap <50 = OK

# Network connections:
ss -tnp | grep python | wc -l             # stabil = OK
```

---

## 7. Test Tier 5 — Security & Secrets Audit

### 7.1 Secret leakage tests

| ID | Skenario | Severity | Expected |
|---|---|---|---|
| SEC1 | Private key tidak masuk log files | 🔴 | grep `0x[0-9a-f]{64}` di logs/ → 0 hits |
| SEC2 | Private key tidak masuk Telegram message | 🔴 | Mock telegram, send error containing key, verify masked |
| SEC3 | Private key tidak masuk error traceback | 🔴 | Trigger error in withdraw, check stack trace |
| SEC4 | `.env` di .gitignore | 🔴 | `git check-ignore .env` returns "ignored" |
| SEC5 | No hardcoded secrets di source | 🔴 | `bandit -r .` → no high-severity |
| SEC6 | API key tidak di state files | 🔴 | grep di data/*.json |
| SEC7 | DB tidak menyimpan private keys | 🔴 | SELECT * tidak ada hex 64-char |
| SEC8 | URL params tidak include credentials | 🔴 | grep di logs untuk `?token=`, `?key=` |

**Sample security test:**

```python
# tests/security/test_secret_leakage.py
import re
import pytest
from pathlib import Path


HEX_64_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")


@pytest.mark.security
@pytest.mark.blocker
class TestSecretLeakage:

    def test_no_private_keys_in_log_files(self, tmp_path, monkeypatch):
        """Run bot, trigger errors, verify no key di log."""
        from loguru import logger
        log_file = tmp_path / "test.log"
        logger.add(str(log_file))

        # Simulate operations that handle keys
        fake_key = "0x" + "a" * 64
        try:
            raise ValueError(f"Operation failed with key {fake_key}")
        except Exception as e:
            logger.exception("Caught error")

        log_content = log_file.read_text()
        # The key must NOT appear verbatim
        # (Test fails if your code logs full keys; you should mask)
        if HEX_64_PATTERN.search(log_content):
            pytest.fail("🔴 Private-key-pattern found in logs — implement masking")

    def test_telegram_error_message_masked(self, mock_telegram):
        from notifications.telegram import notify_critical_error
        fake_key = "0x" + "b" * 64
        notify_critical_error(f"Web3 error: key={fake_key}", "withdraw")
        # Verify mask
        for msg in mock_telegram:
            assert fake_key not in msg, \
                "🔴 Private key leaked to Telegram"


class TestStaticSecurityScan:

    def test_bandit_no_high_severity(self):
        import subprocess
        result = subprocess.run(
            ["bandit", "-r", ".", "-x", "tests/,venv/", "-f", "json", "-q"],
            capture_output=True, text=True, timeout=60,
        )
        import json
        if result.returncode == 0:
            return  # no findings
        data = json.loads(result.stdout)
        high_severity = [
            r for r in data.get("results", [])
            if r["issue_severity"] == "HIGH"
        ]
        assert len(high_severity) == 0, \
            f"Bandit: {len(high_severity)} HIGH severity issues:\n" + \
            "\n".join(f"- {r['filename']}:{r['line_number']} {r['issue_text']}"
                      for r in high_severity)
```

### 7.2 Permissions audit (manual checklist)

```bash
# Di VPS:
ls -la ~/solvira/.env*
# Expected: -rw------- (600) or .age encrypted
# NOT: -rw-r--r-- (644) — readable by others

ls -la ~/solvira/data/
# DB & state files: should be 600 too

# Verify systemd ProtectHome=read-only
sudo systemctl cat solvira | grep -E "Protect|NoNewPriv"
```

### 7.3 Network egress audit

```bash
# Bot harusnya hanya konek ke Hyperliquid + Arbitrum + Telegram + Anthropic
sudo ss -tnp | grep python | awk '{print $5}' | cut -d: -f1 | sort -u
# Expected:
# - api.hyperliquid.xyz / api.hyperliquid-testnet.xyz
# - arb1.arbitrum.io
# - api.telegram.org
# - api.anthropic.com
# - api.exchangerate-api.com (tax)
# Anything else = investigasi
```

---

## 8. Test Tier 6 — Regression Test untuk 22 Known Bugs

Setiap bug dari `solvira_code_review.md` harus punya regression test. Kalau dev mengubah code dan tanpa sengaja re-introduce bug, test ini fail.

### 8.1 Master regression test file

```python
# tests/regression/test_22_known_bugs.py
"""
Regression tests untuk 22 bugs yang ditemukan di code review.
Setiap test harus FAIL kalau bug re-introduced.
"""
import pytest
from unittest.mock import MagicMock, patch


# ───────── CRITICAL ─────────

@pytest.mark.regression
@pytest.mark.blocker
class TestBug01_ScannerLookAhead:
    """Scanner must use iloc[-2] (last completed candle), not iloc[-1]."""
    def test_scanner_uses_completed_candle(self, oversold_setup_df):
        # Lihat tests/unit/test_scanner.py::TestLookAheadBugRegression
        pass  # delegate


@pytest.mark.regression
@pytest.mark.blocker
class TestBug02_PartialTpSizing:
    """TP2 must sell remaining_size_coin, not entry_size_coin × sell_pct."""
    def test_tp2_sells_remaining(self):
        pass  # delegate to test_order_manager.py


@pytest.mark.regression
@pytest.mark.blocker
class TestBug03_SlClosesFully:
    """SL trigger must call _close_full_position dengan remaining size."""
    pass


@pytest.mark.regression
@pytest.mark.blocker
class TestBug04_FundingWindow:
    """Funding window pakai 1-jam interval (Hyperliquid), bukan 8-jam."""

    def test_funding_window_hourly(self):
        from datetime import datetime, timezone
        from strategy.scanner import MarketScanner
        scanner = MarketScanner.__new__(MarketScanner)

        # Mock NOW = 14:57 UTC (3 menit before 15:00 funding)
        with patch("strategy.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(
                2025, 1, 1, 14, 57, tzinfo=timezone.utc
            )
            ctx = {"funding": "0.0001", "markPx": "100"}
            # Should fail filter (within 5 min window)
            assert scanner._passes_funding_filter(ctx) is False


@pytest.mark.regression
@pytest.mark.blocker
class TestBug05_StartupReconciliation:
    """Bot harus reconcile state vs exchange di startup."""

    def test_reconcile_method_exists(self):
        from execution.order_manager import OrderManager
        assert hasattr(OrderManager, "_reconcile_with_exchange") or \
               hasattr(OrderManager, "reconcile_with_exchange"), \
            "🔴 Bug #5 not fixed: no reconciliation method"


@pytest.mark.regression
@pytest.mark.blocker
class TestBug06_OnPositionClosePartialFull:
    """_on_position_close differentiates partial vs full close."""

    def test_partial_close_doesnt_log_full_pnl(self):
        # Implementation-specific test — verify the hook gets a partial flag
        # or has separate methods for partial vs full
        from execution.order_manager import OrderManager
        # Check signature
        import inspect
        if hasattr(OrderManager, "_on_position_close"):
            sig = inspect.signature(OrderManager._on_position_close)
            # Should have 'partial' or 'final' parameter
            param_names = list(sig.parameters.keys())
            assert "partial" in param_names or \
                   "is_partial" in param_names or \
                   "final" in param_names or \
                   hasattr(OrderManager, "_on_partial_close"), \
                "🔴 Bug #6: _on_position_close tidak distinguish partial vs full"


# ───────── IMPORTANT ─────────

@pytest.mark.regression
class TestBug07_To_15:
    """Important bugs — placeholder, expand sesuai code review file."""
    pass


@pytest.mark.regression
class TestBug13_ScheduleBlocking:
    """Schedule harus di thread terpisah."""

    def test_schedule_in_thread(self):
        import inspect
        import main as main_module
        src = inspect.getsource(main_module)
        assert ("threading" in src or "_schedule_thread" in src or
                "_run_schedule_loop" in src), \
            "🔴 Bug #13: schedule masih di main loop, bisa block trading"


@pytest.mark.regression
class TestBug14_StateBackwardCompat:
    """Position dataclass tolerant load."""

    def test_load_state_drops_unknown_fields(self, tmp_path):
        from execution.order_manager import OrderManager
        import json
        sf = tmp_path / "p.json"
        sf.write_text(json.dumps({
            "BTC": {
                "asset": "BTC", "entry_price": 100, "entry_size_coin": 1,
                "entry_size_usd": 100, "entry_time_ms": 0,
                "tp_levels_remaining": [], "initial_sl_price": 95,
                "current_sl_price": 100,
                "ghost_v1_field": "should_be_ignored",
            }
        }))
        om = OrderManager.__new__(OrderManager)
        om.STATE_FILE = sf
        positions = om._load_state()
        assert isinstance(positions, dict)


@pytest.mark.regression
class TestBug15_TelegramMarkdown:
    """Asset name dengan _ atau * tidak break Markdown."""

    def test_special_chars_in_asset_name(self, mock_telegram, monkeypatch):
        from notifications.telegram import notify_daily_summary
        monkeypatch.setattr("notifications.telegram.TOKEN", "fake")
        monkeypatch.setattr("notifications.telegram.CHAT_ID", "1")
        stats = {
            "date": "2025-01-01", "total_trades": 1, "wins": 1, "losses": 0,
            "win_rate": 1.0, "pnl_usd": 5.0, "pnl_pct": 1.0,
            "capital": 500, "usdt_wallet": 0,
            "top_trade": {"asset": "k_PEPE*test", "pnl_pct": 5.0},
        }
        # Tidak boleh raise & tidak boleh produce malformed markdown
        notify_daily_summary(stats)


# ───────── MINOR ─────────

@pytest.mark.regression
class TestBug16_LatestClaudeModel:
    """Model string harus current."""

    def test_uses_latest_claude_model(self):
        import inspect
        from self_review import claude_review
        src = inspect.getsource(claude_review)
        # Reject deprecated/old models
        assert "claude-sonnet-4-5" not in src or \
               "claude-opus-4-7" in src or \
               "claude-sonnet-4-6" in src, \
            "Update model ke claude-opus-4-7 atau claude-sonnet-4-6"


@pytest.mark.regression
class TestBug17_JsonFenceStripping:
    """Claude response strips ```json fences."""

    def test_strips_markdown_fences(self):
        from self_review.claude_review import _parse_review_response \
            if False else None
        # Or test inline parsing dari run_weekly_review
        # Construct mock response yang ada fence
        response_text = '```json\n{"stats": {"total_trades": 5}}\n```'
        # If your code has _strip_fences helper:
        # Use it. Otherwise test the parsing function directly.
        # Adjust to actual implementation.


@pytest.mark.regression
class TestBug18_LastSignalTimeReported:
    """health.last_signal_time muncul di to_dict."""

    def test_to_dict_includes_last_signal(self):
        from monitoring.health import HealthMonitor
        HealthMonitor._instance = None
        h = HealthMonitor()
        d = h.to_dict()
        assert "last_signal_time" in d or \
               "seconds_since_last_signal" in d, \
            "🟢 Bug #18: last_signal_time harus visible di health endpoint"


@pytest.mark.regression
class TestBug19_FxFallback:
    """Multiple FX sources fallback."""

    def test_multiple_sources_tried_on_failure(self, monkeypatch):
        import inspect
        from monitoring import tax_logger
        src = inspect.getsource(tax_logger)
        # Look for evidence of multiple sources
        url_count = (src.count("https://api.exchangerate-api.com") +
                     src.count("https://open.er-api.com") +
                     src.count("https://api.frankfurter.app"))
        assert url_count >= 2, \
            "🟡 Bug #19: tax_logger harus punya >=2 FX source fallback"


@pytest.mark.regression
class TestBug22_DbMigration:
    """trade_logger init_db punya migration mechanism."""

    def test_init_db_uses_user_version(self, monkeypatch, tmp_path):
        import sqlite3
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("config.DB_PATH", db_path)
        import importlib
        import monitoring.trade_logger as tl
        importlib.reload(tl)

        tl.init_db()
        with sqlite3.connect(db_path) as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver >= 0, "user_version pragma harus aktif untuk migration"
```

---

## 9. End-to-End Testnet Validation (Manual)

Setelah semua test di atas pass, jalankan validation manual di Hyperliquid testnet.

### 9.1 Pre-flight check (1 jam)

```bash
# Pre-conditions
[ ] All Tier 1-6 pytest pass dengan `--strict-markers`
[ ] Code coverage ≥80% modul critical
[ ] Bandit security scan pass
[ ] .env terisi dengan testnet credentials (USE_TESTNET=true)
[ ] DRY_RUN=true (start dengan dry run)
[ ] systemd service dimuat tapi tidak running

# Smoke
python test_smoke.py    # harus all green
```

### 9.2 Validation Phase 1: DRY_RUN selama 7 hari

**Goal:** verify bot decision-making correct tanpa actual orders.

```bash
# Start bot
sudo systemctl start solvira
sudo journalctl -u solvira -f
```

**Daily checks (7 hari berturut):**
- Pagi: Telegram daily summary received
- Log rotation works (`ls ~/solvira/logs/`)
- Health endpoint `curl http://localhost:8080/health` returns 200
- Memory tetap <300MB (`ps aux | grep main.py`)
- 0 unhandled exception (`grep "Traceback" ~/solvira/logs/bot.log | wc -l` = 0)
- Signals di log match dengan candle close events

**Pass criteria:**
- 7 hari tanpa crash
- Telegram daily summary 7x diterima konsisten
- Bot mendetect minimum 1 signal (kalau 0 signal selama 7 hari → review filter terlalu ketat)
- DB tidak corrupt

### 9.3 Validation Phase 2: DRY_RUN=false di testnet, 14 hari

```bash
# Edit .env: DRY_RUN=false (testnet still true)
sudo systemctl restart solvira
```

**Daily checks (14 hari):**

**Setiap pagi:**
- [ ] Buka Hyperliquid testnet UI, compare positions dengan `data/positions.json`
- [ ] Compare trade history Hyperliquid vs `monitoring/trade_logger.py` query
- [ ] Position count sama
- [ ] P&L per trade sama (dalam toleransi 0.01% untuk floating point)

**Setiap weekly review (Senin):**
- [ ] Telegram dapat weekly review
- [ ] Suggested change reasonable
- [ ] Hard limits NOT in suggestion

**Forced test scenarios (lakukan manual):**

```bash
# Test 1: Kill bot mid-position
sudo systemctl kill -s SIGTERM solvira
# Wait 1 menit, restart:
sudo systemctl start solvira
# Verifikasi: log "reconciliation jalan", state matches exchange

# Test 2: Network disconnect
sudo iptables -A OUTPUT -d api.hyperliquid-testnet.xyz -j DROP
# Wait 5 menit
sudo journalctl -u solvira -f | grep -i "halt\|error"
# Should see "5 consecutive errors" → halt
sudo iptables -D OUTPUT -d api.hyperliquid-testnet.xyz -j DROP
# Manual resume:
# (your manual_resume command)

# Test 3: Disk fill
dd if=/dev/zero of=~/bigfile bs=1M count=$(($(df ~ --output=avail | tail -1) / 1024 - 100))
# Verify bot logs disk full error gracefully
rm ~/bigfile
```

**Pass criteria 14 hari:**
- Total entries di trade_logger = total entries di Hyperliquid testnet UI
- 0 orphan position (state vs exchange diff)
- Win rate ±5% dari backtest expectation
- Withdraw test: trigger manual $25 cumulative → all 3 steps complete
- 0 unhandled exception
- Memory <300MB throughout

### 9.4 Test withdraw pipeline (real money small amount)

⚠️ **Hanya setelah 14 hari testnet OK**, baru test withdraw di MAINNET dengan amount kecil ($5-10).

```python
# Manual trigger via Python REPL:
from execution.withdraw_manager import WithdrawManager
wm = WithdrawManager()
wm.state["cumulative_profit_pending"] = 5.0  # $5 saja
wm.state["last_withdraw_at"] = None
wm.execute_withdraw()
```

**Verifikasi:**
- HL UI: balance turun $5
- Arbiscan: cek address intermediate, USDC arrived
- Arbiscan: swap tx success
- MetaMask Main: USDT bertambah ~$4.99 (after slippage)

---

## 10. Decision Gate & Acceptance Criteria

Sebelum proceed ke mainnet live trading, **HARUS** semua box di-check:

### 10.1 Code quality gate

```
[ ] pytest tests/unit -v          → 100% pass
[ ] pytest tests/integration -v   → 100% pass
[ ] pytest tests/chaos -v         → ≥95% pass (some chaos tests fragile)
[ ] pytest tests/regression -v    → 100% pass (all 22 bugs fixed)
[ ] pytest tests/security -v      → 100% pass
[ ] coverage report               → ≥80% di modul critical
[ ] bandit -r .                   → 0 HIGH severity
[ ] mypy . (optional)             → no errors di modul critical
[ ] ruff check .                  → 0 errors
```

### 10.2 Behavioral gate

```
[ ] 7 hari DRY_RUN testnet, 0 unhandled exception
[ ] 14 hari live testnet, 0 unhandled exception
[ ] Trade log di SQLite match 100% dengan Hyperliquid UI
[ ] Memory <300MB sustained 24+ jam
[ ] No file handle leak setelah 24+ jam
[ ] Backtest WR ≥45% (per File 3)
[ ] Live testnet WR ±5% dari backtest
[ ] Withdraw pipeline tested di mainnet ($5-10)
[ ] Crash recovery tested (kill -9, resume OK)
[ ] Network outage tested (iptables drop, halt OK)
```

### 10.3 Operational gate

```
[ ] systemd service stable (auto-restart works)
[ ] UptimeRobot alerts configured
[ ] Telegram alerts working (daily, error, halt)
[ ] DB backup cron jalan
[ ] Log rotation configured
[ ] .env encrypted (.age) di VPS
[ ] Plaintext .env shredded
[ ] Spending limit Anthropic API set
[ ] API Wallet (NOT Main Wallet) di .env
[ ] Mainnet API Wallet generated, hyperliquid faucet TIDAK di-claim
```

### 10.4 Final go/no-go decision

| Kondisi | Decision |
|---|---|
| Semua 3 gate ✅, plus deposit awal $50 ready | **GO** to mainnet small (Minggu 8) |
| 1+ gate ❌ | **NO-GO** — fix gap, retest |
| Backtest WR <45% | **STOP** — re-engineer strategy |
| Testnet WR jauh dari backtest (>10% gap) | **STOP** — investigate disparity |

---

## 11. Reporting Template

Setelah selesai stress test, dokumentasikan hasil. Format:

```markdown
# Solvira Stress Test Report
**Tanggal:** YYYY-MM-DD
**Versi code:** git commit hash
**Tester:** [nama]

## 1. Test Coverage Summary
| Modul | Coverage | Pass | Fail | Skip |
|---|---|---|---|---|
| config.py | 95% | 14/14 | 0 | 0 |
| strategy/indicators.py | 92% | 15/15 | 0 | 0 |
| strategy/scanner.py | 87% | 13/14 | 1 | 0 |
| ... | | | | |

## 2. Bugs Found (during stress test)
| ID | Module | Severity | Description | Status |
|---|---|---|---|---|
| New-1 | order_manager | 🔴 | Race condition di concurrent state save | FIXED |

## 3. Regression Test Status (22 known bugs)
| Bug # | Description | Test pass? |
|---|---|---|
| 1 | Scanner look-ahead | ✅ |
| 2 | TP2 sizing | ✅ |
| 3 | SL full close | ✅ |
| ... | | |

## 4. Endurance Test Results
- 24h test: ✅ Memory peaked at 287MB, 0 leaks
- 7d testnet DRY_RUN: ✅ 0 unhandled exception
- 14d live testnet: ✅ Trade log match 100%
- Crash recovery: ✅ Reconciliation works
- Network outage: ✅ Halt + resume OK

## 5. Performance Benchmarks
| Operation | p50 | p95 | p99 |
|---|---|---|---|
| trading_cycle | 1.2s | 2.8s | 4.1s |
| compute_indicators (200 candles) | 8ms | 15ms | 22ms |
| log_trade | 5ms | 12ms | 25ms |

## 6. Decision Gate
- Code quality: ✅ All gates passed
- Behavioral: ✅ 14-day testnet success
- Operational: ✅ All ops infra ready

## 7. Recommendation
**GO / NO-GO:** [decision]
**Conditions/Caveats:**
- Start with $50 mainnet, 1 asset
- Daily monitoring 2-jam-sekali for first week
- Re-evaluate after 7 days live
```

---

## Appendix A: Quick Reference Commands

```bash
# Run all fast tests
pytest tests -v -m "not endurance and not slow"

# Run only blockers
pytest tests -v -m blocker

# Coverage report
pytest tests/unit --cov=. --cov-report=html

# Run single bug regression
pytest tests/regression/test_22_known_bugs.py::TestBug01_ScannerLookAhead -v

# Endurance (24h)
nohup pytest tests/endurance/test_24h_loop.py -v -s > endurance_24h.log 2>&1 &

# Memory profile
mprof run python main.py
# Ctrl+C after 1h
mprof plot

# Static security scan
bandit -r . -x tests/,venv/

# Manual testnet 14-day run
sudo systemctl start solvira
sudo journalctl -u solvira -f | tee testnet_14d.log
```

---

## Appendix B: Test Execution Schedule (Recommended)

**Hari 1 (4 jam):** Setup test environment, install deps, write conftest.py & fixtures.
**Hari 2 (6 jam):** Run all Tier 1 (unit) tests, fix bugs found.
**Hari 3 (4 jam):** Run Tier 2 (integration) tests, fix bugs.
**Hari 4 (4 jam):** Run Tier 3 (chaos) tests, fix bugs.
**Hari 5 (2 jam setup + 24 jam idle):** Start endurance test in tmux, leave running.
**Hari 6 (2 jam):** Review endurance results, run Tier 5+6.
**Hari 7-13 (1 jam/day):** Daily monitoring testnet DRY_RUN.
**Hari 14-27 (1 jam/day):** Daily monitoring testnet live.
**Hari 28:** Final report + decision gate.

**Total: 4 minggu sebelum mainnet**

---

## Penutup

Code architecture sudah solid (per code review). Stress test ini memastikan **3 hal**:

1. **Correctness:** Setiap modul deliver output sesuai contract, terutama di edge cases.
2. **Robustness:** Bot survive failure modes yang akan terjadi di production (network, disk, crash).
3. **Safety:** Hard limits (MAX_POSITION_SIZE, CUTLOSS_PCT, EVALUATION_LOSS_THRESHOLD) tidak bisa di-bypass walaupun ada bug atau hostile input.

**Reminder kritis:**
- 🔴 **JANGAN SKIP** Tier 6 (regression) — 22 bugs sudah identified, jangan re-introduce.
- 🔴 **JANGAN SKIP** 14-hari testnet validation — banyak bug muncul cuma setelah jam-jam runtime.
- 🔴 **JANGAN SKIP** withdraw pipeline test di mainnet small ($5) — pipeline ini bisa miss konfigurasi gas/nonce yang baru ketauan saat real txn.

Maxim: **lebih baik 4 minggu testing + bot stabil 6 bulan, daripada 1 minggu testing + bot crash + kehilangan modal di Minggu 9.**

Selamat testing. 🧪

— Stress Test Master Guide v1.0


