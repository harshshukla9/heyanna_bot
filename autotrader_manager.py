"""
AutoTraderManager: Reusable autotrading logic for API and bot integration.

This module provides the core autotrading functionality from scripts/autotrader.py
as a reusable manager class that can be integrated into the API or bot.

Features:
- Market resolution for multiple timeframes (1m, 5m, 15m, 30m, 1h)
- Limit order execution with price caps to avoid liquidity/slippage issues
- Signal deduplication and freshness validation
- Tracking of traded condition_ids to prevent re-trading
"""

from __future__ import annotations

import json
import time
import logging
import os
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.series_5m_order_app import _resolve_latest_event_for_series_slug
except ImportError:
    from series_5m_order_app import _resolve_latest_event_for_series_slug

from trading import execute_trade_for_user, execute_limit_order_for_user

logger = logging.getLogger(__name__)

# Mapping from signal timeframe to Polymarket series slug
TIMEFRAME_TO_SERIES = {
    "1m": "btc-up-or-down-1m",
    "5m": "btc-up-or-down-5m",
    "15m": "btc-up-or-down-15m",
    "30m": "btc-up-or-down-30m",
    "1h": "btc-up-or-down-1h",
}

# Default settings
DEFAULT_TRADE_AMOUNT_USD = 3.0
MAX_SIGNAL_AGE_SECONDS = 600  # 10 minutes
RECENT_SIGNALS_MAX = 100

# Limit order settings
USE_LIMIT_ORDERS = True
LIMIT_ORDER_PRICE = 0.55  # Max price per share
MIN_ORDER_SHARES = 5  # Polymarket minimum order size


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
    condition_id: str | None = None


