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
MIN_DAILY_VOLUME_USD = 5_000_000   # >$5M/hari di Hyperliquid
MIN_DAILY_DROP_PCT = 10.0           # >10% penurunan vs hari sebelumnya
# Market cap filter DIABAIKAN per spesifikasi user

# === INDICATORS ===
TIMEFRAME = "10m"
CANDLE_LOOKBACK = 100
STOCH_RSI_OVERSOLD = 20
STOCH_RSI_LENGTH = 14
STOCH_RSI_K_SMOOTH = 3
STOCH_RSI_D_SMOOTH = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOLUME_SPIKE_LOOKBACK = 3           # 3 candle sebelumnya
VOLUME_SPIKE_MULTIPLIER = 1.5

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
UNIVERSE_REFRESH_INTERVAL_SECONDS = 3600

SPOT = {
    "timeframe": "5m",
    "candle_lookback": 120,
    "min_7d_avg_daily_volume_usd": 100_000,
    "min_daily_drop_pct": 2.0,            # -2% vs close kemarin
    "max_entry_slippage_pct": 0.3,
    "stoch_rsi_length": 10,
    "stoch_rsi_k_smooth": 5,
    "stoch_rsi_d_smooth": 5,
    "stoch_rsi_oversold": 20,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "vol_spike_lookback": 3,
    "vol_spike_multiplier": 1.5,
    "cutloss_pct": -2.0,
    # (tp_pct, sell_fraction_of_remaining, post_action)
    "take_profits": [(5.0, 0.50, "breakeven"), (10.0, 0.50, None), (20.0, 1.00, None)],
    "max_hold_hours": 6,
    "leverage": 1,
    "allocation_split": [0.30, 0.30, 0.40],  # 1-3 aset
}

DERIVATIVE = {
    "timeframe": "5m",
    "candle_lookback": 120,
    "oi_flush_lookback_candles": 12,        # 1 jam window (12 × 5m)
    "oi_flush_drop_pct": 15.0,              # OI turun >15% = flush
    "cvd_rising_window": 3,                 # 3 × 5m
    "funding_rate_negative_threshold": -0.0005,  # -0.05% → long setup
    "funding_rate_positive_threshold": 0.0005,   # +0.05% → short setup
    "support_resistance_lookback_candles": 96,   # ~8 jam
    "support_resistance_pivot_window": 5,
    "support_proximity_pct": 0.01,          # ±1% dari S/R level
    "swing_lookback_candles": 20,
    "swing_pivot_window": 5,
    "structure_break_buffer_pct": 0.005,    # 0.5% buffer
    "risk_per_trade_pct": 1.5,              # % dari TOTAL equity
    "max_leverage": 5,
    "take_profits": [(5.0, 0.50, "breakeven"), (10.0, 0.50, None), (20.0, 1.00, None)],
    "max_hold_hours": 6,
}

# === RISK MANAGEMENT [HARD LIMITS — DO NOT MODIFY] ===
MAX_POSITION_SIZE_USD = 500
MAX_OPEN_POSITIONS = 3
MIN_POSITION_SIZE_USD = 50
LEVERAGE = 1
USE_ISOLATED_MARGIN = True
MAX_CONSECUTIVE_LOSSES = 7
MAX_DAILY_LOSS_PCT = 10.0
MAX_DAILY_LOSS_USD = 50

# === STOP-LOSS RULE (3 bulan komitmen) ===
EVALUATION_PERIOD_DAYS = 90
EVALUATION_LOSS_THRESHOLD_USD = 200

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

# === BRIDGE ===
HL_BRIDGE_FEE_USD = 1.0  # Hyperliquid bridge withdrawal fee (flat $1)

# === UNIFIED WALLET (perp + spot) ===
# Bot trading hanya di PERP (spot strategy = perp 1x, deriv = perp ≤5x).
# Karena itu USDC di spot wallet harus di-sweep ke perp supaya bisa dipakai
# sebagai margin. Hyperliquid Spot ↔ Perp transfer internal gratis.
AUTO_SWEEP_SPOT_TO_PERP = os.getenv("AUTO_SWEEP_SPOT_TO_PERP", "true").lower() == "true"
MIN_SPOT_SWEEP_USD = float(os.getenv("MIN_SPOT_SWEEP_USD", "1.0"))
SPOT_SWEEP_INTERVAL_MINUTES = int(os.getenv("SPOT_SWEEP_INTERVAL_MINUTES", "15"))

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

# === INITIAL CAPITAL (untuk stop-loss enforcer) ===
INITIAL_CAPITAL_USD = 500