#!/usr/bin/env python3
"""
Simulate a signal to test autotrader for all enabled users.

Usage:
    uv run python scripts/simulate_signal.py --side YES --timeframe 5m
    uv run python scripts/simulate_signal.py --side NO --timeframe 15m --dry-run
"""

from __future__ import annotations

import argparse
import time
import os
import sys

from pathlib import Path

# Ensure repo root is importable
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database_manager import DatabaseManager
from autotrader_manager import AutoTraderManager, TIMEFRAME_TO_SERIES


def main():
    parser = argparse.ArgumentParser(description="Simulate a signal for testing autotrader")
    parser.add_argument(
        "--side",
        choices=["YES", "NO"],
        default="YES",
        help="Signal direction (YES or NO)",
    )
    parser.add_argument(
        "--timeframe",
        choices=list(TIMEFRAME_TO_SERIES.keys()),
        default="5m",
        help="Signal timeframe",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without executing trades",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "app_data.sqlite3"),
        help="Database path",
    )
    args = parser.parse_args()

    db = DatabaseManager(args.db)

    # Get all enabled users with trade amounts
    rows = db.execute(
        "SELECT user_id, signal_trade_amount_usd FROM users "
        "WHERE signal_trading_enabled = 1 AND COALESCE(signal_trade_amount_usd, 0) > 0"
    ).fetchall()

    if not rows:
        print("No enabled users found with signal trading configured.")
        print("Users can enable via bot: /menu -> Signal trading -> Enable")
        return

    print(f"Simulating signal: {args.side} for {args.timeframe}")
    print(f"Found {len(rows)} enabled users")
    print("-" * 50)

    now_ts = int(time.time())

    for r in rows:
        uid = int(r["user_id"])
        shares = float(r["signal_trade_amount_usd"])

        try:
            manager = AutoTraderManager(
                db=db,
                user_id=uid,
                trade_amount_usd=shares,
                dry_run=args.dry_run,
                send_notification=not args.dry_run,
            )

            signal = {
                "signal": args.side,
                "timeframe": args.timeframe,
                "signal_ts": now_ts,
                "market_end_ts": now_ts + int(args.timeframe[:-1]) * 60 if args.timeframe.endswith('m') else 300,
            }

            traded, result = manager.process_signal(signal)

            status = "TRADED" if traded else "SKIPPED"
            print(f"[{status}] User {uid}: {result}")

        except Exception as e:
            print(f"[ERROR] User {uid}: {e}")

    print("-" * 50)
    print("Done!")


if __name__ == "__main__":
    main()
