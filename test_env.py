import os
from dotenv import load_dotenv

load_dotenv()

print("USE_TESTNET:", os.getenv("USE_TESTNET"))
print("DRY_RUN:", os.getenv("DRY_RUN"))
print("HL_ADDRESS:", os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")[:10] + "..." if os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") else "MISSING")
print("TELEGRAM_TOKEN:", "SET" if os.getenv("TELEGRAM_BOT_TOKEN") else "MISSING")
print("ANTHROPIC_KEY:", "SET" if os.getenv("ANTHROPIC_API_KEY") else "MISSING")