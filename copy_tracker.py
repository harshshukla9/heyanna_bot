#!/usr/bin/env python3
"""
Real-time Polymarket Copy Trading Tracker with Programmable Hooks.
Uses RTDS WebSocket to stream trades and execute copy trades instantly.

Hooks support:
- Amount scaling (fixed, percentage, proportional)
- Conditional filters (min/max trade size, outcomes, markets)
- Risk management (max daily loss, position limits)
- Custom expressions for trade logic
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import sqlite3

try:
    import websockets
    import aiohttp
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False


def load_env_file(path=".env"):
    """Load .env file into os.environ."""
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()


class HookExecutor:
    """
    Executable hook with programmable rules.
    Supports expressions, conditions, and custom logic.
    """

    def __init__(self, hook_data: dict):
        self.hook_id = hook_data.get("id", 0)
        self.follower_user_id = hook_data.get("follower_user_id", 0)
        self.leader_user_id = hook_data.get("leader_user_id")
        self.config = hook_data.get("config", {})
        self.enabled = hook_data.get("enabled", True)

        # Parse programmable rules
        self.rules = self._parse_rules()

    def _parse_rules(self) -> dict:
        """Parse hook config into executable rules."""
        cfg = self.config

        # Handle old config format (mode, max_usd_per_trade) -> new format (amount_mode)
        old_mode = cfg.get("mode", "")
        old_max = cfg.get("max_usd_per_trade", 0)
        old_fixed = cfg.get("fixed_usd_amount", 0)

        # Determine amount_mode from old config if not set
        amount_mode = cfg.get("amount_mode")
        if not amount_mode:
            if old_mode == "beginner":
                amount_mode = "fixed"
            elif old_mode == "fractional":
                amount_mode = "percentage"
            else:
                amount_mode = "proportional"

        return {
            # Amount calculation modes: fixed, percentage, proportional, formula
            "amount_mode": amount_mode,
            "fixed_amount": float(cfg.get("fixed_usd_amount") or old_fixed or 1.0),
            "percentage": float(cfg.get("percentage", 100.0)) / 100.0,
            "formula": cfg.get("amount_formula"),  # Custom formula string

            # Trade filters
            "min_trade_size": float(cfg.get("min_trade_size", 0)),
            "max_trade_size": float(cfg.get("max_trade_size") or old_max or float("inf")),
            "min_price": float(cfg.get("min_price", 0)),
            "max_price": float(cfg.get("max_price", 1)),

            # Outcome filters (empty = all outcomes allowed)
            "allowed_outcomes": [
                o.lower() for o in cfg.get("allowed_outcomes", [])
            ],
            "blocked_outcomes": [
                o.lower() for o in cfg.get("blocked_outcomes", [])
            ],

            # Market filters
            "allowed_markets": [
                m.lower() for m in cfg.get("allowed_markets", [])
            ],
            "blocked_markets": [
                m.lower() for m in cfg.get("blocked_markets", [])
            ],

            # Side filters
            "allowed_sides": [
                s.upper() for s in cfg.get("allowed_sides", [])
            ],

            # Risk management
            "max_daily_loss": float(cfg.get("max_daily_loss", float("inf"))),
            "max_position_size": float(cfg.get("max_position_size", float("inf"))),
            "max_trades_per_hour": int(cfg.get("max_trades_per_hour", 0)),

            # Conditional logic
            "condition_expression": cfg.get("condition"),  # Python-like condition
        }

    def should_copy_trade(self, trade: dict) -> tuple[bool, str]:
        """
        Determine if this trade should be copied based on rules.
        Returns (should_copy, reason).
        """
        rules = self.rules

        # Check trade size filters
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
        amount = size * price

        if amount < rules["min_trade_size"]:
            return False, f"Trade size ${amount:.2f} below minimum ${rules['min_trade_size']}"

        if amount > rules["max_trade_size"]:
            return False, f"Trade size ${amount:.2f} above maximum ${rules['max_trade_size']}"

        # Check price filters
        if rules["min_price"] > 0 and price < rules["min_price"]:
            return False, f"Price ${price:.4f} below minimum ${rules['min_price']}"

        if price > rules["max_price"]:
            return False, f"Price ${price:.4f} above maximum ${rules['max_price']}"

        # Check outcome filters
        outcome = (trade.get("outcome") or "").lower()
        if rules["allowed_outcomes"] and outcome not in rules["allowed_outcomes"]:
            return False, f"Outcome '{outcome}' not in allowed list"

        if outcome in rules["blocked_outcomes"]:
            return False, f"Outcome '{outcome}' is blocked"

        # Check side filters
        side = (trade.get("side") or "").upper()
        if rules["allowed_sides"] and side not in rules["allowed_sides"]:
            return False, f"Side '{side}' not in allowed list"

        # Check market filters
        market_slug = (trade.get("slug") or trade.get("eventSlug") or "").lower()
        if rules["allowed_markets"] and not any(
            m in market_slug for m in rules["allowed_markets"]
        ):
            if rules["allowed_markets"]:
                return False, f"Market not in allowed list"

        if any(m in market_slug for m in rules["blocked_markets"]):
            return False, f"Market is blocked"

        # Evaluate custom condition expression
        if rules["condition_expression"]:
            try:
                ctx = {
                    "trade": trade,
                    "amount": amount,
                    "price": price,
                    "size": size,
                    "outcome": outcome,
                    "side": side,
                }
                if not self._eval_condition(rules["condition_expression"], ctx):
                    return False, "Custom condition not met"
            except Exception as e:
                return False, f"Condition evaluation error: {e}"

        return True, "Rules passed"

    def _eval_condition(self, expr: str, ctx: dict) -> bool:
        """
        Evaluate a condition expression.
        Supports simple Python-like expressions with safe evaluation.
        """
        # Whitelist of allowed operations
        allowed_names = ["trade", "amount", "price", "size", "outcome", "side"]

        # Build safe namespace
        safe_ctx = {name: ctx.get(name) for name in allowed_names}
        safe_ctx["abs"] = abs
        safe_ctx["max"] = max
        safe_ctx["min"] = min
        safe_ctx["len"] = len
        safe_ctx["str"] = str
        safe_ctx["float"] = float
        safe_ctx["int"] = int

        try:
            # Compile and evaluate
            code = compile(expr, "<string>", "eval", flags=0)
            # Check for dangerous names in bytecode
            for name in code.co_names:
                if name not in safe_ctx:
                    raise ValueError(f"Name '{name}' not allowed")
            return bool(eval(code, {"__builtins__": {}}, safe_ctx))
        except Exception as e:
            raise ValueError(f"Invalid condition: {e}")

    def calculate_amount(self, leader_amount: float, trade: dict) -> float:
        """Calculate follower trade amount based on rules."""
        rules = self.rules
        mode = rules["amount_mode"]

        # Handle old config format: size_multiplier
        size_mult = self.config.get("size_multiplier", 1.0)

        if mode == "fixed":
            return rules["fixed_amount"] * size_mult

        elif mode == "percentage":
            return leader_amount * rules["percentage"] * size_mult

        elif mode == "formula" and rules["formula"]:
            try:
                ctx = {
                    "leader_amount": leader_amount,
                    "amount": leader_amount,
                    "price": float(trade.get("price") or 0),
                    "size": float(trade.get("size") or 0),
                }
                result = self._eval_condition(rules["formula"], ctx)
                return float(result) * size_mult if result else leader_amount
            except Exception:
                return leader_amount * size_mult

        else:  # proportional (default)
            return leader_amount * size_mult

    def get_last_seen_ts(self) -> int:
        """Get last seen timestamp from config."""
        return int(self.config.get("last_seen_ts") or 0)

    def update_last_seen(self, ts: int) -> dict:
        """Update last seen timestamp, return new config."""
        new_config = self.config.copy()
        new_config["last_seen_ts"] = ts
        return new_config


class CopyTracker:
    """
    Real-time copy trading tracker using RTDS WebSocket.
    Tracks multiple wallets and executes copy trades instantly.
    """

    def __init__(self, db_path: str = "app_data.sqlite3"):
        self.db_path = db_path
        self.tracked_wallets: Dict[str, List[HookExecutor]] = {}
        self.running = False
        self.trade_callback: Optional[Callable] = None

        # Risk tracking per follower
        self.daily_losses: Dict[int, float] = defaultdict(float)
        self.trade_counts: Dict[int, List[float]] = defaultdict(list)

    def get_db_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def load_hooks(self) -> Dict[str, List[dict]]:
        """Load all enabled copy trading hooks from database."""
        logger = logging.getLogger(__name__)

        conn = self.get_db_connection()
        try:
            # First check what's in the database
            all_hooks = conn.execute(
                """
                SELECT id, follower_user_id, leader_user_id, config, enabled
                FROM copy_trading_hooks;
                """
            ).fetchall()

            logger.info(f"[HOOK] Found {len(all_hooks)} total hooks in database")

            rows = conn.execute(
                """
                SELECT id, follower_user_id, leader_user_id, config, enabled
                FROM copy_trading_hooks
                WHERE enabled = 1;
                """
            ).fetchall()

            logger.info(f"[HOOK] Found {len(rows)} enabled hooks")

            hooks_by_wallet = defaultdict(list)

            for row in rows:
                try:
                    config_dict = json.loads(row["config"] or "{}")
                    leader_addr = (config_dict.get("leader_address") or "").strip().lower()

                    logger.info(f"[HOOK] Hook {row['id']}: leader_address={leader_addr}")

                    hook = HookExecutor({
                        "id": row["id"],
                        "follower_user_id": row["follower_user_id"],
                        "leader_user_id": row["leader_user_id"],
                        "config": config_dict,
                        "enabled": row["enabled"]
                    })

                    if hook.enabled and leader_addr:
                        hooks_by_wallet[leader_addr].append(hook)
                        logger.info(f"[HOOK] Loaded hook {hook.hook_id} for wallet {leader_addr}")
                    elif not leader_addr:
                        logger.warning(f"[HOOK] Hook {row['id']} has no leader_address in config")

                except Exception as e:
                    logger.error(f"[HOOK] Error loading hook: {e}", exc_info=True)

            self.tracked_wallets = dict(hooks_by_wallet)

            # Summary
            total_wallets = len(self.tracked_wallets)
            total_hooks = sum(len(h) for h in self.tracked_wallets.values())
            logger.info(f"[HOOK] Active: {total_hooks} hooks for {total_wallets} wallets")

            if total_hooks == 0:
                logger.warning("[HOOK] No active hooks loaded! Check your copy_trading_hooks table.")

            return {
                addr: [{"id": h.hook_id, "follower_id": h.follower_user_id}
                       for h in hooks]
                for addr, hooks in self.tracked_wallets.items()
            }

        finally:
            conn.close()

    def update_hook_config(self, hook_id: int, new_config: dict) -> bool:
        """Update hook configuration in database."""
        conn = self.get_db_connection()
        try:
            conn.execute(
                "UPDATE copy_trading_hooks SET config = ? WHERE id = ?",
                (json.dumps(new_config), hook_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def log_hook_execution(
        self,
        hook_id: int,
        follower_user_id: int,
        leader_address: str,
        trade_side: str,
        trade_outcome: str,
        trade_amount: float,
        trade_price: float,
        trade_size: float,
        follower_amount: float,
        condition_id: str,
        market_title: str,
        status: str,
        error_message: str = None
    ) -> int:
        """Log a hook execution to the database."""
        conn = self.get_db_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO hook_logs (
                    hook_id, follower_user_id, leader_address,
                    trade_side, trade_outcome, trade_amount, trade_price, trade_size,
                    follower_amount, condition_id, market_title,
                    status, error_message, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    hook_id, follower_user_id, leader_address,
                    trade_side, trade_outcome, trade_amount, trade_price, trade_size,
                    follower_amount, condition_id, market_title[:255] if market_title else "",
                    status, error_message, int(time.time())
                )
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_hook_logs(
        self,
        follower_user_id: int = None,
        hook_id: int = None,
        limit: int = 50
    ) -> list[dict]:
        """Get hook execution logs."""
        conn = self.get_db_connection()
        try:
            query = "SELECT * FROM hook_logs WHERE 1=1"
            params = []

            if follower_user_id:
                query += " AND follower_user_id = ?"
                params.append(follower_user_id)

            if hook_id:
                query += " AND hook_id = ?"
                params.append(hook_id)

            query += " ORDER BY executed_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _check_risk_limits(self, follower_id: int, amount: float) -> tuple[bool, str]:
        """Check if trade passes risk limits."""
        now = time.time()
        rules = None

        # Find hook rules for this follower
        for hooks in self.tracked_wallets.values():
            for hook in hooks:
                if hook.follower_user_id == follower_id:
                    rules = hook.rules
                    break
            if rules:
                break

        if not rules:
            return True, "No risk rules"

        # Check max trades per hour
        if rules["max_trades_per_hour"] > 0:
            one_hour_ago = now - 3600
            recent_trades = [t for t in self.trade_counts[follower_id] if t > one_hour_ago]
            self.trade_counts[follower_id] = recent_trades

            if len(recent_trades) >= rules["max_trades_per_hour"]:
                return False, f"Max {rules['max_trades_per_hour']} trades/hour reached"

        # Check max daily loss
        if rules["max_daily_loss"] < float("inf"):
            if self.daily_losses[follower_id] + amount > rules["max_daily_loss"]:
                return False, f"Max daily loss ${rules['max_daily_loss']}"

        return True, "Risk check passed"

    async def process_trade(self, trade: dict):
        """Process a trade and execute copy trades for followers."""
        logger = logging.getLogger(__name__)

        proxy_wallet = (trade.get("proxyWallet") or "").lower()

        if not proxy_wallet:
            return

        # Debug: log all trades received
        logger.debug(f"[TRADE] Received trade from {proxy_wallet[:20]}...")

        # Check if this wallet is being tracked
        hooks = self.tracked_wallets.get(proxy_wallet)
        if not hooks:
            logger.debug(f"[TRADE] Wallet {proxy_wallet[:20]}... not being tracked")
            return

        logger.info(f"[TRADE] Wallet {proxy_wallet[:20]}... IS being tracked with {len(hooks)} hook(s)")

        condition_id = trade.get("conditionId") or trade.get("asset")
        outcome = trade.get("outcome")
        market_title = trade.get("title", "")

        if not condition_id or not outcome:
            return

        leader_side = trade.get("side", "BUY").upper()
        leader_price = float(trade.get("price") or 0)
        leader_size = float(trade.get("size") or 0)
        leader_amount = leader_size * leader_price

        for hook in hooks:
            try:
                # Check if trade should be copied
                should_copy, reason = hook.should_copy_trade(trade)
                if not should_copy:
                    logger.info(f"[HOOK {hook.hook_id}] Skipping: {reason}")
                    # Log skipped trade
                    self.log_hook_execution(
                        hook_id=hook.hook_id,
                        follower_user_id=hook.follower_user_id,
                        leader_address=proxy_wallet,
                        trade_side=leader_side,
                        trade_outcome=outcome,
                        trade_amount=leader_amount,
                        trade_price=leader_price,
                        trade_size=leader_size,
                        follower_amount=0,
                        condition_id=condition_id,
                        market_title=market_title,
                        status="skipped",
                        error_message=reason
                    )
                    continue

                # Calculate follower amount
                follower_amount = hook.calculate_amount(leader_amount, trade)
                if follower_amount <= 0:
                    continue

                # Check risk limits
                risk_ok, risk_reason = self._check_risk_limits(
                    hook.follower_user_id, follower_amount
                )
                if not risk_ok:
                    logger.info(f"[HOOK {hook.hook_id}] Risk limit: {risk_reason}")
                    self.log_hook_execution(
                        hook_id=hook.hook_id,
                        follower_user_id=hook.follower_user_id,
                        leader_address=proxy_wallet,
                        trade_side=leader_side,
                        trade_outcome=outcome,
                        trade_amount=leader_amount,
                        trade_price=leader_price,
                        trade_size=leader_size,
                        follower_amount=0,
                        condition_id=condition_id,
                        market_title=market_title,
                        status="risk_limit",
                        error_message=risk_reason
                    )
                    continue

                # Determine order side
                order_side = "BUY" if leader_side == "BUY" else "SELL"

                # Log to console
                logger.info(
                    f"[COPY] Hook {hook.hook_id}: "
                    f"Follower {hook.follower_user_id} -> "
                    f"{outcome} ${follower_amount:.2f} @ {condition_id[:20]}..."
                )

                # Log successful execution
                log_id = self.log_hook_execution(
                    hook_id=hook.hook_id,
                    follower_user_id=hook.follower_user_id,
                    leader_address=proxy_wallet,
                    trade_side=leader_side,
                    trade_outcome=outcome,
                    trade_amount=leader_amount,
                    trade_price=leader_price,
                    trade_size=leader_size,
                    follower_amount=follower_amount,
                    condition_id=condition_id,
                    market_title=market_title,
                    status="pending",
                    error_message=None
                )

                # Execute via callback
                try:
                    if self.trade_callback:
                        await self.trade_callback(
                            follower_user_id=hook.follower_user_id,
                            outcome=outcome,
                            amount_usd=follower_amount,
                            condition_id=condition_id,
                            order_side=order_side,
                            copied_from_address=proxy_wallet,
                            hook_id=hook.hook_id
                        )

                    # Update log status to success
                    conn = self.get_db_connection()
                    try:
                        conn.execute(
                            "UPDATE hook_logs SET status = ? WHERE id = ?",
                            ("success", log_id)
                        )
                        conn.commit()
                    finally:
                        conn.close()

                except Exception as e:
                    # Update log status to failed
                    conn = self.get_db_connection()
                    try:
                        conn.execute(
                            "UPDATE hook_logs SET status = ?, error_message = ? WHERE id = ?",
                            ("failed", str(e), log_id)
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    logger.error(f"[HOOK {hook.hook_id}] Execution failed: {e}")

                # Update last seen
                trade_ts = int(trade.get("timestamp") or 0)
                new_config = hook.update_last_seen(trade_ts)
                self.update_hook_config(hook.hook_id, new_config)

                # Track for risk management
                self.trade_counts[hook.follower_user_id].append(time.time())

            except Exception as e:
                print(f"[HOOK {hook.hook_id}] Error: {e}")

    async def run(self):
        """Run the real-time tracker."""
        if not DEPS_AVAILABLE:
            print("Install dependencies: uv pip install websockets aiohttp")
            return

        self.running = True
        uri = "wss://ws-live-data.polymarket.com"

        print("=" * 60)
        print("POLYMARKET COPY TRADING TRACKER (WebSocket)")
        print("=" * 60)

        # Initial load of hooks
        self.load_hooks()
        print("=" * 60)

        reconnect_delay = 1
        max_reconnect_delay = 60

        while self.running:
            try:
                async with websockets.connect(uri, ping_interval=5) as ws:
                    reconnect_delay = 1

                    subscribe_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "activity",
                                "type": "trades"
                            }
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    print(f"[{datetime.now()}] Connected to RTDS WebSocket")

                    async for message in ws:
                        if not self.running:
                            break

                        if not message.strip():
                            continue

                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        if data.get("topic") == "activity":
                            trade = data.get("payload", {})
                            await self.process_trade(trade)

            except websockets.exceptions.ConnectionClosed as e:
                print(f"[{datetime.now()}] WebSocket closed: {e}")
            except Exception as e:
                print(f"[{datetime.now()}] Error: {e}")

            if self.running:
                print(f"[{datetime.now()}] Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    def stop(self):
        """Stop the tracker."""
        self.running = False


# Global tracker instance
_tracker: Optional[CopyTracker] = None


def get_tracker(db_path: str = "app_data.sqlite3") -> CopyTracker:
    """Get or create the global tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = CopyTracker(db_path=db_path)
    return _tracker


async def start_copy_tracking(
    db_path: str = "app_data.sqlite3",
    trade_callback: Optional[Callable] = None
):
    """Start the copy trading tracker."""
    tracker = get_tracker(db_path)
    tracker.trade_callback = trade_callback
    await tracker.run()


def stop_copy_tracking():
    """Stop the copy trading tracker."""
    if _tracker:
        _tracker.stop()
