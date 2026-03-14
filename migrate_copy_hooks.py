#!/usr/bin/env python3
"""
Migrate copy trading hooks from old copy_trading_hooks table to new copy_hooks table.
This script transfers existing hooks while preserving their configuration.
"""

import sqlite3
import json
import sys


def migrate_hooks(db_path: str = "app_data.sqlite3"):
    """Migrate hooks from copy_trading_hooks to copy_hooks."""

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")

    try:
        # Check if old table exists
        old_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='copy_trading_hooks'"
        ).fetchone()

        if not old_table:
            print("No old copy_trading_hooks table found. Nothing to migrate.")
            return 0

        # Check if new table exists
        new_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='copy_hooks'"
        ).fetchone()

        if not new_table:
            print("ERROR: New copy_hooks table not found. Run the app first to create it.")
            return 1

        # Get existing hooks from old table
        old_hooks = conn.execute("""
            SELECT id, follower_user_id, leader_user_id, config, enabled, created_at
            FROM copy_trading_hooks WHERE enabled = 1;
        """).fetchall()

        if not old_hooks:
            print("No hooks to migrate.")
            return 0

        migrated = 0
        for hook in old_hooks:
            hook_id, follower_id, leader_user_id, config_json, enabled, created_at = hook

            # Parse config
            try:
                config = json.loads(config_json or "{}")
            except:
                config = {}

            # Get leader address from config or from user lookup
            leader_address = config.get("leader_address")

            if not leader_address and leader_user_id:
                # Look up user's eth_address
                user = conn.execute(
                    "SELECT eth_address FROM users WHERE user_id = ?",
                    (leader_user_id,)
                ).fetchone()
                if user and user[0]:
                    leader_address = user[0]

            if not leader_address:
                print(f"Skipping hook {hook_id}: No leader address found")
                continue

            leader_address = leader_address.lower().strip()

            # Insert into new table
            conn.execute("""
                INSERT OR REPLACE INTO copy_hooks (
                    follower_user_id, leader_address, config, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?);
            """, (follower_id, leader_address, config_json, enabled, created_at))

            migrated += 1
            print(f"Migrated hook {hook_id}: follower={follower_id} leader={leader_address}")

        conn.commit()
        print(f"\nMigration complete: {migrated} hooks migrated.")
        return 0

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "app_data.sqlite3"
    exit_code = migrate_hooks(db_path)
    sys.exit(exit_code)