class AutoTraderManager:
    """
    Reusable autotrading manager that can be integrated into API or bot.

    Usage:
        manager = AutoTraderManager(db, user_id, trade_amount_usd=3.0, dry_run=False)

        # For single signal processing
        result = manager.process_signal(signal_dict)

        # For checking if a signal is valid
        is_valid, reason = manager.validate_signal_timing(signal_dict)

        # For resolving market
        resolution = manager.resolve_market_for_signal(signal_dict)
    """

    def __init__(
        self,
        db: Any,  # DatabaseManager instance
        user_id: int,
        trade_amount_usd: float = DEFAULT_TRADE_AMOUNT_USD,
        dry_run: bool = False,
        use_limit_orders: bool = USE_LIMIT_ORDERS,
        limit_order_price: float = LIMIT_ORDER_PRICE,
        send_notification: bool = True,
    ):
        self.db = db
        self.user_id = user_id
        self.trade_amount_usd = trade_amount_usd
        self.dry_run = dry_run
        self.use_limit_orders = use_limit_orders
        self.limit_order_price = limit_order_price
        self.send_notification = send_notification

        # Telegram bot token for notifications
        self.bot_token = os.getenv("BOT_TOKEN")

        # Track recent signals to avoid duplicates
        self.recent_signals: deque[str] = deque(maxlen=RECENT_SIGNALS_MAX)
        self.signal_states: dict[str, SignalState] = {}
        self.traded_condition_ids: set[str] = set()

        # Load user info
        user = self.db.get_user(user_id)
        if not user:
            raise RuntimeError(f"User {user_id} not found in database.")
        if not user.get("eth_address"):
            raise RuntimeError(f"User {user_id} has no eth_address.")
        self.user = user

        # Load already-traded condition_ids from DB
        self._load_traded_condition_ids()

        # Setup order logger
        self._setup_order_logger()

    def _setup_order_logger(self) -> None:
        """Setup order logging to file."""
        LOGS_DIR = Path(__file__).parent / "logs"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ORDER_LOG_FILE = LOGS_DIR / "autotrader_orders.jsonl"

        self.order_logger = logging.getLogger(f"autotrader_{self.user_id}")
        self.order_logger.setLevel(logging.INFO)
        self.order_logger.propagate = False

        # Remove existing handlers to avoid duplicates
        self.order_logger.handlers = []

        fh = logging.FileHandler(ORDER_LOG_FILE, mode='a')
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter('%(message)s'))
        self.order_logger.addHandler(fh)

    def _send_telegram_notification(self, chat_id: int, message: str) -> None:
        """Send a Telegram notification to the user."""
        if not self.send_notification or not self.bot_token:
            return

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = json.dumps({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }).encode('utf-8')

            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                logger.debug(f"[autotrader:{self.user_id}] Notification sent to chat_id {chat_id}")
        except urllib.error.URLError as e:
            logger.warning(f"[autotrader:{self.user_id}] Failed to send Telegram notification: {e}")
        except Exception as e:
            logger.warning(f"[autotrader:{self.user_id}] Unexpected error sending notification: {e}")

    def _send_signal_trade_notification(
        self,
        condition_id: str,
        signal_side: str,
        mapped_side: str,
        shares: float = None,
        amount: float = None,
        price: float = None,
        timeframe: str = None,
    ) -> None:
        """Send a Telegram notification about a signal trade execution."""
        if not self.send_notification or not self.bot_token:
            return

        # user_id is the Telegram user ID which can be used as chat_id
        chat_id = self.user_id

        # Build notification message
        timeframe_str = f" ({timeframe.upper()})" if timeframe else ""
        order_type_str = f" @ ${price:.2f}/share" if price else ""
        size_str = f"{shares} shares" if shares else f"${amount:.2f}"

        if price:
            total_value = shares * price
            size_str = f"{shares} shares ({total_value:.2f} USD)"

        message = (
            f"📡 *Signal Trade Executed*{timeframe_str}\n\n"
            f"Direction: *{signal_side}*\n"
            f"Market: *{condition_id[:40]}...*\n"
            f"Size: *{size_str}*{order_type_str}\n\n"
            f"Trade placed successfully ✅"
        )

        self._send_telegram_notification(chat_id, message)

    def _load_traded_condition_ids(self) -> None:
        """Load traded condition_ids from DB to prevent re-trading after restart."""
        try:
            # Create tracking table if not exists
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS traded_condition_ids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    condition_id TEXT,
                    traded_at INTEGER,
                    UNIQUE(user_id, condition_id)
                );
                """
            )

            rows = self.db.execute(
                "SELECT DISTINCT condition_id FROM traded_condition_ids WHERE user_id = ?;",
                (self.user_id,)
            ).fetchall()
            self.traded_condition_ids = {row[0] for row in rows if row[0]}
            logger.info(f"[autotrader:{self.user_id}] Loaded {len(self.traded_condition_ids)} previously traded condition_ids")
        except Exception as e:
            logger.warning(f"[autotrader:{self.user_id}] Could not load traded condition_ids: {e}")

    def _message_key(self, signal: dict) -> str:
        """Generate unique message key for a signal."""
        # Use signal_ts + condition_id as unique key
        signal_ts = signal.get("signal_ts") or signal.get("market_end_ts") or 0
        condition_id = signal.get("condition_id") or ""
        return f"{signal_ts}:{condition_id}"

    def _is_signal_recent(self, message_key: str) -> bool:
        """Check if we've already processed this signal."""
        return message_key in self.recent_signals

    def validate_signal_timing(self, signal: dict) -> tuple[bool, str]:
        """
        Check if signal is fresh enough to trade on.
        Returns (is_valid, reason).
        """
        signal_ts = signal.get("signal_ts") or signal.get("market_end_ts")
        if not signal_ts:
            return False, "No timestamp"

        now = int(time.time())
        age = now - signal_ts

        # Check if signal is too old
        if age > MAX_SIGNAL_AGE_SECONDS:
            return False, f"too old ({age}s)"

        # Check if market has already ended
        market_end_ts = signal.get("market_end_ts") or signal.get("end_ts")
        if market_end_ts and now > market_end_ts:
            return False, f"market ended ({now - market_end_ts}s ago)"

        return True, f"OK (age={age}s)"

    def resolve_market_for_signal(self, signal: dict) -> tuple[str, str] | None:
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
            logger.error(f"[autotrader:{self.user_id}] Failed to resolve {series_slug}: {e}")
            return None

    def map_signal_side(self, side: str, condition_id: str) -> str:
        """Map signal side (YES/NO) to market outcome (Up/Down)."""
        import market_cache

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

    def execute_trade(
        self,
        condition_id: str,
        side: str,
        signal: dict | None = None,
    ) -> str:
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
            if self.use_limit_orders:
                order_log["order_type"] = "limit"
                order_log["price"] = self.limit_order_price
                order_log["size"] = self.trade_amount_usd  # Now stores shares directly
                order_log["status"] = "dry_run"
                self.order_logger.info(json.dumps(order_log))
                return f"[DRY RUN] Would limit order: {side} {self.trade_amount_usd} shares @ ${self.limit_order_price} on {condition_id[:20]}..."
            else:
                order_log["order_type"] = "market"
                order_log["amount"] = self.trade_amount_usd
                order_log["status"] = "dry_run"
                self.order_logger.info(json.dumps(order_log))
                return f"[DRY RUN] Would trade: {side} on condition {condition_id[:20]}... ${self.trade_amount_usd}"

        try:
            # Map signal side (YES/NO) to market outcome (Up/Down)
            mapped_side = self.map_signal_side(side, condition_id)

            if self.use_limit_orders:
                # Use limit order with price cap to avoid liquidity/slippage issues
                # trade_amount_usd now stores shares directly
                shares = max(self.trade_amount_usd, MIN_ORDER_SHARES)
                order_log["order_type"] = "limit"
                order_log["price"] = self.limit_order_price
                order_log["size"] = shares
                order_log["signal_side"] = side
                order_log["mapped_side"] = mapped_side

                result = execute_limit_order_for_user(
                    db=self.db,
                    user_id=self.user_id,
                    side=mapped_side,
                    price=self.limit_order_price,
                    size=shares,
                    condition_id=condition_id,
                    order_side="BUY",
                )

                order_log["result"] = result
                order_log["status"] = "success" if "✅" in result else "failed"
                self.order_logger.info(json.dumps(order_log))

                # Send Telegram notification on success
                if "✅" in result:
                    self._send_signal_trade_notification(
                        condition_id=condition_id,
                        signal_side=side,
                        mapped_side=mapped_side,
                        shares=shares,
                        price=self.limit_order_price,
                        timeframe=signal_info.get("timeframe"),
                    )

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
                self.order_logger.info(json.dumps(order_log))

                # Send Telegram notification on success
                if "✅" in result:
                    self._send_signal_trade_notification(
                        condition_id=condition_id,
                        signal_side=side,
                        mapped_side=mapped_side,
                        amount=self.trade_amount_usd,
                        timeframe=signal_info.get("timeframe"),
                    )

                return result
        except Exception as e:
            order_log["error"] = str(e)
            order_log["status"] = "error"
            self.order_logger.info(json.dumps(order_log))
            logger.error(f"[autotrader:{self.user_id}] Trade execution failed: {e}")
            return f"Trade execution failed: {e}"

    def process_signal(self, signal: dict) -> tuple[bool, str]:
        """
        Process a single signal and potentially execute a trade.
        Returns (traded, result_message).
        """
        message_key = self._message_key(signal)
        if not message_key:
            return False, "No message key"

        # Safety: only execute trades for live signals.
        # Listener stores historical backfill as parsed signals in the same JSONL,
        # but we never want to execute those.
        src = str(signal.get("source") or "")
        if src and not src.startswith("live:"):
            self.recent_signals.append(message_key)
            return False, "Skip non-live signal"

        # Check if already processed
        if self._is_signal_recent(message_key):
            return False, "Already processed"

        # Check if already traded (double-check via signal_states)
        if message_key in self.signal_states:
            existing = self.signal_states[message_key]
            if existing.traded:
                logger.info(f"[autotrader:{self.user_id}] Already traded for signal {message_key[:20]}...")
                self.recent_signals.append(message_key)
                return False, "Already traded"

        # Validate timing FIRST
        valid, reason = self.validate_signal_timing(signal)
        if not valid:
            logger.info(f"[autotrader:{self.user_id}] Skipping signal: {reason}")
            self.recent_signals.append(message_key)
            return False, reason

        # Extract signal details
        side = signal.get("signal")  # YES or NO
        if not side:
            self.recent_signals.append(message_key)
            return False, "No signal side"

        # Resolve market
        resolution = self.resolve_market_for_signal(signal)
        if not resolution:
            logger.warning(f"[autotrader:{self.user_id}] Could not resolve market for signal")
            self.recent_signals.append(message_key)
            return False, "Could not resolve market"

        condition_id, event_slug = resolution

        # Check if we've already traded on this condition_id
        if condition_id in self.traded_condition_ids:
            logger.info(f"[autotrader:{self.user_id}] Already traded on market {condition_id[:16]}..., skipping")
            self.recent_signals.append(message_key)
            return False, "Already traded on this market"

        # Execute trade
        logger.info(f"[autotrader:{self.user_id}] Executing trade: {side} on {condition_id[:20]}...")
        result = self.execute_trade(condition_id, side, {
            **signal,
            "message_key": message_key,
        })

        # Record trade in DB immediately to persist across restarts
        if "✅" in result:
            try:
                self.db.execute(
                    "INSERT OR IGNORE INTO traded_condition_ids (user_id, condition_id, traded_at) VALUES (?, ?, ?);",
                    (self.user_id, condition_id, int(time.time()))
                )
            except Exception as e:
                logger.warning(f"[autotrader:{self.user_id}] Could not record traded condition_id: {e}")

        # Record state
        self.signal_states[message_key] = SignalState(
            message_key=message_key,
            signal_ts=signal.get("signal_ts", 0),
            timeframe=signal.get("timeframe"),
            side=side,
            market_end_ts=signal.get("market_end_ts") or signal.get("end_ts", 0),
            traded=True,
            trade_ts=int(time.time()),
            trade_result=result,
            condition_id=condition_id,
        )
        self.traded_condition_ids.add(condition_id)
        self.recent_signals.append(message_key)

        return True, result

    def clear_traded_condition_ids(self) -> int:
        """Clear the tracked condition_ids (reset autotrader)."""
        try:
            result = self.db.execute(
                "DELETE FROM traded_condition_ids WHERE user_id = ?;",
                (self.user_id,)
            ).rowcount
            self.traded_condition_ids.clear()
            logger.info(f"[autotrader:{self.user_id}] Cleared {result} tracked condition_ids")
            return result or 0
        except Exception as e:
            logger.error(f"[autotrader:{self.user_id}] Error clearing condition_ids: {e}")
            return 0


def execute_signal_trade_for_user(
    db: Any,
    user_id: int,
    signal: dict,
    trade_amount_usd: float = DEFAULT_TRADE_AMOUNT_USD,
    dry_run: bool = False,
    use_limit_orders: bool = USE_LIMIT_ORDERS,
    limit_order_price: float = LIMIT_ORDER_PRICE,
) -> tuple[bool, str]:
    """
    Convenience function to execute a single signal trade.

    Args:
        db: DatabaseManager instance
        user_id: User ID to trade as
        signal: Signal dict with keys like 'signal', 'timeframe', 'signal_ts', etc.
        trade_amount_usd: Amount to trade in USD
        dry_run: If True, don't execute actual trades
        use_limit_orders: If True, use limit orders instead of market orders
        limit_order_price: Max price per share for limit orders

    Returns:
        (success, message) tuple
    """
    manager = AutoTraderManager(
        db=db,
        user_id=user_id,
        trade_amount_usd=trade_amount_usd,
        dry_run=dry_run,
        use_limit_orders=use_limit_orders,
        limit_order_price=limit_order_price,
    )
    return manager.process_signal(signal)
