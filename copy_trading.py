#!/usr/bin/env python3
"""
Simplified Copy Trading System.
Real-time WebSocket-based trade copying from leader wallets to followers.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


def load_env(path=".env"):
    """Load environment variables from .env file."""
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                import os
                os.environ[k.strip()] = v.strip()


class CopyTradingManager:
    """
    Manage copy trading hooks and execute trades in real-time.
    """

    def __init__(self, db_path: str = "app_data.sqlite3"):
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)
        self.tracked: Dict[str, List[Dict]] = {}  # leader_address -> [hooks]
        self.running = False
        self.on_trade: Optional[Callable] = None
        # Position tracking for fractional sells: {(leader_address, condition_id, outcome): {"shares": float}}
        self._positions: Dict[str, Dict[str, float]] = {}

    def get_conn(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def init_schema(self):
        """Initialize copy trading tables."""
        conn = self.get_conn()
        try:
            # Main hooks table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copy_hooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    follower_user_id INTEGER NOT NULL,
                    leader_address TEXT NOT NULL,
                    config TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_seen_ts INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(follower_user_id, leader_address)
                );
            """)

            # Execution logs
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copy_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hook_id INTEGER NOT NULL,
                    follower_user_id INTEGER NOT NULL,
                    leader_address TEXT,
                    trade_side TEXT,
                    trade_outcome TEXT,
                    trade_amount REAL,
                    trade_price REAL,
                    trade_size REAL,
                    follower_amount REAL,
                    condition_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    executed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (hook_id) REFERENCES copy_hooks(id)
                );
            """)

            conn.commit()
            self.logger.info("[COPY] Schema initialized")
        finally:
            conn.close()

    def add_hook(
        self,
        follower_user_id: int,
        leader_address: str,
        config: Dict = None
    ) -> int:
        """Add a new copy hook."""
        leader_address = leader_address.lower().strip()
        config = config or {}

        conn = self.get_conn()
        try:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO copy_hooks
                (follower_user_id, leader_address, config, enabled, created_at)
                VALUES (?, ?, ?, 1, strftime('%s', 'now'));
                """,
                (follower_user_id, leader_address, json.dumps(config))
            )
            conn.commit()
            self.logger.info(f"[COPY] Added hook: follower={follower_user_id} leader={leader_address}")
            self.reload()
            return cur.lastrowid or 0
        finally:
            conn.close()

    def remove_hook(self, follower_user_id: int, leader_address: str) -> bool:
        """Remove a copy hook."""
        leader_address = leader_address.lower().strip()

        conn = self.get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM copy_hooks WHERE follower_user_id = ? AND leader_address = ?",
                (follower_user_id, leader_address)
            )
            conn.commit()
            self.reload()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_hook_enabled(self, follower_user_id: int, leader_address: str, enabled: bool):
        """Enable or disable a hook."""
        leader_address = leader_address.lower().strip()

        conn = self.get_conn()
        try:
            conn.execute(
                "UPDATE copy_hooks SET enabled = ? WHERE follower_user_id = ? AND leader_address = ?",
                (1 if enabled else 0, follower_user_id, leader_address)
            )
            conn.commit()
            self.reload()
        finally:
            conn.close()

    def update_last_seen(self, hook_id: int, ts: int):
        """Update last seen timestamp for a hook."""
        conn = self.get_conn()
        try:
            conn.execute(
                "UPDATE copy_hooks SET last_seen_ts = ? WHERE id = ?",
                (ts, hook_id)
            )
            conn.commit()
        finally:
            conn.close()

    def log_execution(
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
        status: str,
        error: str = None
    ):
        """Log a hook execution."""
        conn = self.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO copy_logs (
                    hook_id, follower_user_id, leader_address,
                    trade_side, trade_outcome, trade_amount, trade_price, trade_size,
                    follower_amount, condition_id, status, error, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'));
                """,
                (
                    hook_id, follower_user_id, leader_address,
                    trade_side, trade_outcome, trade_amount, trade_price, trade_size,
                    follower_amount, condition_id, status, error
                )
            )
            conn.commit()
        finally:
            conn.close()

    def get_logs(
        self,
        follower_user_id: int = None,
        hook_id: int = None,
        limit: int = 100
    ) -> List[Dict]:
        """Get execution logs."""
        conn = self.get_conn()
        try:
            query = "SELECT * FROM copy_logs WHERE 1=1"
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

    def reload(self):
        """Reload hooks from database."""
        conn = self.get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, follower_user_id, leader_address, config, enabled, last_seen_ts
                FROM copy_hooks WHERE enabled = 1;
                """
            ).fetchall()

            self.tracked = defaultdict(list)

            for row in rows:
                config = json.loads(row["config"] or "{}")
                leader = (row["leader_address"] or "").lower()

                if leader:
                    self.tracked[leader].append({
                        "id": row["id"],
                        "follower_user_id": row["follower_user_id"],
                        "leader_address": leader,
                        "config": config,
                        "last_seen_ts": row["last_seen_ts"] or 0
                    })

            self.logger.info(f"[COPY] Reloaded {len(self.tracked)} tracked wallets")
        finally:
            conn.close()

    def calculate_amount(self, config: Dict, leader_amount: float, leader_sell_pct: float = 1.0) -> float:
        """Calculate follower trade amount.

        Args:
            config: Hook configuration
            leader_amount: Leader's trade amount in USD
            leader_sell_pct: For SELL orders, what percentage of position leader is selling (0.0-1.0)
        """
        mode = config.get("mode", "proportional")

        if mode == "fixed":
            result = float(config.get("fixed_usd_amount", 1.0))
        elif mode == "percentage":
            pct = float(config.get("percentage", 100)) / 100
            result = leader_amount * pct
        elif mode == "multiplier":
            mult = float(config.get("multiplier", 1.0))
            result = leader_amount * mult
        else:
            # Default/proportional/beginner modes
            result = leader_amount

        # For fractional sells, scale by the sell percentage
        if leader_sell_pct < 1.0:
            result = result * leader_sell_pct

        # Apply minimum floor of $1.00 to meet exchange requirements
        return max(result, 1.0)

    def should_copy(self, trade: Dict, config: Dict) -> tuple:
        """Check if trade should be copied. Returns (should_copy, reason)."""
        amount = float(trade.get("amount") or 0)
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)

        # Min trade size
        min_size = float(config.get("min_trade_size", 0))
        if amount < min_size:
            return False, f"Below min size ${min_size}"

        # Max trade size
        max_size = float(config.get("max_trade_size", 999999))
        if amount > max_size:
            return False, f"Above max size ${max_size}"

        # Outcome filter
        allowed = config.get("allowed_outcomes", [])
        outcome = (trade.get("outcome") or "").lower()
        if allowed and outcome not in [o.lower() for o in allowed]:
            return False, f"Outcome not allowed"

        return True, "OK"

    async def process_trade(self, trade: Dict):
        """Process a trade and execute copy trades."""
        leader = (trade.get("proxyWallet") or trade.get("maker") or "").lower()

        if not leader or leader not in self.tracked:
            return

        hooks = self.tracked.get(leader, [])
        if not hooks:
            return

        condition_id = trade.get("conditionId") or trade.get("asset")
        outcome = trade.get("outcome") or ""
        outcome_token_id = trade.get("tokenID") or trade.get("token_id")
        oi_raw = trade.get("outcomeIndex")
        try:
            outcome_index = int(oi_raw) if oi_raw is not None and str(oi_raw).strip() != "" else None
        except (TypeError, ValueError):
            outcome_index = None
        side = (trade.get("side") or "BUY").upper()
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
        amount = size * price
        ts = int(trade.get("timestamp") or 0)

        # Track leader's position for fractional sells
        pos_key = f"{leader}:{condition_id}:{outcome}"
        sell_pct = 1.0  # Default: full position

        if side == "BUY":
            # Add to position
            if pos_key not in self._positions:
                self._positions[pos_key] = {"shares": 0.0}
            self._positions[pos_key]["shares"] += size
        elif side == "SELL":
            # Calculate what percentage of position is being sold
            current_shares = self._positions.get(pos_key, {}).get("shares", 0)
            if current_shares > 0:
                sell_pct = min(1.0, size / current_shares)  # Percentage being sold
                self._positions[pos_key]["shares"] = max(0, current_shares - size)
            # If no position tracked, sell_pct stays at 1.0 (sell full amount)

        for hook in hooks:
            hook_id = hook["id"]
            follower_id = hook["follower_user_id"]
            config = hook["config"]
            last_ts = hook["last_seen_ts"]

            # Skip old trades
            if ts <= last_ts:
                continue

            # Check if should copy
            should, reason = self.should_copy(trade, config)
            if not should:
                self.log_execution(
                    hook_id, follower_id, leader, side, outcome,
                    amount, price, size, 0, condition_id,
                    "skipped", reason
                )
                continue

            # Calculate amount (with fractional sell support)
            follower_amount = self.calculate_amount(config, amount, sell_pct if side == "SELL" else 1.0)
            if follower_amount <= 0:
                continue

            # Determine order side
            order_side = "BUY" if side == "BUY" else "SELL"

            # Log as pending
            self.log_execution(
                hook_id, follower_id, leader, side, outcome,
                amount, price, size, follower_amount, condition_id,
                "pending", None
            )

            # Execute trade
            trade_result = None
            try:
                if self.on_trade:
                    trade_result = await self.on_trade(
                        follower_user_id=follower_id,
                        outcome=outcome,
                        amount_usd=follower_amount,
                        condition_id=condition_id,
                        order_side=order_side,
                        leader_address=leader,
                        hook_id=hook_id,
                        token_id=outcome_token_id,
                        outcome_index=outcome_index,
                    )

                # Check if trade succeeded based on result
                is_success = trade_result is None or "✅" in str(trade_result) or "EXECUTED" in str(trade_result)

                # Update status based on result
                conn = self.get_conn()
                try:
                    latest = conn.execute(
                        "SELECT id FROM copy_logs WHERE hook_id = ? ORDER BY id DESC LIMIT 1",
                        (hook_id,)
                    ).fetchone()
                    if latest:
                        if is_success:
                            conn.execute(
                                "UPDATE copy_logs SET status = 'success' WHERE id = ?",
                                (latest["id"],)
                            )
                        else:
                            conn.execute(
                                "UPDATE copy_logs SET status = 'failed', error = ? WHERE id = ?",
                                (str(trade_result) if trade_result else "Unknown error", latest["id"])
                            )
                    conn.commit()
                finally:
                    conn.close()

                if not is_success:
                    self.logger.error(f"[COPY] Hook {hook_id} failed: {trade_result}")

            except Exception as e:
                # Update status to failed
                conn = self.get_conn()
                try:
                    latest = conn.execute(
                        "SELECT id FROM copy_logs WHERE hook_id = ? ORDER BY id DESC LIMIT 1",
                        (hook_id,)
                    ).fetchone()
                    if latest:
                        conn.execute(
                            "UPDATE copy_logs SET status = 'failed', error = ? WHERE id = ?",
                            (str(e), latest["id"])
                        )
                    conn.commit()
                finally:
                    conn.close()
                self.logger.error(f"[COPY] Hook {hook_id} failed: {e}")

            # Update last seen
            self.update_last_seen(hook_id, ts)

    async def run(self):
        """Run the copy trading daemon."""
        if not WEBSOCKETS_AVAILABLE:
            self.logger.error("[COPY] websockets not installed")
            return

        self.running = True
        uri = "wss://ws-live-data.polymarket.com"

        self.reload()

        reconnect_delay = 1

        while self.running:
            try:
                async with websockets.connect(uri, ping_interval=5) as ws:
                    reconnect_delay = 1

                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{"topic": "activity", "type": "trades"}]
                    }))

                    self.logger.info("[COPY] Connected to WebSocket")

                    async for msg in ws:
                        if not self.running:
                            break

                        if not msg.strip():
                            continue

                        try:
                            data = json.loads(msg)
                            if data.get("topic") == "activity":
                                await self.process_trade(data.get("payload", {}))
                        except json.JSONDecodeError:
                            pass

            except Exception as e:
                self.logger.error(f"[COPY] WebSocket error: {e}")

            if self.running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    def stop(self):
        """Stop the daemon."""
        self.running = False


# Global instance
_manager: Optional[CopyTradingManager] = None


def get_manager(db_path: str = "app_data.sqlite3") -> CopyTradingManager:
    """Get or create the global manager instance."""
    global _manager
    if _manager is None:
        _manager = CopyTradingManager(db_path=db_path)
        _manager.init_schema()
    return _manager


def stop_daemon():
    """Stop the copy trading daemon."""
    if _manager:
        _manager.stop()
