"""
Solvira Phase A — Crypto Only Configuration
HARDCODED LIMITS marked [HARD LIMIT] tidak bisa di-override oleh AI/auto-tune.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === API CREDENTIALS ===
HYPERLIQUID_PRIVATE_KEY = os.getenv("HYPERLIQUID_PRIVATE_KEY")  # API Wallet, BUKAN main
HYPERLIQUID_ACCOUNT = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")  # Main wallet address
ARBITRUM_PRIVATE_KEY = os.getenv("ARBITRUM_PRIVATE_KEY")
ARBITRUM_RPC_URL = os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
DESTINATION_USDT_WALLET = os.getenv("DESTINATION_USDT_WALLET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# === ENVIRONMENT ===
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# === MARKET FILTERS (per spesifikasi user) ===
MIN_DAILY_VOLUME_USD = 100_000   # >$5M/hari di Hyperliquid
MIN_DAILY_DROP_PCT = 2.0            # >2% penurunan vs hari sebelumnya
# Market cap filter DIABAIKAN per spesifikasi user

# Spot universe/scan volume gate per spec: "24h volume > $500,000".
# Replaces the old 7d-avg 100k proxy for SPOT scanning (derivative path unchanged).
SCAN_MIN_24H_VOLUME_USD = 500_000

# === INDICATORS ===
TIMEFRAME = "10m"
CANDLE_LOOKBACK = 100
STOCH_RSI_OVERSOLD = 20
STOCH_RSI_LENGTH = 10
STOCH_RSI_K_SMOOTH = 5
STOCH_RSI_D_SMOOTH = 5
# MACD tuned for spot per spec: fast=10, slow=30, signal=10 (was 12/26/9).
MACD_FAST = 10
MACD_SLOW = 30
MACD_SIGNAL = 10
VOLUME_SPIKE_LOOKBACK = 3           # 3 candle sebelumnya (legacy/deriv path)
VOLUME_SPIKE_MULTIPLIER = 1.5

# Parabolic SAR (runner exit gate past max_hold)
PSAR_STEP = 0.02
PSAR_MAX_STEP = 0.2
PSAR_HOLD_MAX_HOURS = 24  # absolute ceiling beyond 6h to prevent infinite hold

# === SPOT WINDOWED-CONFIRMATION PARAMS (closed candles only) ===
# Conditions 2-4 confirm over a short window of recent CLOSED candles instead
# of demanding all three line up on the single entry candle (the old 0-trades
# bug). All windows count back from the latest closed candle (iloc[-2]).
STOCH_CROSS_LOOKBACK = 3      # golden cross may occur within last N closed candles
MACD_HIST_RISING_BARS = 1     # histogram must be rising for at least this many bars
VOLUME_SMA_PERIOD = 20        # baseline SMA period for volume
VOLUME_SPIKE_WINDOW = 48      # spike must have occurred within last N closed candles (30-60)

# === EXECUTION (DEPRECATED — kept for backward compat during phase 1-3 rollout) ===
TAKE_PROFITS = [
    (10.0, 0.60),  # +10% sell 60% of remaining (= 60% of original at TP1)
    (20.0, 1.00),  # +20% sell 100% of remaining (= 40% of original, since TP1 sold 60%)
]
CUTLOSS_PCT = -5.0
USE_BREAKEVEN_AFTER_TP1 = True
MAX_HOLD_HOURS = 6
SLIPPAGE_TOLERANCE = 0.005

# === DUAL-STRATEGY (new dual spot+derivative architecture) ===
STRATEGY_POOL_SPLIT = {"spot": 0.50, "derivative": 0.50}
MAX_OPEN_POSITIONS_PER_STRATEGY = {"spot": 3, "derivative": 2}
ENABLE_DERIVATIVE_STRATEGY = os.getenv("ENABLE_DERIVATIVE", "false").lower() == "true"
# Universe ctx (dayNtlVlm, prevDayPx, funding) berubah terus, harus fresh tiap
# cycle. Cycle = 60s, jadi cache 60s = 1 API call/menit untuk universe.
UNIVERSE_REFRESH_INTERVAL_SECONDS = 60

# === LOOP CADENCE (two concurrent async loops) ===
SCAN_CYCLE_SECONDS = 300         # spot scan + management base loop runs every 5 min
POSITION_MANAGE_INTERVAL_SECONDS = 60   # position management loop
DERIVATIVE_SCAN_EVERY_N_CYCLES = 2      # 2 × 300s = 600s (10 min)

# Bug D: periodic exchange/state reconciliation as a safety net (orphan/ghost +
# SL drift). Bug C already fixed the main orphan source, so this is a backup, not
# the primary defense — 360 min (6h) is plenty. Driven from the 60s management
# loop (no separate thread); see Solvira._maybe_periodic_reconcile.
RECONCILE_INTERVAL_MIN = int(os.getenv("RECONCILE_INTERVAL_MIN", "360"))

# Hard cap survivor count yang di-fetch 5m candles per cycle. Mencegah
# burst calls saat banyak aset lolos ctx pre-filter. 20 × 1 call/asset
# = 20 calls/min, jauh di bawah HL rate limit (~60/min).
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "20"))

# Inter-call sleep saat fetch batch 5m candles. 100ms × 20 = 2s total — kecil
# tapi cukup untuk smoothing burst supaya tidak trip per-second sub-limit.
CANDLE_FETCH_INTER_CALL_SLEEP_SEC = float(os.getenv("CANDLE_FETCH_SLEEP", "0.1"))

SPOT = {
    "timeframe": "5m",
    # Lookback must cover the widest window + indicator warmup so the windowed
    # checks always see valid data: max(VOLUME_SPIKE_WINDOW=48, 60)=60 + ~MACD
    # (30+10) / stoch (10+5+5) warmup ~= 100 bars. 120 closed 5m bars (~10h)
    # clears that with ~20 bars margin.
    "candle_lookback": 240,                # EMA60 needs ~4x period to converge
    "min_7d_avg_daily_volume_usd": 100_000,   # legacy key (deriv/other paths)
    "scan_min_24h_volume_usd": SCAN_MIN_24H_VOLUME_USD,  # spot gate per spec
    "min_daily_drop_pct": 2.0,            # stage-1 ctx 24h pre-filter (markPx/prevDayPx)
    "drop_pct": -5.0,                     # stage-2: % drop from 72-bar high on 5m timeframe (~6 hours)
    "drop_lookback_candles": 72,          # 72 × 5m = 6 hours
    "max_entry_slippage_pct": 0.3,
    "stoch_rsi_length": 10,
    "stoch_rsi_k_smooth": 5,
    "stoch_rsi_d_smooth": 5,
    "stoch_rsi_oversold": 20,
    "stoch_cross_lookback": 2,           # golden cross may occur within last 2 closed candles
    "ema_fast_period": 10,
    "ema_slow_period": 60,
    "macd_fast": 10, "macd_slow": 30, "macd_signal": 10,
    "macd_turning_negative": True,       # hist[-2] negative & |hist[-2]| < |hist[-3]|
    "vol_spike_lookback": 3,              # legacy key (unused by spot windowed path)
    "vol_spike_multiplier": 1.5,
    "volume_sma_period": VOLUME_SMA_PERIOD,
    "volume_spike_window": VOLUME_SPIKE_WINDOW,  # legacy key (evaluate_spot_conditions)
    "volume_lookback_candles": 72,
    "volume_burst_multiplier": 1.5,
    "volume_burst_min_bars": 1,
    "volume_burst_max_bars": 10,
    "cutloss_pct": -2.0,
    # (tp_pct, sell_fraction_of_remaining, post_action)
    "take_profits": [(2.0, 0.50, "breakeven"), (5.0, 1.00, None)],
    "max_hold_hours": 6.0,
    "leverage": 1,
    "allocation_split": [0.30, 0.30, 0.40],  # 1-3 aset
}

DERIVATIVE = {
    "timeframe": "5m",
    "candle_lookback": 100,                 # ≥72 for 6h S/R; 100 gives swing/CVD buffer
    "oi_flush_lookback_candles": 36,        # 3 jam window (36 × 5m)
    "oi_flush_drop_pct": 7.0,               # OI turun >7% = flush
    "cvd_rising_window": 3,                 # 3 × 5m
    "funding_rate_negative_threshold": -0.0002,  # -0.02% → long setup
    "funding_rate_positive_threshold": 0.0002,   # +0.02% → short setup
    "support_resistance_lookback_candles": 96,   # ~8 jam
    "support_resistance_pivot_window": 5,
    "support_proximity_pct": 0.025,         # ±2.5% dari S/R level
    "swing_lookback_candles": 20,
    "swing_pivot_window": 5,
    "min_sl_distance_pct": 1.5,             # min SL distance floor (% from entry) to avoid SL≈entry bug
    "structure_break_buffer_pct": 0.005,    # 0.5% buffer
    "risk_per_trade_pct": 1.5,              # % dari TOTAL equity
    "max_leverage": 5,
    "take_profits": [(2.0, 0.50, "breakeven"), (5.0, 1.00, None)],
    "max_hold_hours": 6.0,
}

# === RISK MANAGEMENT [HARD LIMITS — DO NOT MODIFY] ===
# Left at 500: on a ~$40 account this ceiling never binds (spot pool ~$20 and
# derivative margin are already capped well below it by pool capacity), so
# lowering it adds no protection.
MAX_POSITION_SIZE_USD = 500
MAX_OPEN_POSITIONS = 3
# Lowered 50 -> 10 (operator decision) so small pools can trade down to the
# Hyperliquid exchange minimum. Governs both spot & derivative entry gates.
MIN_POSITION_SIZE_USD = 10
LEVERAGE = 1

# === SPOT POSITION FLOOR (exchange-minimum aware) ===
# Hyperliquid enforces a flat platform-wide minimum ORDER VALUE; its spot meta
# (verified via /info spotMeta: 301 pairs / 464 tokens) exposes NO per-asset
# min-notional field — only szDecimals (lot granularity). So the runtime
# "exchange min" lookup resolves to this documented platform minimum, and spot
# sizing uses max(exchange_min_notional, SPOT_MIN_POSITION_FLOOR).
SPOT_MIN_POSITION_FLOOR = 10
HL_PLATFORM_MIN_ORDER_USD = 10
USE_ISOLATED_MARGIN = True
MAX_CONSECUTIVE_LOSSES = 7
# Net deposited principal. Update via .env on each top-up (no redeploy).
# Risk limits below derive from this so they scale with the account.
INITIAL_CAPITAL_USD = float(os.getenv("INITIAL_CAPITAL_USD", "128.08"))
MAX_DAILY_LOSS_PCT = 5.0
# Derived from INITIAL_CAPITAL_USD: daily loss cap = 5% of principal.
# The MAX_DAILY_LOSS_PCT branch additionally scales with live equity.
MAX_DAILY_LOSS_USD = round(INITIAL_CAPITAL_USD * 0.05, 2)

# === STOP-LOSS RULE (3 bulan komitmen) ===
EVALUATION_PERIOD_DAYS = 90
# Rescaled 200 -> 20 (operator, 2026-06-02): ~50% of a ~$40 account, so the
# 3-month halt can actually fire before the account is depleted.
EVALUATION_LOSS_THRESHOLD_USD = round(INITIAL_CAPITAL_USD * 0.20, 2)

# === ALLOCATION ===
def get_allocation(capital: float) -> list[float]:
    """Distribusi modal antar posisi aktif."""
    if capital < 700:
        return [0.40, 0.60]                    # 2 aset
    elif capital < 1500:
        return [0.30, 0.30, 0.40]              # 3 aset
    else:
        return [0.25, 0.25, 0.25, 0.25]        # 4 aset

# === WHITELIST (volume >$5M/hari di Hyperliquid) ===
CRYPTO_WHITELIST = [
    "BTC", "ETH", "SOL", "BNB", "HYPE",
    "ARB", "OP", "AVAX", "DOGE", "LINK", "ATOM", "NEAR", "INJ",
    "TIA", "SUI", "APT", "SEI", "DOT", "PENDLE", "ENA", "TAO",
]

# === FUNDING RATE FILTER ===
NEVER_TRADE_FUNDING_WINDOW_MINUTES = 5
MAX_ACCEPTABLE_FUNDING_RATE_HOURLY = 0.005  # 0.5%/jam

# === TRADING COOLDOWN ===
COOLDOWN_AFTER_CLOSE_MINUTES = 60  # cooldown per asset setelah posisi close

# === FEES ===
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.00045"))
# HL base-tier perp taker = 0.045% of NOTIONAL (verified Jun 2026).
# Re-check if 14-day volume tier changes.

# === BRIDGE ===
HL_BRIDGE_FEE_USD = 1.0  # Hyperliquid bridge withdrawal fee (flat $1)

# === UNIFIED WALLET (perp + spot) ===
# Akun Hyperliquid dalam mode Unified Account: spot+perp balance sudah merged.
# USDC adalah SATU balance yang cover spot + perps sekaligus, dan
# marginSummary.accountValue sudah include semua USDC. usdClassTransfer
# (Spot→Perp) tidak diperlukan dan tidak berlaku di mode ini.
# Hardcoded False: unified account mode, no spot/perp separation.
AUTO_SWEEP_SPOT_TO_PERP = False

# === WITHDRAWAL ===
WITHDRAW_PROFIT_PCT = 0.50             # 50% profit ke USDT-Arbitrum
WITHDRAW_THRESHOLD_USD = 25            # batch ketika kumulatif >$25
WITHDRAW_MIN_INTERVAL_HOURS = 24
USDC_TOKEN_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT_TOKEN_ARB = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
UNISWAP_USDC_USDT_FEE = 100  # 0.01% pool

# === PATHS ===
DATA_DIR = Path(__file__).parent / "data"
LOGS_DIR = Path(__file__).parent / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "solvira.db"

# === API URLs ===
def get_api_url() -> str:
    if USE_TESTNET:
        return "https://api.hyperliquid-testnet.xyz"
    return "https://api.hyperliquid.xyz"

# Rate limit fix — max candidates per scan cycle
MAX_CANDIDATES_PER_CYCLE = int(os.getenv('MAX_CANDIDATES_PER_CYCLE', '20'))

# Candle fetch inter-call sleep
CANDLE_FETCH_INTER_CALL_SLEEP_SEC = float(os.getenv('CANDLE_FETCH_SLEEP', '0.1'))
