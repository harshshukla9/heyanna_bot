#!/usr/bin/env python3
"""
Autotrader: Execute trades automatically based on Telegram signals.

This script:
1. Reads parsed signals from the Telegram announcement listener JSONL output
2. Resolves the correct Polymarket market (5m/15m series) based on signal timeframe
3. Executes trades using a configured user wallet
4. Tracks signal performance (win/loss)

Requirements:
- TELEGRAM_SIGNAL_OUTPUT: Path to the signals JSONL file (default: logs/announcement_signals.jsonl)
- USER_ID: The user ID to trade as (from users table)
- DB_PATH: Path to the SQLite database (default: app_data.sqlite3)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections import deque

from dotenv import load_dotenv
import logging

# Ensure repo root is importable
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database_manager import DatabaseManager
import market_cache
from trading import execute_trade_for_user, execute_limit_order_for_user
from scripts.series_5m_order_app import _resolve_latest_event_for_series_slug

# Import AutoTraderManager for multi-user support
try:
    from autotrader_manager import AutoTraderManager
    AUTOTRADER_MANAGER_AVAILABLE = True
except ImportError:
    AUTOTRADER_MANAGER_AVAILABLE = False

# Setup order log file
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
ORDER_LOG_FILE = LOGS_DIR / "autotrader_orders.jsonl"

# Setup logger for orders
order_logger = logging.getLogger("autotrader_orders")
order_logger.setLevel(logging.INFO)
order_logger.propagate = False

# File handler for order logging
fh = logging.FileHandler(ORDER_LOG_FILE, mode='a')
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter('%(message)s'))
order_logger.addHandler(fh)


# Mapping from signal timeframe to Polymarket series slug
TIMEFRAME_TO_SERIES = {
    "1m": "btc-up-or-down-1m",
    "5m": "btc-up-or-down-5m",
    "15m": "btc-up-or-down-15m",
    "30m": "btc-up-or-down-30m",
    "1h": "btc-up-or-down-1h",
}

# Default amount to trade (USD)
# Note: This is overridden by the --amount argument
DEFAULT_TRADE_AMOUNT_USD = 3.0  # 3 USD per trade

# Signal age limit: skip signals older than this (seconds)
MAX_SIGNAL_AGE_SECONDS = 600  # 10 minutes

# Track recent signals to avoid duplicates
RECENT_SIGNALS_MAX = 100

# Limit order settings
USE_LIMIT_ORDERS = True  # Use limit orders instead of market orders
LIMIT_ORDER_PRICE = 0.55  # Max price per share (55 cents)
MIN_ORDER_SHARES = 5  # Polymarket minimum order size

# Poll interval for instant execution
DEFAULT_POLL_INTERVAL = 1  # 1 second for instant execution
# Order size: max of (trade_amount / price) or MIN_ORDER_SHARES


@dataclass
class SignalState:
    """Track state for a signal."""
    message_key: str
    signal_ts: int
    timeframe: str | None
    side: str  # YES or NO
    market_end_ts: int
    traded: bool = False
    trade_ts: int | None = None
    trade_result: str | None = None


class AutoTrader:
    """Autotrader that executes trades based on Telegram signals."""

    def __init__(
        self,
        db_path: str,
        user_id: int,
        signals_path: Path,
        trade_amount_usd: float = DEFAULT_TRADE_AMOUNT_USD,
        dry_run: bool = False,
    ):
        self.db_path = db_path
        self.user_id = user_id
        self.signals_path = signals_path
        self.trade_amount_usd = trade_amount_usd
        self.dry_run = dry_run

        self.db = DatabaseManager(db_path=db_path)
        self.db.init_schema()

        self.recent_signals: deque[str] = deque(maxlen=RECENT_SIGNALS_MAX)
        self.signal_states: dict[str, SignalState] = {}
        self.traded_condition_ids: set[str] = set()  # Track condition_ids we've already traded

        # Load user info
        user = self.db.get_user(user_id)
        if not user:
            raise RuntimeError(f"User {user_id} not found in database.")
        if not user.get("eth_address"):
            raise RuntimeError(f"User {user_id} has no eth_address.")
        self.user = user

        # Load already-traded condition_ids from DB to prevent re-trading after restart
        self._load_traded_condition_ids()

    def _load_traded_condition_ids(self) -> None:
        """Load traded condition_ids from DB."""
        # Create tracking table if not exists
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS traded_condition_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, condition_id TEXT, traded_at INTEGER, UNIQUE(user_id, condition_id));"
            )
        except Exception:
            pass

        try:
            rows = self.db.execute(
                "SELECT DISTINCT condition_id FROM traded_condition_ids WHERE user_id = ?;",
                (self.user_id,)
            ).fetchall()
            self.traded_condition_ids = {row[0] for row in rows if row[0]}
            print(f"[autotrader] Loaded {len(self.traded_condition_ids)} previously traded condition_ids from DB")
        except Exception as e:
            print(f"[autotrader] Warning: Could not load traded condition_ids: {e}")

    def _is_signal_recent(self, message_key: str) -> bool:
        """Check if we've already processed this signal."""
        return message_key in self.recent_signals

    def _validate_signal_timing(self, signal: dict[str, Any]) -> tuple[bool, str]:
        """Check if signal is fresh enough to trade on."""
        signal_ts = signal.get("signal_ts") or signal.get("market_end_ts")
        if not signal_ts:
            return False, "No timestamp"

        now = int(time.time())
        age = now - signal_ts

        # Check if signal is too old
        if age > MAX_SIGNAL_AGE_SECONDS:
            return False, f"too old ({age}s)"

        # Check if market has already ended
        market_end_ts = signal.get("market_end_ts")
        if market_end_ts and now > market_end_ts:
            return False, f"market ended ({now - market_end_ts}s ago)"

        return True, f"OK (age={age}s)"

    def _resolve_market_for_signal(self, signal: dict[str, Any]) -> tuple[str, str] | None:
        """
        Resolve the Polymarket market condition_id for a signal.
        Returns (condition_id, event_slug) or None if resolution fails.
        """
        timeframe = signal.get("timeframe")
        if not timeframe:
            return None

        series_slug = TIMEFRAME_TO_SERIES.get(timeframe)
        if not series_slug:
            return None

        try:
            resolved = _resolve_latest_event_for_series_slug(
                series_slug,
                max_events=2000,
                page_size=200,
                active_only=False,
            )
            return (resolved.condition_id, resolved.event_slug)
        except Exception as e:
            print(f"  Failed to resolve market for {series_slug}: {e}", file=sys.stderr)
            return None

    def _map_signal_side(self, side: str, condition_id: str) -> str:
        """Map signal side (YES/NO) to market outcome (Up/Down)."""
        # First, get market info to find actual outcome names
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if not m:
            return side  # Return as-is if market not found

        outcomes_lower = [o.lower() for o in m.outcomes]
        side_lower = side.lower()

        # Map YES to first outcome (typically Up/Yes), NO to second (Down/No)
        if side_lower == "yes":
            return m.outcomes[0] if "up" in outcomes_lower[0] or "yes" in outcomes_lower[0] else m.outcomes[0]
        elif side_lower == "no":
            return m.outcomes[1] if len(m.outcomes) > 1 else m.outcomes[0]
        return side

    def _execute_trade(self, condition_id: str, side: str, signal: dict[str, Any] | None = None) -> str:
        """Execute a trade for the signal."""
        now_ts = int(time.time())
        signal_info = signal or {}

        # Log order attempt
        order_log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timestamp_ts": now_ts,
            "user_id": self.user_id,
            "condition_id": condition_id,
            "side": side,
            "signal": signal_info.get("signal"),
            "timeframe": signal_info.get("timeframe"),
            "message_key": signal_info.get("message_key"),
            "signal_ts": signal_info.get("signal_ts"),
            "dry_run": self.dry_run,
        }

        if self.dry_run:
            if USE_LIMIT_ORDERS:
                order_log["order_type"] = "limit"
                order_log["price"] = LIMIT_ORDER_PRICE
                order_log["size"] = LIMIT_ORDER_SIZE_USD / LIMIT_ORDER_PRICE
                order_log["status"] = "dry_run"
                order_logger.info(json.dumps(order_log))
                return f"[DRY RUN] Would limit order: {side} @ ${LIMIT_ORDER_PRICE} on {condition_id[:20]}..."
            else:
                order_log["order_type"] = "market"
                order_log["amount"] = self.trade_amount_usd
                order_log["status"] = "dry_run"
                order_logger.info(json.dumps(order_log))
                return f"[DRY RUN] Would trade: {side} on condition {condition_id[:20]}..."

        try:
            # Map signal side (YES/NO) to market outcome (Up/Down)
            mapped_side = self._map_signal_side(side, condition_id)

            if USE_LIMIT_ORDERS:
                # Use limit order with price cap to avoid liquidity/slippage issues
                # Polymarket minimum: 5 shares
                shares = max(self.trade_amount_usd / LIMIT_ORDER_PRICE, MIN_ORDER_SHARES)
                order_log["order_type"] = "limit"
                order_log["price"] = LIMIT_ORDER_PRICE
                order_log["size"] = shares
                order_log["signal_side"] = side
                order_log["mapped_side"] = mapped_side

                result = execute_limit_order_for_user(
                    db=self.db,
                    user_id=self.user_id,
                    side=mapped_side,
                    price=LIMIT_ORDER_PRICE,
                    size=shares,
                    condition_id=condition_id,
                    order_side="BUY",
                )

                order_log["result"] = result
                order_log["status"] = "success" if "✅" in result else "failed"
                order_logger.info(json.dumps(order_log))
                return result
            else:
                order_log["order_type"] = "market"
                order_log["amount"] = self.trade_amount_usd
                order_log["signal_side"] = side
                order_log["mapped_side"] = mapped_side

                result = execute_trade_for_user(
                    db=self.db,
                    user_id=self.user_id,
                    side=mapped_side,
                    amount=self.trade_amount_usd,
                    condition_id=condition_id,
                    order_side="BUY",
                )

                order_log["result"] = result
                order_log["status"] = "success" if "✅" in result else "failed"
                order_logger.info(json.dumps(order_log))
                return result
        except Exception as e:
            order_log["error"] = str(e)
            order_log["status"] = "error"
            order_logger.info(json.dumps(order_log))
            return f"Trade execution failed: {e}"

    def process_signal(self, signal: dict[str, Any]) -> bool:
        """
        Process a single signal and potentially execute a trade.
        Returns True if a trade was attempted.
        """
        message_key = signal.get("message_key")
        if not message_key:
            return False

        # Check if already processed (skip to prevent re-trading)
        if message_key in self.recent_signals:
            return False

        # Check if already traded (double-check via signal_states)
        if message_key in self.signal_states:
            existing = self.signal_states[message_key]
            if existing.traded:
                print(f"  Already traded for this signal, skipping")
                self.recent_signals.append(message_key)
                return False

        # Validate timing FIRST
        valid, reason = self._validate_signal_timing(signal)
        if not valid:
            print(f"  Skipping signal: {reason}")
            self.recent_signals.append(message_key)
            return False

        # Extract signal details
        side = signal.get("signal")  # YES or NO
        if not side:
            self.recent_signals.append(message_key)
            return False

        # Resolve market
        resolution = self._resolve_market_for_signal(signal)
        if not resolution:
            print(f"  Could not resolve market for signal")
            self.recent_signals.append(message_key)
            return False

        condition_id, event_slug = resolution

        # Check if we've already traded on this condition_id (same market)
        # This prevents multiple orders on the same market from different signals
        if condition_id in self.traded_condition_ids:
            print(f"  Already traded on this market ({condition_id[:16]}...), skipping")
            self.recent_signals.append(message_key)
            return False

        # Execute trade
        print(f"  Executing trade...")
        result = self._execute_trade(condition_id, side, signal)

        # Record trade in DB immediately to persist across restarts
        # Only record successful trades to DB, not failed attempts
        if "✅" in result:
            try:
                self.db.execute(
                    "INSERT OR IGNORE INTO traded_condition_ids (user_id, condition_id, traded_at) VALUES (?, ?, ?);",
                    (self.user_id, condition_id, int(time.time()))
                )
            except Exception:
                pass  # Table may not exist, but in-memory tracking still works

        # Record state IMMEDIATELY after trade attempt
        self.signal_states[message_key] = SignalState(
            message_key=message_key,
            signal_ts=signal.get("signal_ts", 0),
            timeframe=signal.get("timeframe"),
            side=side,
            market_end_ts=signal.get("market_end_ts", 0),
            traded=True,
            trade_ts=int(time.time()),
            trade_result=result,
        )
        self.traded_condition_ids.add(condition_id)
        self.recent_signals.append(message_key)

        return True

    def run(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
        """Main loop: poll for new signals and trade."""
        print(f"[autotrader] Starting autotrader")
        print(f"[autotrader] User: {self.user.get('username')} (ID: {self.user_id})")
        print(f"[autotrader] Trade amount: ${self.trade_amount_usd}")
        print(f"[autotrader] Dry run: {self.dry_run}")
        print(f"[autotrader] Signals file: {self.signals_path}")
        print()

        if self.dry_run:
            print("⚠️  DRY RUN MODE - No actual trades will be executed!")
            print()

        while True:
            try:
                # Read signals file
                if not self.signals_path.exists():
                    time.sleep(poll_interval)
                    continue

                # Read new signals
                with open(self.signals_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        signal = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Only process parsed signals
                    if not signal.get("parsed"):
                        continue

                    self.process_signal(signal)

                time.sleep(poll_interval)

            except KeyboardInterrupt:
                print("\n[autotrader] Stopped by user.")
                break
            except Exception as e:
                print(f"[autotrader] Error: {e}")
                time.sleep(poll_interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autotrader for Telegram signals")
    parser.add_argument(
        "--db-path",
        default=os.getenv("DB_PATH") or "app_data.sqlite3",
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=int(os.getenv("USER_ID") or "0"),
        help="User ID to trade as (use --all-users for multi-user mode)",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Trade for all users with signal_trading_enabled=1 (multi-user mode)",
    )
    parser.add_argument(
        "--signals-path",
        default=os.getenv("TELEGRAM_SIGNAL_OUTPUT") or "logs/announcement_signals.jsonl",
        help="Path to signals JSONL file",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=DEFAULT_TRADE_AMOUNT_USD,
        help=f"Trade amount in USD (default: ${DEFAULT_TRADE_AMOUNT_USD})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print trades without executing",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help="Poll interval in seconds (default: 1 for instant execution)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear tracked condition_ids and exit",
    )
    return parser.parse_args()


def clear_tracked_condition_ids(db_path: str, user_id: int) -> int:
    """Clear the tracked condition_ids for a user (reset autotrader)."""
    db = DatabaseManager(db_path=db_path)
    db.init_schema()

    try:
        db.execute("CREATE TABLE IF NOT EXISTS traded_condition_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, condition_id TEXT, traded_at INTEGER, UNIQUE(user_id, condition_id));")
    except Exception:
        pass

    result = db.execute(
        "DELETE FROM traded_condition_ids WHERE user_id = ?;",
        (user_id,)
    ).rowcount

    print(f"Cleared {result} tracked condition_ids for user {user_id}")
    return 0


def main() -> int:
    load_dotenv()
    args = parse_args()

    # Handle clear command
    if hasattr(args, 'clear') and args.clear:
        if args.all_users:
            # Clear for all users - need to iterate
            db = DatabaseManager(db_path=args.db_path)
            db.init_schema()
            rows = db.execute("SELECT user_id FROM users WHERE signal_trading_enabled = 1;").fetchall()
            for r in rows:
                clear_tracked_condition_ids(args.db_path, r["user_id"])
            return 0
        if not args.user_id:
            print("Error: USER_ID is required. Set via --user-id or USER_ID env.", file=sys.stderr)
            return 1
        return clear_tracked_condition_ids(args.db_path, args.user_id)

    # Multi-user mode: trade for all enabled users
    if args.all_users:
        return run_multitrader_mode(
            db_path=args.db_path,
            signals_path=args.signals_path,
            dry_run=args.dry_run,
            poll_interval=args.poll_interval,
        )

    # Single-user mode
    if not args.user_id:
        print("Error: USER_ID is required. Set via --user-id, USER_ID env, or use --all-users.", file=sys.stderr)
        return 1

    signals_path = Path(args.signals_path)
    autotrader = AutoTrader(
        db_path=args.db_path,
        user_id=args.user_id,
        signals_path=signals_path,
        trade_amount_usd=args.amount,
        dry_run=args.dry_run,
    )

    autotrader.run(poll_interval=args.poll_interval)
    return 0


def run_multitrader_mode(db_path: str, signals_path: str, dry_run: bool, poll_interval: int) -> int:
    """
    Multi-user mode: automatically trade for all users with signal_trading_enabled=1.
    Each user trades with their own amount from the database.
    """
    print("[autotrader] Starting multi-user mode - trading for all enabled users")

    db = DatabaseManager(db_path=db_path)
    db.init_schema()

    signals_path_obj = Path(signals_path)

    while True:
        try:
            # Read signals
            if not signals_path_obj.exists():
                time.sleep(poll_interval)
                continue

            with open(signals_path_obj, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # Get all enabled users
            rows = db.execute(
                "SELECT user_id, signal_trade_amount_usd FROM users WHERE signal_trading_enabled = 1 AND COALESCE(signal_trade_amount_usd, 0) > 0;"
            ).fetchall()

            if not rows:
                print("[autotrader] No enabled users found. Waiting...")
                time.sleep(poll_interval)
                continue

            print(f"[autotrader] Found {len(rows)} enabled users")

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    signal = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not signal.get("parsed"):
                    continue

                # Trade for each user
                for r in rows:
                    try:
                        uid = int(r["user_id"])
                        amount = float(r["signal_trade_amount_usd"] or 0.0)

                        manager = AutoTraderManager(
                            db=db,
                            user_id=uid,
                            trade_amount_usd=amount,
                            dry_run=dry_run,
                        )

                        traded, result = manager.process_signal(signal)
                        if traded:
                            print(f"[autotrader:{uid}] Trade executed: {result[:100]}...")
                        else:
                            print(f"[autotrader:{uid}] Signal skipped: {result}")
                    except Exception as e:
                        print(f"[autotrader] Error for user {r['user_id']}: {e}")
                        continue

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n[autotrader] Multi-user mode stopped.")
            break
        except Exception as e:
            print(f"[autotrader] Error: {e}")
            time.sleep(poll_interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
