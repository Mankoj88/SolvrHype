Solvira — Autonomous Crypto Trading Bot for Hyperliquid
Solvira is a live, production-grade automated trading system built natively for the Hyperliquid decentralized exchange — one of the fastest-growing on-chain perpetuals and spot trading platforms. It operates 24/7 on a cloud VPS, trading both spot and perpetual derivative markets with real capital using a fully systematic, signal-driven strategy.
What It Does
Solvira identifies high-probability entry setups by combining multiple technical signals into a strict multi-condition AND-gate entry system. For spot markets, it screens for: significant short-term price drops from recent highs, bullish momentum confirmation via Stochastic RSI golden cross, MACD turning points, and concentrated volume bursts — all evaluated on closed candle data to eliminate look-ahead bias. For perpetual derivatives, it layers in on-chain signals unique to Hyperliquid: funding rate extremes, open interest flush detection via a rolling 6-hour ring buffer, and support/resistance proximity checks.
Position management is equally systematic — featuring a two-tier take-profit structure (TP1 at +2% with partial close and breakeven stop, TP2 at +5% full close), Parabolic SAR-based hold extension, and hard 24-hour position ceilings. Risk is governed by dynamic capital-relative loss limits, with a circuit breaker that halts trading if daily drawdown exceeds 5% of initial capital.
Technical Highlights

Async dual-loop architecture — scan/entry and position management run as concurrent async tasks, protected by a shared lock to prevent race conditions
Stateful OI history — open interest snapshots persist across restarts via JSON, preventing signal drought from cold-start data gaps
Unified Account compatibility — correctly reads capital from both Perps margin and Spot USDC balance on Hyperliquid's Unified Account model
Secure secrets management — .env encrypted with age, decrypted at runtime by systemd via a shell loader script
Structured funnel telemetry — per-stage rejection counters logged every cycle, enabling systematic diagnosis of why candidates are filtered out
Telegram notifications for trade events and alerts

Why Contribute?
Solvira is a working, self-funded live trading experiment — not a backtested demo. Every architectural decision has been pressure-tested against real market behavior on mainnet Hyperliquid. The codebase has gone through multiple significant overhauls: fixing candle indexing bugs that caused stale-data entries, redesigning risk constants to be capital-relative rather than hardcoded, and rebuilding state persistence that was silently wiping itself on each restart.
There's meaningful open work ahead: improving entry frequency without sacrificing signal quality, building a rigorous walk-forward validation framework, implementing the weekly compounding withdrawal model, and expanding the strategy universe beyond the current indicator set. If you're interested in quantitative strategy design, async Python systems, on-chain data integration, or simply building something that trades real money on a novel exchange — this project is worth your time.

Built on Python · Hyperliquid SDK · Asyncio · Systemd · Ubuntu VPS · GitHub CI workflow
