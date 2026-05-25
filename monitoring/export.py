"""
CLI export untuk evaluasi mingguan trade history.

Usage:
    python -m monitoring.export weekly --csv data/weekly.csv
    python -m monitoring.export weekly --csv data/weekly_spot.csv --strategy spot
    python -m monitoring.export weekly --csv data/weekly_deriv.csv --strategy derivative
    python -m monitoring.export all --csv data/full_history.csv --days 90
"""
import argparse
import csv
from pathlib import Path
from monitoring.trade_logger import get_recent_trades


COLUMNS = [
    "id", "asset", "asset_class", "strategy_type", "side", "leverage",
    "entry_price", "exit_price", "entry_swing_price",
    "size_coin", "size_usd",
    "entry_time_utc", "exit_time_utc", "hold_duration_seconds",
    "pnl_usd", "pnl_pct", "fees_usd",
    "exit_reason", "indicators_snapshot", "notes", "created_at",
]


def export_trades_csv(out_path: Path, days: int, strategy: str = None) -> int:
    trades = get_recent_trades(days_back=days, strategy_type=strategy)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in trades:
            writer.writerow(row)
    return len(trades)


def main():
    ap = argparse.ArgumentParser(description="Solvira trade history exporter")
    ap.add_argument("command", choices=["weekly", "all"],
                    help="weekly = last 7 days; all = last --days days")
    ap.add_argument("--csv", default="data/export.csv", help="Output CSV path")
    ap.add_argument("--days", type=int, default=90, help="Days back (for 'all' command)")
    ap.add_argument("--strategy", choices=["spot", "derivative"], default=None,
                    help="Filter by strategy_type (default: all)")
    args = ap.parse_args()

    days = 7 if args.command == "weekly" else args.days
    out_path = Path(args.csv)
    count = export_trades_csv(out_path, days=days, strategy=args.strategy)
    label = args.strategy or "all"
    print(f"Exported {count} trades ({label}, last {days}d) → {out_path}")


if __name__ == "__main__":
    main()
