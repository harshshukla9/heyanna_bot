#!/usr/bin/env python3
"""
Autotrader with integrated Telegram listener.

This script:
1. Listens to Telegram signals in real-time
2. Automatically resolves the correct Polymarket market (5m/15m series)
3. Executes trades immediately when a signal arrives
4. Logs all trades and results

Requirements:
- TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SIGNAL_CHAT (for Telegram)
- USER_ID (to trade as)
- DB_PATH (SQLite database)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure repo root is importable
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
from telethon import TelegramClient, events

from database_manager import DatabaseManager
from trading import execute_trade_for_user
try:
    from .series_5m_order_app import _resolve_latest_event_for_series_slug
except ImportError:
    from series_5m_order_app import _resolve_latest_event_for_series_slug


# Mapping from signal timeframe to Polymarket series slug
TIMEFRAME_TO_SERIES = {
    "1m": "btc-up-or-down-1m",
    "5m": "btc-up-or-down-5m",
    "15m": "btc-up-or-down-15m",
    "30m": "btc-up-or-down-30m",
    "1h": "btc-up-or-down-1h",
}

DEFAULT_TRADE_AMOUNT_USD = 3.0  # 3 USD per trade
MAX_SIGNAL_AGE_SECONDS = 600  # 10 minutes

# Limit order settings
USE_LIMIT_ORDERS = True  # Use limit orders instead of market orders
LIMIT_ORDER_PRICE = 0.25  # Max price per share (25 cents) - testing
MIN_ORDER_SHARES = 5  # Polymarket minimum order size

# Note: LIMIT_ORDER_SIZE_USD removed - now uses max(trade_amount_usd / price, MIN_ORDER_SHARES)


def parse_signal_from_text(text: str) -> dict[str, Any] | None:
    """Lightweight signal parser - matches telegram_announcement_listener logic."""
    src = (text or "").strip()
    if not src:
        return None
    low = src.lower()

    # Only parse signal announcements
    if "signal" not in low or "—" not in src:
        return None
    # Skip outcome reports
    import re
    if re.search(r"LOSS\s*—|WIN\s*—", src):
        return None

    # BTC signals
    asset = "BTC"

    # Parse timeframe
    timeframe = None
    if re.search(r"\b(1m|1\s*min(?:ute)?s?)\b", low):
        timeframe = "1m"
    elif re.search(r"\b(5m|5\s*min(?:ute)?s?)\b", low):
        timeframe = "5m"
    elif re.search(r"\b(15m|15\s*min(?:ute)?s?)\b", low):
        timeframe = "15m"
    elif re.search(r"\b(30m|30\s*min(?:ute)?s?)\b", low):
        timeframe = "30m"
    elif re.search(r"\b(1h|60\s*min(?:ute)?s?)\b", low):
        timeframe = "1h"

    # Parse side
    side = None
    if re.search(r"\b(long|buy\s*yes|yes|bull|up|call)\b", low) or "🟢" in src:
        side = "YES"
    elif re.search(r"\b(short|buy\s*no|no|bear|down|put)\b", low) or "🔴" in src or re.search(r"▼\s*down", low):
        side = "NO"

    if not side:
        return None
    if not timeframe:
        return None

    return {
        "asset": asset,
        "timeframe": timeframe,
        "signal": side,
        "direction": "UP" if side == "YES" else "DOWN",
    }


class AutoTraderListener:
    """Telegram listener with integrated autotrading."""

    def __init__(
        self,
        db_path: str,
        user_id: int,
        telegram_api_id: int,
        telegram_api_hash: str,
        telegram_chat: str,
        telegram_session: str = "autotrader_listener",
        trade_amount_usd: float = DEFAULT_TRADE_AMOUNT_USD,
        dry_run: bool = False,
    ):
        self.db_path = db_path
        self.user_id = user_id
        self.telegram_api_id = telegram_api_id
        self.telegram_api_hash = telegram_api_hash
        self.telegram_chat = telegram_chat
        self.telegram_session = telegram_session
        self.trade_amount_usd = trade_amount_usd
        self.dry_run = dry_run

        self.db = DatabaseManager(db_path=db_path)
        self.db.init_schema()

        # Load user info
        user = self.db.get_user(user_id)
        if not user:
            raise RuntimeError(f"User {user_id} not found in database.")
        self.user = user

        self.seen_messages: set[str] = set()

    def _message_key(self, chat_id: int, msg_id: int) -> str:
        return f"{chat_id}:{msg_id}"

    def _resolve_market(self, timeframe: str) -> tuple[str, str] | None:
        """Resolve market condition_id for timeframe."""
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
            print(f"Failed to resolve {series_slug}: {e}", file=sys.stderr)
            return None

    def _execute_trade(self, condition_id: str, side: str) -> str:
        """Execute a trade."""
        if self.dry_run:
            if USE_LIMIT_ORDERS:
                return f"[DRY RUN] Limit order: {side} @ ${LIMIT_ORDER_PRICE} on {condition_id[:20]}..."
            return f"[DRY RUN] {side} on {condition_id[:20]}... ${self.trade_amount_usd}"

        try:
            if USE_LIMIT_ORDERS:
                # Use limit order with price cap to avoid liquidity/slippage issues
                # Polymarket minimum: 5 shares
                from trading import execute_limit_order_for_user
                shares = max(self.trade_amount_usd / LIMIT_ORDER_PRICE, MIN_ORDER_SHARES)
                result = execute_limit_order_for_user(
                    db=self.db,
                    user_id=self.user_id,
                    side=side,
                    price=LIMIT_ORDER_PRICE,
                    size=shares,
                    condition_id=condition_id,
                    order_side="BUY",
                )
                return result
            else:
                result = execute_trade_for_user(
                    db=self.db,
                    user_id=self.user_id,
                    side=side,
                    amount=self.trade_amount_usd,
                    condition_id=condition_id,
                    order_side="BUY",
                )
                return result
        except Exception as e:
            return f"Trade failed: {e}"

    async def run(self):
        """Start the Telegram listener with autotrading."""
        print("[autotrader] Starting Telegram autotrader")
        print(f"[autotrader] User: {self.user.get('username')} (ID: {self.user_id})")
        print(f"[autotrader] Trade amount: ${self.trade_amount_usd}")
        print(f"[autotrader] Dry run: {self.dry_run}")
        print(f"[autotrader] Chat: {self.telegram_chat}")
        print()

        if self.dry_run:
            print("⚠️  DRY RUN MODE - No actual trades will be executed!")
            print()

        client = TelegramClient(self.telegram_session, self.telegram_api_id, self.telegram_api_hash)
        await client.start()

        chat = await client.get_entity(self.telegram_chat)
        chat_id = int(getattr(chat, "id", 0) or 0)
        chat_title = getattr(chat, "title") or getattr(chat, "username") or self.telegram_chat
        print(f"[autotrader] Connected to chat: {chat_title} (id={chat_id})")

        @client.on(events.NewMessage(chats=chat))
        async def handler(event):
            msg_id = event.id
            key = self._message_key(chat_id, msg_id)

            if key in self.seen_messages:
                return
            self.seen_messages.add(key)

            text = event.message.text or ""
            parsed = parse_signal_from_text(text)

            if not parsed:
                return

            print(f"\n[signal] Detected: {parsed['signal']} {parsed['timeframe']}")

            # Resolve market
            resolution = self._resolve_market(parsed["timeframe"])
            if not resolution:
                print(f"[trade] Failed to resolve market")
                return

            condition_id, event_slug = resolution
            print(f"[trade] Market: {event_slug}")

            # Execute trade
            result = self._execute_trade(condition_id, parsed["signal"])
            print(f"[trade] Result: {result[:150]}...")

        print("[autotrader] Listening for signals...")
        await client.run_until_disconnected()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram autotrader with integrated listener")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH") or "app_data.sqlite3")
    parser.add_argument("--user-id", type=int, default=int(os.getenv("USER_ID") or "0"))
    parser.add_argument("--telegram-api-id", type=int, default=int(os.getenv("TELEGRAM_API_ID") or "0"))
    parser.add_argument("--telegram-api-hash", default=os.getenv("TELEGRAM_API_HASH") or "")
    parser.add_argument("--telegram-chat", default=os.getenv("TELEGRAM_SIGNAL_CHAT") or "")
    parser.add_argument("--telegram-session", default="autotrader_listener")
    parser.add_argument("--amount", type=float, default=DEFAULT_TRADE_AMOUNT_USD)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    if not args.user_id:
        print("Error: USER_ID required", file=sys.stderr)
        return 1
    if not args.telegram_api_id or not args.telegram_api_hash or not args.telegram_chat:
        print("Error: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SIGNAL_CHAT required", file=sys.stderr)
        return 1

    try:
        listener = AutoTraderListener(
            db_path=args.db_path,
            user_id=args.user_id,
            telegram_api_id=args.telegram_api_id,
            telegram_api_hash=args.telegram_api_hash,
            telegram_chat=args.telegram_chat,
            telegram_session=args.telegram_session,
            trade_amount_usd=args.amount,
            dry_run=args.dry_run,
        )
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        print("\n[autotrader] Stopped.")
    except Exception as e:
        print(f"[autotrader] Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
