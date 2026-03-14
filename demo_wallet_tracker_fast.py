#!/usr/bin/env python3
"""
Fast Polymarket wallet tracker with concurrent polling.
Uses async HTTP for faster response times.
"""

import asyncio
from datetime import datetime

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("Install aiohttp for faster tracking: pip install aiohttp")


def print_trade(trade: dict):
    """Pretty-print a single trade."""
    ts = trade.get("timestamp", 0)
    dt = datetime.fromtimestamp(ts) if ts else "?"

    market_id = trade.get("conditionId") or trade.get("asset", "N/A")
    side = trade.get("side", "?").upper()
    price = float(trade.get("price") or 0)
    size = float(trade.get("size") or 0)
    amount = size * price if price > 0 and size > 0 else 0.0
    outcome = trade.get("outcome", "?")
    title = trade.get("title", "Unknown")

    print(f"\n[{'🟢' if side == 'BUY' else '🔴'}] {dt}")
    print(f"  Market:     {market_id[:30]}...")
    print(f"  Side:       {side}")
    print(f"  Amount:     ${amount:.2f} USD")
    print(f"  Price:      ${price:.4f} /share")
    print(f"  Size:       {size:.4f} shares")
    print(f"  Outcome:    {outcome}")
    print(f"  Market:     {title[:50]}")


async def track_wallet_async(wallet_address: str, session: aiohttp.ClientSession):
    """Track wallet using async HTTP for speed."""
    wallet = wallet_address.lower()
    seen_hashes = set()
    last_ts = 0

    print("=" * 60)
    print("POLYMARKET FAST WALLET TRACKER (Async)")
    print("=" * 60)
    print(f"Tracking: {wallet}")
    print("Polling every 50ms with async HTTP...")
    print("=" * 60)

    url = "https://data-api.polymarket.com/trades"

    while True:
        try:
            async with session.get(
                url,
                params={"user": wallet, "takerOnly": "true", "limit": 20},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                trades = await resp.json()

                for t in reversed(trades):  # Newest first
                    tx_hash = t.get("transactionHash", "")
                    ts = t.get("timestamp", 0)

                    if ts > last_ts and tx_hash not in seen_hashes:
                        seen_hashes.add(tx_hash)
                        print_trade(t)
                        last_ts = ts

        except asyncio.TimeoutError:
            print("⚠️  Request timeout")
        except Exception as e:
            print(f"⚠️  Error: {e}")

        await asyncio.sleep(0.05)


def main():
    if not AIOHTTP_AVAILABLE:
        print("Install aiohttp: pip install aiohttp")
        return

    wallet_address = "0x63704b64bC05617A489A497CB0E8a2EAa289fe8E"

    async def run():
        async with aiohttp.ClientSession() as session:
            await track_wallet_async(wallet_address, session)

    asyncio.run(run())


if __name__ == "__main__":
    main()
