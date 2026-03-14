#!/usr/bin/env python3
"""
Real-time Polymarket Wallet Tracker using RTDS WebSocket.
Streams trade events in real-time without polling.

Endpoint: wss://ws-live-data.polymarket.com
Topic: activity (trades)
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

try:
    import websockets
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


def print_trade(trade: dict):
    """Pretty-print a single trade event."""
    ts = trade.get("timestamp", 0)
    dt = datetime.fromtimestamp(ts / 1000) if ts and ts > 1000000000 else datetime.fromtimestamp(ts) if ts else "?"

    asset = trade.get("asset") or trade.get("conditionId") or "N/A"
    outcome = trade.get("outcome") or "?"
    side = trade.get("side", "?").upper()
    price = float(trade.get("price") or 0)
    size = float(trade.get("size") or 0)
    amount = size * price if price > 0 and size > 0 else 0.0
    tx_hash = trade.get("transactionHash", "")[:12] + "..." if trade.get("transactionHash") else ""
    proxy_wallet = trade.get("proxyWallet", "")[:20] + "..." if trade.get("proxyWallet") else ""

    print(f"\n[{'🟢' if side == 'BUY' else '🔴'}] {dt}")
    print(f"  Wallet:     {proxy_wallet}")
    print(f"  Asset:      {asset[:40]}...")
    print(f"  Outcome:    {outcome}")
    print(f"  Side:       {side}")
    print(f"  Amount:     ${amount:.2f} USD")
    print(f"  Price:      ${price:.4f} /share")
    print(f"  Size:       {size:.4f} shares")
    print(f"  Tx Hash:    {tx_hash}")
    print("-" * 50)


async def track_wallet_from_trades(wallet_address: str):
    """
    Track a specific wallet by filtering all trades stream.
    Real-time via RTDS WebSocket - no polling!
    """
    if not DEPS_AVAILABLE:
        print("Install dependencies: uv pip install websockets")
        return

    wallet = wallet_address.lower()

    print("=" * 60)
    print("POLYMARKET WALLET TRACKER (RTDS WebSocket)")
    print("=" * 60)
    print(f"Tracking: {wallet}")
    print("Listening for trades in real-time...")
    print("Press Ctrl+C to stop")
    print("=" * 60)

    uri = "wss://ws-live-data.polymarket.com"
    seen_hashes = set()

    async with websockets.connect(uri, ping_interval=5) as ws:
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
        print("✓ Connected and subscribed to activity/trades\n")

        async for message in ws:
            if not message.strip():
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            if data.get("topic") == "activity":
                trade = data.get("payload", {})
                proxy_wallet = (trade.get("proxyWallet") or "").lower()

                if wallet in proxy_wallet:
                    tx_hash = trade.get("transactionHash") or trade.get("id", "")
                    if tx_hash and tx_hash not in seen_hashes:
                        seen_hashes.add(tx_hash)
                        print_trade(trade)


async def track_wallet_polling(wallet_address: str):
    """Fallback polling-based tracker."""
    import aiohttp

    seen_hashes = set()
    last_ts = 0

    print("=" * 60)
    print("POLYMARKET WALLET TRACKER (Polling)")
    print("=" * 60)
    print(f"Tracking: {wallet_address}")
    print("Polling every 50ms...")
    print("=" * 60)

    url = "https://data-api.polymarket.com/trades"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    url,
                    params={"user": wallet_address.lower(), "takerOnly": "true", "limit": 10},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    trades = await resp.json()

                    for t in reversed(trades):
                        tx_hash = t.get("transactionHash", "")
                        ts = t.get("timestamp", 0)

                        if ts > last_ts and tx_hash not in seen_hashes:
                            seen_hashes.add(tx_hash)
                            print_trade(t)
                            last_ts = ts

            except Exception as e:
                print(f"Polling error: {e}")

            await asyncio.sleep(0.05)


def main():
    load_env_file()

    wallet_address = os.getenv("TRACK_WALLET", "0x63704b64bC05617A489A497CB0E8a2EAa289fe8E")

    # Try RTDS WebSocket first (real-time, no polling)
    try:
        asyncio.run(track_wallet_from_trades(wallet_address))
    except Exception as e:
        print(f"\nWebSocket failed: {e}")
        print("\nFalling back to polling...")
        import aiohttp
        asyncio.run(track_wallet_polling(wallet_address))


if __name__ == "__main__":
    main()
