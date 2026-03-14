import os
import sqlite3
import threading
import time
import uuid
import base64
import logging
from contextlib import contextmanager
from typing import Any, Callable, Generator, Iterable, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


DEFAULT_DB_PATH = os.getenv("DB_PATH", "app_data.sqlite3")
_SECRET_PREFIX = "enc:v1:"
logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Thread-safe database manager using SQLite with WAL mode enabled.
    This abstracts DB access so both the Telegram bot and the FastAPI
    API can share the same storage layer safely.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._local = threading.local()
        # Used to serialize schema changes / multi-statement write transactions
        self._lock = threading.RLock()
        self._encryption_enabled = self._env_truthy(
            os.getenv("DB_ENCRYPTION_ENABLED", "0")
        )
        self._fernet = self._build_fernet()
        self._encryption_required = self._env_truthy(
            os.getenv("DB_ENCRYPTION_REQUIRED", "0")
        )
        if self._encryption_required and not self._fernet:
            raise RuntimeError(
                "DB_ENCRYPTION_REQUIRED is enabled but no DB encryption key is configured."
            )

    @staticmethod
    def _env_truthy(v: str | None) -> bool:
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}

    def _build_fernet(self) -> Fernet | None:
        """
        Build Fernet instance from environment key material.
        Supported env vars:
          - DB_ENCRYPTION_ENABLED=1 (must be set to enable encryption)
          - DB_ENCRYPTION_KEY (preferred)
          - PRIVATE_KEY_ENCRYPTION_KEY (fallback)

        Accepts either:
          1) a valid Fernet key, or
          2) an arbitrary passphrase (derived via HKDF-SHA256 -> urlsafe base64).
        """
        if not self._encryption_enabled:
            return None
        raw = (
            os.getenv("DB_ENCRYPTION_KEY")
            or os.getenv("PRIVATE_KEY_ENCRYPTION_KEY")
            or ""
        ).strip()
        if not raw:
            return None

        # Try as a direct Fernet key first.
        try:
            return Fernet(raw.encode("utf-8"))
        except Exception:
            pass

        # Derive a Fernet key from passphrase-like input using HKDF-SHA256.
        salt = (
            os.getenv("DB_ENCRYPTION_SALT")
            or "tgbotanna-db-encryption-salt-v1"
        ).encode("utf-8")
        info = b"tgbotanna-db-key-encryption"
        kdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=info,
        )
        key_material = kdf.derive(raw.encode("utf-8"))
        key = base64.urlsafe_b64encode(key_material)
        return Fernet(key)

    def _encrypt_secret(self, value: str | None) -> str:
        if not value:
            return ""
        s = str(value)
        if s.startswith(_SECRET_PREFIX):
            return s
        if not self._encryption_enabled:
            return s
        if not self._fernet:
            if self._encryption_required:
                raise RuntimeError("Encryption required but key is unavailable.")
            return s
        token = self._fernet.encrypt(s.encode("utf-8")).decode("utf-8")
        return _SECRET_PREFIX + token

    def _decrypt_secret(self, value: str | None) -> str:
        if not value:
            return ""
        s = str(value)
        if not s.startswith(_SECRET_PREFIX):
            return s
        if not self._encryption_enabled:
            if self._encryption_required:
                raise RuntimeError(
                    "Encrypted private key found but DB_ENCRYPTION_ENABLED is disabled."
                )
            return s
        token = s[len(_SECRET_PREFIX) :]
        if not self._fernet:
            raise RuntimeError(
                "Encrypted private key found but DB encryption key is missing."
            )
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            raise RuntimeError(
                "Failed to decrypt private key; DB encryption key is invalid for this data."
            )

    def _decode_user_row(self, row: sqlite3.Row | None) -> Optional[dict]:
        if not row:
            return None
        d = dict(row)
        d["eth_private_key"] = self._decrypt_secret(d.get("eth_private_key"))
        d["sol_private_key"] = self._decrypt_secret(d.get("sol_private_key"))
        return d

    def _migrate_encrypt_user_keys(self, conn: sqlite3.Connection) -> None:
        """
        Best-effort one-time migration: encrypt plaintext keys already in DB
        if encryption is enabled.
        """
        if not self._encryption_enabled or not self._fernet:
            return
        rows = conn.execute(
            """
            SELECT user_id, eth_private_key, sol_private_key
            FROM users;
            """
        ).fetchall()
        for r in rows:
            uid = r["user_id"]
            eth = r["eth_private_key"] or ""
            sol = r["sol_private_key"] or ""
            new_eth = self._encrypt_secret(eth)
            new_sol = self._encrypt_secret(sol)
            if new_eth != eth or new_sol != sol:
                conn.execute(
                    """
                    UPDATE users
                    SET eth_private_key = ?, sol_private_key = ?
                    WHERE user_id = ?;
                    """,
                    (new_eth, new_sol, uid),
                )

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        return self._local.conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Transaction context manager for atomic multi-statement operations.
        """
        conn = self.get_connection()
        with self._lock:
            try:
                conn.execute("BEGIN;")
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """
        Execute a single SQL statement with optional parameters.
        """
        conn = self.get_connection()
        return conn.execute(sql, tuple(params))

    def init_schema(self) -> None:
        """
        Initialize the core schema. Call once at application startup.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id         INTEGER PRIMARY KEY,
                    username        TEXT,
                    eth_address     TEXT,
                    eth_private_key TEXT,
                    sol_address     TEXT,
                    sol_private_key TEXT
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id          TEXT PRIMARY KEY,
                    user_id     INTEGER NOT NULL,
                    created_at  INTEGER NOT NULL,
                    revoked_at  INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                """
            )

            # Optional column to track whether Polymarket approvals have been done.
            # If the column already exists, this will raise an OperationalError we can ignore.
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN polymarket_approved INTEGER DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                # Column already exists or table is in a state where this is not needed.
                pass

            # Optional flag indicating whether user has enabled copy trading.
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN copy_trading_enabled INTEGER DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                pass

            # Safe / proxy wallet address for gasless Polymarket trading.
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN safe_address TEXT;"
                )
            except sqlite3.OperationalError:
                pass

            # Global trade feed table for social/copy trading.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id             INTEGER NOT NULL,
                    market_id           INTEGER NOT NULL,
                    side                TEXT NOT NULL,
                    amount              REAL NOT NULL,
                    status              TEXT NOT NULL,
                    order_id            TEXT,
                    tx_hash             TEXT,
                    executed_at         INTEGER NOT NULL,
                    copied_from_user_id INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                """
            )

            # Optional column for order_id if upgrading an existing DB.
            try:
                conn.execute(
                    "ALTER TABLE trades ADD COLUMN order_id TEXT;"
                )
            except sqlite3.OperationalError:
                pass

            # Polymarket unique key for the market (we do not assign market ids; this is the canonical id).
            try:
                conn.execute(
                    "ALTER TABLE trades ADD COLUMN condition_id TEXT;"
                )
            except sqlite3.OperationalError:
                pass

            # Size (shares) and price (per share) for trade display.
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN size REAL;")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN price REAL;")
            except sqlite3.OperationalError:
                pass

            # order_side: BUY (open) | SELL (close)
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN order_side TEXT;")
            except sqlite3.OperationalError:
                pass

            # Follows relationship for copy trading (follower → leader).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_trading_follows (
                    follower_user_id INTEGER NOT NULL,
                    leader_user_id   INTEGER NOT NULL,
                    created_at       INTEGER NOT NULL,
                    PRIMARY KEY (follower_user_id, leader_user_id),
                    FOREIGN KEY (follower_user_id) REFERENCES users(user_id),
                    FOREIGN KEY (leader_user_id) REFERENCES users(user_id)
                );
                """
            )

            # Hooks: following spawns a hook; enabling runs all hooks.
            # Note: For global hooks (external wallets), leader_user_id is NULL
            # and the actual leader address is stored in config.leader_address.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_trading_hooks (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    follower_user_id  INTEGER NOT NULL,
                    leader_user_id    INTEGER,
                    created_at        INTEGER NOT NULL,
                    config            TEXT,
                    enabled           INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (follower_user_id) REFERENCES users(user_id),
                    FOREIGN KEY (leader_user_id) REFERENCES users(user_id)
                );
                """
            )
            try:
                conn.execute("ALTER TABLE copy_trading_hooks ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
            # Hook execution log table
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hook_logs (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        hook_id           INTEGER NOT NULL,
                        follower_user_id  INTEGER NOT NULL,
                        leader_address    TEXT,
                        trade_side        TEXT,
                        trade_outcome     TEXT,
                        trade_amount      REAL,
                        trade_price       REAL,
                        trade_size        REAL,
                        follower_amount   REAL,
                        condition_id      TEXT,
                        market_title      TEXT,
                        status            TEXT NOT NULL,
                        error_message     TEXT,
                        executed_at       INTEGER NOT NULL,
                        FOREIGN KEY (hook_id) REFERENCES copy_trading_hooks(id),
                        FOREIGN KEY (follower_user_id) REFERENCES users(user_id)
                    );
                    """
                )
            except sqlite3.OperationalError:
                pass

            # Cached Polymarket public profile metadata for arbitrary wallets
            # (including global leaders not registered in our app).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS polymarket_profiles (
                    address    TEXT PRIMARY KEY,
                    username   TEXT,
                    fetched_at INTEGER NOT NULL
                );
                """
            )

            # Migrate existing follows into hooks (idempotent).
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO copy_trading_hooks (follower_user_id, leader_user_id, created_at, config, enabled)
                    SELECT follower_user_id, leader_user_id, created_at, '{}', 1
                    FROM copy_trading_follows
                    WHERE NOT EXISTS (
                        SELECT 1 FROM copy_trading_hooks h
                        WHERE h.follower_user_id = copy_trading_follows.follower_user_id
                          AND h.leader_user_id = copy_trading_follows.leader_user_id
                    );
                    """
                )
            except sqlite3.OperationalError:
                pass

            # Migrate existing follows into hooks (idempotent - legacy).
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO copy_trading_hooks (follower_user_id, leader_user_id, created_at, config, enabled)
                    SELECT follower_user_id, leader_user_id, created_at, '{}', 1
                    FROM copy_trading_follows
                    WHERE NOT EXISTS (
                        SELECT 1 FROM copy_trading_hooks h
                        WHERE h.follower_user_id = copy_trading_follows.follower_user_id
                          AND h.leader_user_id = copy_trading_follows.leader_user_id
                    );
                    """
                )
            except sqlite3.OperationalError:
                pass

            # Encrypt legacy plaintext keys when encryption is configured.
            self._migrate_encrypt_user_keys(conn)

    def get_user(self, user_id: int) -> Optional[dict]:
        row = self.execute(
            "SELECT * FROM users WHERE user_id = ?;",
            (user_id,),
        ).fetchone()
        return self._decode_user_row(row)

    def get_user_by_address(self, eth_address: str) -> Optional[dict]:
        row = self.execute(
            "SELECT * FROM users WHERE eth_address = ?;",
            (eth_address,),
        ).fetchone()
        return self._decode_user_row(row)

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """
        Fetch a user by their username.
        """
        row = self.execute(
            "SELECT * FROM users WHERE username = ?;",
            (username,),
        ).fetchone()
        return self._decode_user_row(row)

    def create_user(self, user_id: int, username: str, eth_data: dict, sol_data: Any) -> None:
        if isinstance(sol_data, dict):
            sol_address = sol_data.get("address", "")
            sol_private_key = sol_data.get("private_key", "")
        else:
            sol_address = sol_data[0] if sol_data else ""
            sol_private_key = sol_data[1] if len(sol_data) > 1 else ""

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username,
                    eth_address, eth_private_key,
                    sol_address, sol_private_key
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    user_id,
                    username,
                    eth_data["address"],
                    self._encrypt_secret(eth_data["private_key"]),
                    sol_address,
                    self._encrypt_secret(sol_private_key),
                ),
            )

    def update_safe_address(self, eth_address: str, safe_address: str) -> None:
        """Persist the Safe / proxy wallet address for a user."""
        self.execute(
            "UPDATE users SET safe_address = ? WHERE eth_address = ?;",
            (safe_address, eth_address),
        )

    def create_session(self, user_id: int) -> str:
        """
        Create a new login session for the given user and return its ID.
        """
        session_id = uuid.uuid4().hex
        now = int(time.time())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, revoked_at) VALUES (?, ?, ?, NULL);",
                (session_id, user_id, now),
            )
        return session_id

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self.execute(
            "SELECT * FROM sessions WHERE id = ?;",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def revoke_session(self, session_id: str) -> None:
        """
        Mark a session as revoked (logout).
        """
        now = int(time.time())
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ?;",
                (now, session_id),
            )

    def record_trade(
        self,
        user_id: int,
        market_id: int,
        side: str,
        amount: float,
        status: str,
        order_id: str | None,
        tx_hash: str | None,
        executed_at: int,
        copied_from_user_id: int | None = None,
        condition_id: str | None = None,
        size: float | None = None,
        price: float | None = None,
        order_side: str | None = None,
    ) -> None:
        """
        Persist a single trade. amount = cost in USD. size = shares, price = per share.
        Only call with status=matched and valid tx_hash.
        """
        if not tx_hash or not str(tx_hash).strip() or str(tx_hash).strip().lower() == "pending":
            return
        tx_hash = str(tx_hash).strip()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM trades WHERE TRIM(tx_hash) = ? LIMIT 1;",
                (tx_hash,),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """
                INSERT INTO trades (
                    user_id, market_id, side, amount,
                    status, order_id, tx_hash, executed_at, copied_from_user_id, condition_id,
                    size, price, order_side
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    user_id,
                    market_id,
                    side,
                    amount,
                    status,
                    order_id or "",
                    tx_hash,
                    executed_at,
                    copied_from_user_id,
                    condition_id or "",
                    size,
                    price,
                    (order_side or "").strip().upper() or None,
                ),
            )

    def create_copy_hook(
        self, follower_user_id: int, leader_user_id: int, config: dict | None = None
    ) -> int:
        """Create a copy-trading hook. Following spawns a hook. Returns hook id."""
        import json

        now = int(time.time())
        config_json = json.dumps(config or {})
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO copy_trading_hooks (
                    follower_user_id,
                    leader_user_id,
                    created_at,
                    config,
                    enabled
                ) VALUES (?, ?, ?, ?, 1);
                """,
                (follower_user_id, leader_user_id, now, config_json),
            )
            row = conn.execute(
                "SELECT id FROM copy_trading_hooks WHERE follower_user_id = ? AND leader_user_id = ?;",
                (follower_user_id, leader_user_id),
            ).fetchone()
            return int(row["id"]) if row else 0

    def update_copy_hook_config(self, hook_id: int, config: dict) -> None:
        """Persist updated config JSON for a given hook id."""
        import json

        cfg_json = json.dumps(config or {})
        with self.transaction() as conn:
            conn.execute(
                "UPDATE copy_trading_hooks SET config = ? WHERE id = ?;",
                (cfg_json, hook_id),
            )

    def delete_copy_hook(self, follower_user_id: int, leader_user_id: int) -> bool:
        """Remove a copy-trading hook (unfollow)."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                DELETE FROM copy_trading_hooks
                WHERE follower_user_id = ? AND leader_user_id = ?;
                """,
                (follower_user_id, leader_user_id),
            )
            return cur.rowcount > 0

    def add_global_copy_hook(
        self, follower_user_id: int, leader_address: str, config: dict | None = None
    ) -> int:
        """
        Add a global copy-trading hook for tracking an external wallet address.
        Uses the new copy_hooks table. Returns hook id.
        """
        import json

        leader_address = leader_address.lower().strip()
        config_json = json.dumps(config or {})

        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO copy_hooks (
                    follower_user_id,
                    leader_address,
                    config,
                    enabled,
                    created_at
                ) VALUES (?, ?, ?, 1, strftime('%s', 'now'));
                """,
                (follower_user_id, leader_address, config_json),
            )
            row = conn.execute(
                "SELECT id FROM copy_hooks WHERE follower_user_id = ? AND leader_address = ?;",
                (follower_user_id, leader_address),
            ).fetchone()
            return int(row["id"]) if row else 0

    def remove_global_copy_hook(self, follower_user_id: int, leader_address: str) -> bool:
        """Remove a global copy-trading hook by wallet address."""
        leader_address = leader_address.lower().strip()

        with self.transaction() as conn:
            cur = conn.execute(
                """
                DELETE FROM copy_hooks
                WHERE follower_user_id = ? AND leader_address = ?;
                """,
                (follower_user_id, leader_address),
            )
            return cur.rowcount > 0

    def get_global_copy_hooks(self, follower_user_id: int | None = None) -> list[dict]:
        """Get all global copy hooks, optionally filtered by follower."""
        import json

        if follower_user_id is not None:
            rows = self.execute(
                """
                SELECT id, follower_user_id, leader_address, config, enabled, created_at
                FROM copy_hooks
                WHERE follower_user_id = ? AND enabled = 1;
                """,
                (follower_user_id,),
            ).fetchall()
        else:
            rows = self.execute(
                """
                SELECT id, follower_user_id, leader_address, config, enabled, created_at
                FROM copy_hooks
                WHERE enabled = 1;
                """,
            ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            try:
                d["config"] = json.loads(d["config"] or "{}")
            except Exception:
                d["config"] = {}
            out.append(d)
        return out

    def get_polymarket_profile(self, address: str) -> Optional[dict]:
        """Return cached Polymarket profile row for an address."""
        addr = (address or "").strip().lower()
        if not addr:
            return None
        row = self.execute(
            "SELECT address, username, fetched_at FROM polymarket_profiles WHERE address = ?;",
            (addr,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_polymarket_profile(self, address: str, username: str | None) -> None:
        """Insert/update cached Polymarket profile data for an address."""
        addr = (address or "").strip().lower()
        if not addr:
            return
        uname = (username or "").strip() or None
        now = int(time.time())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO polymarket_profiles (address, username, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    username = excluded.username,
                    fetched_at = excluded.fetched_at;
                """,
                (addr, uname, now),
            )

    def refetch_polymarket_username(
        self, address: str, fetcher: Callable[[str], str | None]
    ) -> str | None:
        """
        Force-refresh username for an address via fetcher and persist to cache.
        Returns the fetched username (or None).
        """
        addr = (address or "").strip().lower()
        if not addr:
            return None
        username = fetcher(addr)
        self.upsert_polymarket_profile(addr, username)
        return username

    
    def delete_trades_without_valid_tx_hash(self) -> int:
        """
        Delete all trades that do not have a valid on-chain tx_hash.
        Use this to flush the feed to only trades with real settlement hashes.
        Returns the number of rows deleted.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """
                DELETE FROM trades
                WHERE tx_hash IS NULL
                   OR TRIM(COALESCE(tx_hash, '')) = ''
                   OR LOWER(TRIM(tx_hash)) = 'pending';
                """
            )
            return cur.rowcount


