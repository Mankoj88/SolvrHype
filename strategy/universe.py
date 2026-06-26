"""
UniverseFetcher: ambil whitelist asset dinamis dari Hyperliquid Info API.

Per spec user:
- Skip market cap filter (Hyperliquid tidak expose), pakai 7-day avg volume
  sebagai proxy likuiditas (dilakukan di Strategy, bukan di sini).
- Whitelist seluruh ekosistem HYPE (~150-200 perp aset).

Cache 1 jam supaya tidak hammer API tiap cycle (60s).
"""
import time
from loguru import logger
from hyperliquid.info import Info
from config import get_api_url, UNIVERSE_REFRESH_INTERVAL_SECONDS


class UniverseFetcher:
    """
    Sumber tunggal untuk daftar (asset, ctx) tradeable di Hyperliquid perp.
    `ctx` adalah dict yang mengandung markPx, openInterest, dayNtlVlm, funding.
    """

    def __init__(self, info: Info = None):
        self._info = info or Info(get_api_url(), skip_ws=True)
        self._cache: list[tuple[str, dict]] = []
        self._cache_time: float = 0
        # Last _cache_time already snapshotted — dedup guard so one refresh does
        # not double-write (see _refresh hook below). Purely observability state.
        self._last_snapshot_cache_time: float = 0

    def iter_assets(self):
        """Generator yield (asset_name, ctx). Refresh otomatis jika cache expired."""
        if not self._cache or (time.time() - self._cache_time) > UNIVERSE_REFRESH_INTERVAL_SECONDS:
            self._refresh()
        yield from self._cache

    def get_ctx(self, asset: str) -> dict:
        """Ambil ctx terbaru untuk asset spesifik. Trigger refresh jika perlu."""
        for name, ctx in self.iter_assets():
            if name == asset:
                return ctx
        return {}

    def _refresh(self):
        try:
            meta, contexts = self._info.meta_and_asset_ctxs()
        except Exception as e:
            logger.warning(f"Universe refresh failed: {e}. Reusing stale cache ({len(self._cache)} aset)")
            return

        universe = meta.get("universe", [])
        result = []
        for asset_info, ctx in zip(universe, contexts):
            asset = asset_info.get("name")
            if not asset:
                continue
            # Skip aset yang di-delist (isDelisted true) jika field tersebut ada
            if asset_info.get("isDelisted"):
                continue
            result.append((asset, ctx))

        self._cache = result
        self._cache_time = time.time()
        logger.info(f"Universe refreshed: {len(result)} aset tradeable")

        # Best-effort market-context snapshot for offline backtesting. Fully
        # isolated: the cache is already set above, and write_ctx_snapshot never
        # raises, but the try/except is a hard belt-and-suspenders guarantee that
        # NOTHING here can affect refresh / scan / entries. Dedup guard skips a
        # repeat write for a cache_time already snapshotted by this instance.
        if getattr(self, "_last_snapshot_cache_time", None) != self._cache_time:
            try:
                from monitoring.ctx_snapshot import write_ctx_snapshot
                write_ctx_snapshot(result)
            except Exception as e:
                logger.warning(f"ctx snapshot hook failed (non-fatal): {e}")
            finally:
                self._last_snapshot_cache_time = self._cache_time
