"""
Unit tests — monitoring/tax_logger.py

Test IDs: TX1–TX6 from solvira_stress_test_master.md §3.11.
TX4: Bug #19 regression — multiple FX source fallback.
"""

import csv
import inspect
import time
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def tax_env(monkeypatch, tmp_path):
    """Redirect the module-level TAX_LOG_PATH to tmp and freeze the FX cache."""
    from monitoring import tax_logger as tx_mod
    csv_path = tmp_path / "tax.csv"
    monkeypatch.setattr(tx_mod, "TAX_LOG_PATH", csv_path)
    # Pre-fill cache so log_taxable_event doesn't try to hit the network
    tx_mod.USD_IDR_CACHE["rate"] = 16000.0
    tx_mod.USD_IDR_CACHE["fetched_at"] = time.time()
    return tx_mod, csv_path


# -----------------------------------------------------------------------------
# TX1 — log_taxable_event writes a CSV row
# -----------------------------------------------------------------------------
class TestLogTaxableEventCsv:
    def test_writes_csv_row(self, tax_env):
        tx_mod, csv_path = tax_env
        tx_mod.log_taxable_event("trade_close", "BTC", 100.0, pnl_usd=5.0)
        assert csv_path.exists()
        rows = list(csv.reader(csv_path.read_text().splitlines()))
        assert len(rows) == 2  # header + 1 data row
        header, data = rows[0], rows[1]
        assert "event_type" in header
        assert data[header.index("event_type")] == "trade_close"
        assert data[header.index("asset")] == "BTC"


# -----------------------------------------------------------------------------
# TX2 — Header written once even on append
# -----------------------------------------------------------------------------
class TestHeaderOnce:
    def test_header_not_duplicated_on_append(self, tax_env):
        tx_mod, csv_path = tax_env
        tx_mod.log_taxable_event("trade_close", "BTC", 100.0, pnl_usd=5.0)
        tx_mod.log_taxable_event("trade_close", "ETH", 50.0, pnl_usd=-2.0)

        rows = list(csv.reader(csv_path.read_text().splitlines()))
        assert len(rows) == 3  # 1 header + 2 data rows
        header_count = sum(1 for r in rows if r[0] == "timestamp_utc")
        assert header_count == 1


# -----------------------------------------------------------------------------
# TX3 — USD/IDR cache holds for 1 hour (one upstream call per window)
# -----------------------------------------------------------------------------
class TestFxCache:
    def test_fx_call_cached_within_hour(self, monkeypatch, tax_env):
        tx_mod, _ = tax_env
        # Invalidate the cache so the first call hits the network
        tx_mod.USD_IDR_CACHE["fetched_at"] = 0

        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json.return_value = {"rates": {"IDR": 16500.0}}
        mock_get = MagicMock(return_value=ok_resp)
        monkeypatch.setattr(tx_mod.requests, "get", mock_get)

        r1 = tx_mod._fetch_usd_idr()
        r2 = tx_mod._fetch_usd_idr()
        r3 = tx_mod._fetch_usd_idr()

        assert r1 == r2 == r3 == 16500.0
        # One network call total — subsequent reads served from cache
        assert mock_get.call_count == 1


# -----------------------------------------------------------------------------
# TX4 — 🟡 Bug #19 regression: multiple FX sources fallback
# -----------------------------------------------------------------------------
class TestBug19FxFallback:
    pytestmark = pytest.mark.regression

    def test_multiple_fx_sources_present(self):
        try:
            from monitoring import tax_logger
        except ImportError:
            pytest.skip("tax_logger not importable in test env")
        src = inspect.getsource(tax_logger)
        url_count = (
            src.count("https://api.exchangerate-api.com")
            + src.count("https://open.er-api.com")
            + src.count("https://api.frankfurter.app")
            + src.count("https://api.exchangerate.host")
        )
        assert url_count >= 2, "tax_logger must have >=2 FX source fallbacks"

    def test_fallback_used_when_primary_fails(self, monkeypatch, tax_env):
        """Primary source raises; secondary returns valid rate → use secondary."""
        import requests

        tx_mod, _ = tax_env
        tx_mod.USD_IDR_CACHE["fetched_at"] = 0  # force fresh fetch

        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json.return_value = {"rates": {"IDR": 16000.0}}

        mock_get = MagicMock(side_effect=[
            requests.exceptions.ConnectionError("primary down"),
            ok_resp,
        ])
        monkeypatch.setattr(tx_mod.requests, "get", mock_get)

        rate = tx_mod._fetch_usd_idr()
        assert rate == 16000.0
        assert mock_get.call_count == 2  # primary failed, secondary succeeded


# -----------------------------------------------------------------------------
# TX5 — FX sanity check: implausible rates rejected, plausible ones cached
# -----------------------------------------------------------------------------
class TestFxSanity:
    @pytest.mark.parametrize("rate,accepted", [
        (5_000.0, False),    # absurdly low
        (10_001.0, True),
        (16_000.0, True),
        (24_999.0, True),
        (50_000.0, False),   # absurdly high
    ])
    def test_rate_sanity_bounds(self, rate, accepted, monkeypatch, tax_env):
        tx_mod, _ = tax_env
        previous_rate = 17500.0
        tx_mod.USD_IDR_CACHE["rate"] = previous_rate
        tx_mod.USD_IDR_CACHE["fetched_at"] = 0  # force fresh fetch

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"rates": {"IDR": rate}}
        # All three sources return the same value
        monkeypatch.setattr(tx_mod.requests, "get", MagicMock(return_value=resp))

        result = tx_mod._fetch_usd_idr()
        if accepted:
            assert result == rate, "Plausible rate should be returned and cached"
            assert tx_mod.USD_IDR_CACHE["rate"] == rate
        else:
            # Implausible rate is rejected; the cached (previous) value persists
            assert result == previous_rate
            assert tx_mod.USD_IDR_CACHE["rate"] == previous_rate


# -----------------------------------------------------------------------------
# TX6 — Concurrent CSV writes don't corrupt
# -----------------------------------------------------------------------------
class TestConcurrentCsvWrites:
    def test_concurrent_writes_no_corruption(self, tmp_path):
        pytest.skip("Concurrency test — requires file lock in tax_logger, deferred")
