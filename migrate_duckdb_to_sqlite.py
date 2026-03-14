"""
One-time migration script: copy user data from the legacy DuckDB file
(`bot_data.duckdb`, used by `database.py`) into the new SQLite database
managed by `DatabaseManager` (`app_data.sqlite3` by default).

Usage:
    python migrate_duckdb_to_sqlite.py
    python migrate_duckdb_to_sqlite.py --force   # overwrite existing users

Safe to run multiple times. By default, existing users in SQLite are left
unchanged. With --force, existing users are overwritten from DuckDB.
"""

import duckdb
import sys

from database_manager import DatabaseManager


def migrate(force: bool = False) -> None:
    # Connect to legacy DuckDB
    try:
        conn = duckdb.connect("bot_data.duckdb", read_only=True)
    except Exception as e:
        print(f"Could not open bot_data.duckdb: {e}")
        return

    try:
        rows = conn.execute(
            "SELECT user_id, username, eth_address, eth_private_key, sol_address, sol_private_key FROM users"
        ).fetchall()
    except Exception as e:
        print(f"No users table or failed to read from DuckDB: {e}")
        conn.close()
        return

    conn.close()

    if not rows:
        print("No users found in DuckDB; nothing to migrate.")
        return

    db = DatabaseManager()
    db.init_schema()

    migrated = 0
    skipped = 0
    overwritten = 0
    for (
        user_id,
        username,
        eth_address,
        eth_private_key,
        sol_address,
        sol_private_key,
    ) in rows:
        existing = db.get_user(user_id)
        eth_data = {"address": eth_address, "private_key": eth_private_key}
        sol_data = (sol_address or "", sol_private_key or "")

        if existing and not force:
            skipped += 1
            continue

        if existing and force:
            # Overwrite existing record
            with db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, eth_address = ?, eth_private_key = ?,
                        sol_address = ?, sol_private_key = ?
                    WHERE user_id = ?;
                    """,
                    (
                        username or "",
                        eth_address,
                        eth_private_key,
                        sol_address or "",
                        sol_private_key or "",
                        int(user_id),
                    ),
                )
            overwritten += 1
        else:
            # Insert new user
            db.create_user(
                user_id=int(user_id),
                username=username or "",
                eth_data=eth_data,
                sol_data=sol_data,
            )
            migrated += 1

    print(
        f"Migrated {migrated} new users to SQLite, "
        f"overwrote {overwritten} existing users, "
        f"skipped {skipped} users."
    )


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    migrate(force=force_flag)

