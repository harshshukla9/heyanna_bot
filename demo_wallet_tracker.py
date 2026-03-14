#!/usr/bin/env python3
"""
Demo: Real-time Polymarket wallet tracker.
Tracks a specified wallet address and prints trades as they occur.

Run: python demo_wallet_tracker.py
"""

import sys
import time
from datetime import datetime

import requests


def fetch_trades(wallet_address: str, limit: int = 50) -> list[dict]:
    """Fetch recent trades for a given wallet from Polymarket Data API."""
    url = "https://data-api.polymarket.com/trades"
    params = {
        "user": wallet_address.lower(),
        "takerOnly": "true",
        "limit": limit,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def print_trade(trade: dict):
    """Pretty-print a single trade with market_id, amount, buy rate, etc."""
    ts = trade.get("timestamp", 0)
    dt = datetime.fromtimestamp(ts) if ts else "N/A"

    market_id = trade.get("conditionId") or trade.get("asset", "N/A")
    side = trade.get("side", "?").upper()
    price = float(trade.get("price") or 0)
    size = float(trade.get("size") or 0)
    # Calculate amount from size * price (API doesn't return amount directly)
    amount = size * price if price > 0 and size > 0 else 0.0
    outcome = trade.get("outcome", "?")
    title = trade.get("title", "Unknown")
    tx_hash = trade.get("transactionHash", "")[:12] + "..." if trade.get("transactionHash") else ""

    print(f"\n{'='*60}")
    print(f"NEW TRADE DETECTED!")
    print(f"{'='*60}")
    print(f"  Market ID:   {market_id}")
    print(f"  Time:        {dt}")
    print(f"  Side:        {side}")
    print(f"  Amount:      ${amount:.2f} USD")
    print(f"  Price:       ${price:.4f} /share")
    print(f"  Size:        {size:.4f} shares")
    print(f"  Outcome:     {outcome}")
    print(f"  Market:      {title}")
    print(f"  Tx Hash:     {tx_hash}")
    print(f"{'='*60}")


def track_wallet(wallet_address: str, interval_sec: float = 0.05):
    """
    Continuously track a wallet's trades and print them in real-time.
    Polls every `interval_sec` seconds (default 50ms).
    """
    print(f"\n{'#'*60}")
    print(f"# POLYMARKET WALLET TRACKER")
    print(f"{'#'*60}")
    print(f"# Wallet:  {wallet_address}")
    print(f"# Interval: {interval_sec}s (50ms)")
    print(f"# Press Ctrl+C to stop")
    print(f"{'#'*60}\n")

    last_ts = 0
    seen_hashes = set()
    poll_count = 0

    try:
        while True:
            trades = fetch_trades(wallet_address, limit=50)
            poll_count += 1

            # Status update every 10 seconds (200 polls at 50ms)
            if poll_count % 200 == 0:
                print(f"[{datetime.now()}] Poll #{poll_count}: {len(trades)} trades found")

            # Filter to new trades only
            new_trades = [
                t for t in trades
                if t.get("timestamp", 0) > last_ts
                and t.get("transactionHash") not in seen_hashes
            ]

            if new_trades:
                # Sort by timestamp descending (newest first)
                new_trades.sort(key=lambda t: t.get("timestamp", 0), reverse=True)

                for t in new_trades:
                    print_trade(t)

                    # Update last_ts and seen_hashes
                    tx_hash = t.get("transactionHash")
                    if tx_hash:
                        seen_hashes.add(tx_hash)
                    if t.get("timestamp", 0) > last_ts:
                        last_ts = t["timestamp"]

            time.sleep(interval_sec)

    except KeyboardInterrupt:
        print(f"\n\n[STOPPED] Tracker stopped by user after {poll_count} polls.")


def main():
    # Wallet to track
    wallet_address = "0x63704b64bC05617A489A497CB0E8a2EAa289fe8E"

    # Polling interval: 50ms = 0.05 seconds (real-time)
    interval_sec = 0.05

    track_wallet(wallet_address, interval_sec)


if __name__ == "__main__":
    main()
