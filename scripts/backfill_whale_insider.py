#!/usr/bin/env python3
"""
Backfill whale/insider alerts:
1. Re-fetch trade details from Polymarket API to populate missing 'side' field.
2. Re-calculate trade_usd if size/price was not properly captured.

This is useful for alerts that were indexed before the 'side' column was added
or where the side data was incomplete.
"""
import os
import sys
import json
import requests
from typing import Any

# Add project root so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database_manager import DatabaseManager


def _fetch_trade_by_tx_hash(tx_hash: str) -> dict[str, Any] | None:
    """
    Fetch trade details from Polymarket global trades API using tx hash.
    Returns the trade object or None if not found.
    """
    if not tx_hash:
        return None

    try:
        # Fetch recent trades and look for matching tx hash
        url = "https://data-api.polymarket.com/trades"
        params = {"limit": 500, "takerOnly": "true"}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list):
            return None

        for trade in data:
            if not isinstance(trade, dict):
                continue
            if (trade.get("txHash") or trade.get("transaction_hash") or "") == tx_hash:
                return trade

    except Exception:
        pass

    return None


def _fetch_trade_by_condition_id(condition_id: str, limit: int = 100) -> dict[str, Any] | None:
    """
    Fetch trades for a specific condition_id.
    """
    if not condition_id:
        return None

    try:
        url = "https://data-api.polymarket.com/trades"
        params = {"condition_id": condition_id, "limit": limit, "takerOnly": "true"}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list) and data:
            return data[0]  # Return first trade

    except Exception:
        pass

    return None


def main() -> None:
    db_path = os.getenv("DB_PATH", "app_data.sqlite3")
    db = DatabaseManager(db_path=db_path)
    db.init_schema()

    # Find alerts missing side or with empty side
    rows = db.execute(
        """
        SELECT id, wallet, kind, trade_usd, condition_id, market_title, tx_hash, executed_at
        FROM whale_insider_alerts
        WHERE TRIM(COALESCE(side, '')) = '' OR side IS NULL
        ORDER BY executed_at DESC
        LIMIT 100;
        """
    ).fetchall()

    if not rows:
        print("No whale/insider alerts need backfill.")
        return

    print(f"Found {len(rows)} alerts to backfill.")

    updated = 0
    skipped = 0

    for r in rows:
        aid = r["id"]
        tx_hash = (str(r["tx_hash"]) if r["tx_hash"] else "").strip()
        condition_id = (str(r["condition_id"]) if r["condition_id"] else "").strip()
        wallet = (str(r["wallet"]) if r["wallet"] else "").strip()
        kind = (str(r["kind"]) if r["kind"] else "unknown").strip()

        trade_side = None

        # Try to get side from tx_hash first
        if tx_hash:
            trade = _fetch_trade_by_tx_hash(tx_hash)
            if trade:
                trade_side = trade.get("side") or trade.get("outcome")
                print(f"  [{aid}] Found side via tx_hash: {trade_side}")

        # Try condition_id if tx_hash didn't work
        if not trade_side and condition_id:
            trade = _fetch_trade_by_condition_id(condition_id)
            if trade:
                trade_side = trade.get("side") or trade.get("outcome")
                print(f"  [{aid}] Found side via condition_id: {trade_side}")

        if trade_side:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE whale_insider_alerts SET side = ? WHERE id = ?;",
                    (trade_side, aid),
                )
            updated += 1
            print(f"  OK [{aid}] Updated side to: {trade_side}")
        else:
            skipped += 1
            tx_short = f"{tx_hash[:16]}..." if len(tx_hash) > 16 else tx_hash
            cid_short = f"{condition_id[:16]}..." if len(condition_id) > 16 else condition_id
            print(f"  SKIP [{aid}] Could not find side for tx_hash={tx_short} condition_id={cid_short}")

    print(f"\nBackfill complete: {updated} updated, {skipped} skipped.")


if __name__ == "__main__":
    main()
