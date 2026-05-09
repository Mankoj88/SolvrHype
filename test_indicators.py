import pandas as pd
from strategy.indicators import compute_all_indicators

df = pd.DataFrame({
    "close": [100, 101, 99, 98, 100, 102, 99, 97, 95, 96, 98, 100],
    "volume": [1000, 1100, 900, 1200, 1500, 800, 700, 600, 1800, 2000, 1500, 1200],
})
df = compute_all_indicators(df)
print(df.tail())

# Verify kolom yang harus ada
expected_cols = ["stoch_rsi_k", "stoch_rsi_d", "macd_hist", "volume_spike"]
for col in expected_cols:
    assert col in df.columns, f"❌ Missing column: {col}"
print("✅ All indicator columns present")