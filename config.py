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

# === EXECUTION ===
TAKE_PROFITS = [
    (10.0, 0.60),  # +10% sell 60% of remaining (= 60% of original at TP1)
    (20.0, 1.00),  # +20% sell 100% of remaining (= 40% of original, since TP1 sold 60%)
]
CUTLOSS_PCT = -5.0
USE_BREAKEVEN_AFTER_TP1 = True
MAX_HOLD_HOURS = 6
SLIPPAGE_TOLERANCE = 0.005

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