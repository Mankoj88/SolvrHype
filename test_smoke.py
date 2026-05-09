"""
Smoke test untuk verify environment setup.
Run: python test_smoke.py
"""
import sys
import importlib

print(f"Python version: {sys.version}")
assert sys.version_info >= (3, 13), f"Need Python 3.13+, got {sys.version_info}"
print("✅ Python 3.14+ OK")

# Test imports
modules_to_test = [
    ("hyperliquid.info", "Info"),
    ("hyperliquid.exchange", "Exchange"),
    ("hyperliquid.utils.constants", "MAINNET_API_URL"),
    ("pandas", None),
    ("numpy", None),
    ("ta.momentum", "StochRSIIndicator"),
    ("ta.trend", "MACD"),
    ("web3", "Web3"),
    ("eth_account", "Account"),
    ("anthropic", None),
    ("loguru", None),
    ("dotenv", "load_dotenv"),
    ("requests", None),
    ("schedule", None),
    ("sqlalchemy", None),
]

errors = []
for mod_name, attr in modules_to_test:
    try:
        mod = importlib.import_module(mod_name)
        if attr:
            getattr(mod, attr)
        print(f"✅ {mod_name}" + (f".{attr}" if attr else ""))
    except (ImportError, AttributeError) as e:
        print(f"❌ {mod_name}: {e}")
        errors.append((mod_name, str(e)))

# Verify web3.py 7.x API
try:
    from web3 import Web3
    from eth_account import Account
    
    acct = Account.create()
    tx = {
        "to": acct.address, "value": 0, "gas": 21000,
        "gasPrice": 1_000_000_000, "nonce": 0, "chainId": 42161,
    }
    signed = acct.sign_transaction(tx)
    
    assert hasattr(signed, "raw_transaction"), "web3.py 7.x should have raw_transaction"
    print("✅ web3.py 7.x signed.raw_transaction API confirmed")
    
except Exception as e:
    print(f"❌ web3 API test failed: {e}")
    errors.append(("web3 API", str(e)))

# Test Hyperliquid testnet connection
try:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = Info(constants.TESTNET_API_URL, skip_ws=True)
    meta = info.meta()
    assert len(meta["universe"]) > 50
    print(f"✅ Hyperliquid testnet OK ({len(meta['universe'])} assets)")
except Exception as e:
    print(f"❌ Hyperliquid connection: {e}")
    errors.append(("Hyperliquid", str(e)))

print("\n" + "=" * 50)
if errors:
    print(f"❌ {len(errors)} error(s):")
    for name, msg in errors:
        print(f"  - {name}: {msg}")
    sys.exit(1)
else:
    print("✅ All checks passed. Setup OK.")
    sys.exit(0)
    
