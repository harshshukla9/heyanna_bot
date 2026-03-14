#!/usr/bin/env python3
"""
Backfill trades table:
1. Populate size (shares) and price (per share) for rows with amount but null size/price.
2. Populate order_side (BUY=open, SELL=close) for rows with null order_side.

Note: order_side backfill defaults to BUY for historical trades (we cannot infer open vs close).
"""
import os
import sys

# Add project root so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database_manager import DatabaseManager
import market_cache


def main() -> None:
    db_path = os.getenv("DB_PATH", "app_data.sqlite3")
    db = DatabaseManager(db_path=db_path)
    db.init_schema()

    rows = db.execute(
        """
        SELECT id, condition_id, side, amount
        FROM trades
        WHERE (size IS NULL OR price IS NULL)
          AND condition_id IS NOT NULL
          AND TRIM(COALESCE(condition_id, '')) != ''
          AND amount > 0
        ORDER BY id;
        """
    ).fetchall()

    if not rows:
        print("No trades need backfill.")
        return

    updated = 0
    failed = 0

    for r in rows:
        tid = r["id"]
        cid = (r["condition_id"] or "").strip()
        side = (r["side"] or "Yes").strip()
        amount = float(r["amount"] or 0)

        m = market_cache.ensure_market_cached(cid)
        if not m:
            print(f"  Skip trade {tid}: market not found for {cid[:20]}...")
            failed += 1
            continue

        odds_cents = m.odds.get(side, 0)
        if not odds_cents or odds_cents <= 0:
            print(f"  Skip trade {tid}: no price for side {side}")
            failed += 1
            continue

        price = float(odds_cents) / 100.0
        size = amount / price if price > 0 else None
        if size is None or size <= 0:
            failed += 1
            continue

        with db.transaction() as conn:
            conn.execute(
                "UPDATE trades SET size = ?, price = ? WHERE id = ?;",
                (size, price, tid),
            )
        updated += 1
        print(f"  OK trade {tid}: amount={amount:.4f} price={price:.2f} size={size:.4f}")

    print(f"\nBackfill size/price done: {updated} updated, {failed} skipped/failed.")

    # 2. Backfill order_side (default BUY for historical trades we cannot infer)
    rows_os = db.execute(
        "SELECT id FROM trades WHERE order_side IS NULL OR TRIM(COALESCE(order_side, '')) = '';"
    ).fetchall()

    os_updated = 0
    for r in rows_os:
        with db.transaction() as conn:
            conn.execute("UPDATE trades SET order_side = 'BUY' WHERE id = ?;", (r["id"],))
        os_updated += 1

    if os_updated:
        print(f"Backfill order_side done: {os_updated} set to BUY (open).")


if __name__ == "__main__":
    main()
