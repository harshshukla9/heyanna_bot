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
                    sol_private_key TEXT,
                    invite_code     TEXT,
                    onboarded       INTEGER DEFAULT 0
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

            # Invite codes table for invite-based authentication
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invite_codes (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    code            TEXT UNIQUE NOT NULL,
                    created_by      INTEGER,
                    created_at      INTEGER NOT NULL,
                    expires_at      INTEGER,
                    max_uses        INTEGER,
                    current_uses    INTEGER DEFAULT 0,
                    claimed_by      INTEGER,
                    is_active       INTEGER DEFAULT 1,
                    FOREIGN KEY (created_by) REFERENCES users(user_id),
                    FOREIGN KEY (claimed_by) REFERENCES users(user_id)
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

            # Add invite_code column for invite-based auth
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN invite_code TEXT;"
                )
            except sqlite3.OperationalError:
                pass

            # Add onboarded flag for invite-based auth
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                pass

            # Migrate invite_codes.created_by to allow NULL (for admin-generated codes)
            # SQLite requires recreating the table to change column constraints
            try:
                # Check if invite_codes table exists
                conn.execute("SELECT 1 FROM invite_codes LIMIT 1;")
                # Table exists - recreate it with nullable created_by
                conn.execute("DROP TABLE IF EXISTS invite_codes_new;")
                conn.execute("""
                    CREATE TABLE invite_codes_new (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        code            TEXT UNIQUE NOT NULL,
                        created_by      INTEGER,
                        created_at      INTEGER NOT NULL,
                        expires_at      INTEGER,
                        max_uses        INTEGER,
                        current_uses    INTEGER DEFAULT 0,
                        claimed_by      INTEGER,
                        is_active       INTEGER DEFAULT 1
                    );
                """)
                conn.execute("INSERT INTO invite_codes_new SELECT * FROM invite_codes;")
                conn.execute("DROP TABLE invite_codes;")
                conn.execute("ALTER TABLE invite_codes_new RENAME TO invite_codes;")
            except sqlite3.OperationalError:
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

            # ── Signal trading (auto-trade from series signals) ──
            # Opt-in flag + per-user amount. Default OFF.
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN signal_trading_enabled INTEGER DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN signal_trade_amount_usd REAL DEFAULT 0;"
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

            # One row per user per signal auto-trade.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_autotrade_jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    series_slug TEXT,
                    series      TEXT,
                    event_slug  TEXT,
                    condition_id TEXT NOT NULL,
                    timeframe   TEXT,
                    signal      TEXT,
                    signal_ts   INTEGER NOT NULL,
                    end_ts      INTEGER NOT NULL,
                    status      TEXT NOT NULL,
                    order_id    TEXT,
                    tx_hash     TEXT,
                    error       TEXT,
                    created_at  INTEGER NOT NULL,
                    updated_at  INTEGER NOT NULL,
                    UNIQUE(user_id, condition_id, signal_ts),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                """
            )
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_signal_autotrade_jobs_due ON signal_autotrade_jobs(end_ts, status);"
                )
            except sqlite3.OperationalError:
                pass

            # Outbox for Telegram notifications (bot delivers and marks sent).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_notifications_outbox (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    kind        TEXT NOT NULL,
                    text        TEXT NOT NULL,
                    created_at  INTEGER NOT NULL,
                    sent_at     INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                """
            )
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_signal_notifications_outbox_unsent ON signal_notifications_outbox(sent_at, created_at);"
                )
            except sqlite3.OperationalError:
                pass

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
            # Global copy hooks: follow by wallet address (leader_address), not leader_user_id.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_hooks (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    follower_user_id  INTEGER NOT NULL,
                    leader_address    TEXT NOT NULL,
                    config            TEXT,
                    enabled           INTEGER NOT NULL DEFAULT 1,
                    last_seen_ts      INTEGER NOT NULL DEFAULT 0,
                    created_at        INTEGER NOT NULL,
                    UNIQUE (follower_user_id, leader_address),
                    FOREIGN KEY (follower_user_id) REFERENCES users(user_id)
                );
                """
            )
            try:
                conn.execute("ALTER TABLE copy_hooks ADD COLUMN last_seen_ts INTEGER NOT NULL DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
            # Hook execution log table (hook_id may be from copy_trading_hooks or copy_hooks — no FK on hook_id).
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
                        FOREIGN KEY (follower_user_id) REFERENCES users(user_id)
                    );
                    """
                )
            except sqlite3.OperationalError:
                pass

            # copy_logs: execution log for global copy hooks (used by copy_trading.py and /me/copy-trading/notifications).
            # Same schema as copy_trading module; no FK on hook_id so copy_hooks ids are valid.
            conn.execute(
                """
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
                    executed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                );
                """
            )

            # Migration: recreate hook_logs without hook_id FK (so copy_hooks ids can be logged).
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_logs'").fetchone():
                    conn.execute(
                        """
                        CREATE TABLE hook_logs_new (
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
                            FOREIGN KEY (follower_user_id) REFERENCES users(user_id)
                        );
                        """
                    )
                    conn.execute(
                        "INSERT INTO hook_logs_new SELECT id, hook_id, follower_user_id, leader_address, trade_side, trade_outcome, trade_amount, trade_price, trade_size, follower_amount, condition_id, market_title, status, error_message, executed_at FROM hook_logs;"
                    )
                    conn.execute("DROP TABLE hook_logs;")
                    conn.execute("ALTER TABLE hook_logs_new RENAME TO hook_logs;")
            except sqlite3.OperationalError:
                pass

            # Whale / insider detection daemon: alerts and last processed timestamp.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_insider_alerts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet       TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    trade_usd    REAL NOT NULL,
                    condition_id TEXT,
                    market_title TEXT,
                    tx_hash      TEXT,
                    executed_at  INTEGER NOT NULL,
                    created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_insider_seen_wallets (
                    wallet       TEXT PRIMARY KEY,
                    first_seen_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_insider_daemon_state (
                    key   TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS whale_insider_alerts_tx_kind ON whale_insider_alerts(tx_hash, kind) WHERE tx_hash != '' AND tx_hash IS NOT NULL;"
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
        # Auto-create Polymarket profile with Telegram username so leader/follower displays show it.
        eth_addr = (eth_data.get("address") or "").strip()
        if eth_addr:
            self.upsert_polymarket_profile(eth_addr, username)

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

    # ── Whale / insider detection daemon ─────────────────────────────────────

    def get_last_whale_daemon_ts(self) -> int:
        """Return last processed trade timestamp for whale/insider daemon."""
        row = self.execute(
            "SELECT value FROM whale_insider_daemon_state WHERE key = ?;",
            ("last_ts",),
        ).fetchone()
        return int(row["value"]) if row else 0

    def set_last_whale_daemon_ts(self, ts: int) -> None:
        """Set last processed trade timestamp for whale/insider daemon."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO whale_insider_daemon_state (key, value) VALUES (?, ?);",
                ("last_ts", ts),
            )

    def has_seen_whale_wallet(self, wallet: str) -> bool:
        """Return True if we have already seen this wallet (for insider = new-account check)."""
        wallet = (wallet or "").strip().lower()
        if not wallet:
            return True
        row = self.execute(
            "SELECT 1 FROM whale_insider_seen_wallets WHERE wallet = ? LIMIT 1;",
            (wallet,),
        ).fetchone()
        return row is not None

    def ensure_seen_whale_wallet(self, wallet: str, first_seen_at: int) -> None:
        """Record that we have seen this wallet (so future large trades are whale, not insider)."""
        wallet = (wallet or "").strip().lower()
        if not wallet:
            return
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO whale_insider_seen_wallets (wallet, first_seen_at) VALUES (?, ?);",
                (wallet, first_seen_at),
            )

    def insert_whale_insider_alert(
        self,
        wallet: str,
        kind: str,
        trade_usd: float,
        executed_at: int,
        condition_id: str | None = None,
        market_title: str | None = None,
        tx_hash: str | None = None,
    ) -> bool:
        """Insert a whale or insider alert (indexed flag). kind must be 'whale' or 'insider'. Returns True if inserted."""
        wallet = (wallet or "").strip()
        if not wallet or kind not in ("whale", "insider"):
            return False
        tx_hash = (tx_hash or "").strip() or ""
        with self.transaction() as conn:
            if tx_hash:
                existing = conn.execute(
                    "SELECT 1 FROM whale_insider_alerts WHERE tx_hash = ? AND kind = ? LIMIT 1;",
                    (tx_hash, kind),
                ).fetchone()
                if existing:
                    return False
            conn.execute(
                """
                INSERT INTO whale_insider_alerts (wallet, kind, trade_usd, condition_id, market_title, tx_hash, executed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'));
                """,
                (wallet, kind, trade_usd, condition_id or "", (market_title or "")[:255], tx_hash, executed_at),
            )
            return True

    def get_recent_whale_insider_alerts(self, limit: int = 50) -> list[dict]:
        """Return recent whale/insider alerts for notifications, newest first."""
        rows = self.execute(
            """
            SELECT wallet, kind, trade_usd, condition_id, market_title, tx_hash, executed_at, created_at
            FROM whale_insider_alerts
            ORDER BY executed_at DESC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_whale_insider_index(
        self,
        limit: int = 100,
        offset: int = 0,
        kind: str | None = None,
    ) -> list[dict]:
        """Return indexed whale/insider flags (paginated). kind: None = all, 'whale' or 'insider'."""
        if kind and kind not in ("whale", "insider"):
            kind = None
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        if offset < 0:
            offset = 0
        if kind:
            rows = self.execute(
                """
                SELECT id, wallet, kind, trade_usd, condition_id, market_title, tx_hash, executed_at, created_at
                FROM whale_insider_alerts
                WHERE kind = ?
                ORDER BY executed_at DESC
                LIMIT ? OFFSET ?;
                """,
                (kind, limit, offset),
            ).fetchall()
        else:
            rows = self.execute(
                """
                SELECT id, wallet, kind, trade_usd, condition_id, market_title, tx_hash, executed_at, created_at
                FROM whale_insider_alerts
                ORDER BY executed_at DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_whale_insider_stats(self) -> dict:
        """Return counts for the whale/insider index."""
        whale = self.execute("SELECT COUNT(*) AS n FROM whale_insider_alerts WHERE kind = 'whale';").fetchone()
        insider = self.execute("SELECT COUNT(*) AS n FROM whale_insider_alerts WHERE kind = 'insider';").fetchone()
        last_ts = self.execute("SELECT value FROM whale_insider_daemon_state WHERE key = 'last_ts';").fetchone()
        return {
            "whale_count": int(whale["n"]) if whale else 0,
            "insider_count": int(insider["n"]) if insider else 0,
            "last_indexed_ts": int(last_ts["value"]) if last_ts else 0,
        }

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

    def get_hooks_for_leader(self, leader_user_id: int) -> list[dict]:
        """Get all copy-trading hooks that follow this leader (enabled only)."""
        import json

        rows = self.execute(
            """
            SELECT id, follower_user_id, leader_user_id, config, enabled, created_at
            FROM copy_trading_hooks
            WHERE leader_user_id = ? AND enabled = 1;
            """,
            (leader_user_id,),
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
            # Get hook id(s) for this follower+leader so we can clear dependent rows first.
            rows = conn.execute(
                "SELECT id FROM copy_hooks WHERE follower_user_id = ? AND leader_address = ?;",
                (follower_user_id, leader_address),
            ).fetchall()
            hook_ids = [r["id"] for r in rows] if rows else []
            if not hook_ids:
                return False
            # Remove dependent rows (copy_logs/hook_logs may have FK to copy_hooks).
            for hook_id in hook_ids:
                try:
                    conn.execute("DELETE FROM copy_logs WHERE hook_id = ?;", (hook_id,))
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("DELETE FROM hook_logs WHERE hook_id = ?;", (hook_id,))
                except sqlite3.OperationalError:
                    pass
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

    # ── Invite Code Methods ──────────────────────────────────────────────

    def create_invite_code(
        self,
        created_by: int,
        max_uses: Optional[int] = None,
        expires_at: Optional[int] = None,
    ) -> str:
        """
        Create a new invite code and return the code string.
        """
        import uuid
        code = uuid.uuid4().hex[:12].upper()
        now = int(time.time())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO invite_codes (code, created_by, created_at, expires_at, max_uses, current_uses, is_active)
                VALUES (?, ?, ?, ?, ?, 0, 1);
                """,
                (code, created_by, now, expires_at, max_uses),
            )
        return code

    def bulk_create_invite_codes(
        self,
        created_by: int,
        count: int,
        max_uses_per_code: Optional[int] = None,
        expires_at: Optional[int] = None,
    ) -> list[str]:
        """
        Create multiple invite codes and return the list of code strings.
        """
        import uuid
        codes = []
        now = int(time.time())
        with self.transaction() as conn:
            for _ in range(count):
                code = uuid.uuid4().hex[:12].upper()
                conn.execute(
                    """
                    INSERT INTO invite_codes (code, created_by, created_at, expires_at, max_uses, current_uses, is_active)
                    VALUES (?, ?, ?, ?, ?, 0, 1);
                    """,
                    (code, created_by, now, expires_at, max_uses_per_code),
                )
                codes.append(code)
        return codes

    def get_invite_code(self, code: str) -> Optional[dict]:
        """
        Get invite code by code string. Returns None if not found or inactive.
        """
        row = self.execute(
            """
            SELECT * FROM invite_codes
            WHERE code = ? AND is_active = 1;
            """,
            (code.upper(),),
        ).fetchone()
        return dict(row) if row else None

    def validate_invite_code(self, code: str) -> tuple[bool, str]:
        """
        Validate an invite code. Returns (is_valid, message).
        """
        code = code.upper().strip()
        code_data = self.get_invite_code(code)
        if not code_data:
            return False, "Invalid invite code."

        import time
        now = int(time.time())

        # Check expiry
        if code_data.get("expires_at") and code_data["expires_at"] < now:
            return False, "Invite code has expired."

        # Check max uses
        max_uses = code_data.get("max_uses")
        if max_uses and code_data["current_uses"] >= max_uses:
            return False, "Invite code has been used the maximum number of times."

        return True, "Valid invite code."

    def claim_invite_code(self, code: str, user_id: int) -> tuple[bool, str]:
        """
        Claim an invite code for a user. Returns (success, message).
        """
        code = code.upper().strip()
        code_data = self.get_invite_code(code)

        if not code_data:
            return False, "Invalid invite code."

        import time
        now = int(time.time())

        # Check expiry
        if code_data.get("expires_at") and code_data["expires_at"] < now:
            return False, "Invite code has expired."

        # Check max uses
        max_uses = code_data.get("max_uses")
        if max_uses and code_data["current_uses"] >= max_uses:
            return False, "Invite code has been used the maximum number of times."

        with self.transaction() as conn:
            # Mark code as claimed
            conn.execute(
                """
                UPDATE invite_codes
                SET current_uses = current_uses + 1, claimed_by = ?
                WHERE code = ?;
                """,
                (user_id, code),
            )

            # Mark user as onboarded with their invite code
            conn.execute(
                """
                UPDATE users
                SET onboarded = 1, invite_code = ?
                WHERE user_id = ?;
                """,
                (code, user_id),
            )

        return True, "Successfully claimed invite code."

    def get_invite_codes_for_user(self, created_by: int) -> list[dict]:
        """
        Get all invite codes created by a user, with claim status.
        """
        rows = self.execute(
            """
            SELECT ic.*, u.username as claimed_username
            FROM invite_codes ic
            LEFT JOIN users u ON ic.claimed_by = u.user_id
            WHERE ic.created_by = ?
            ORDER BY ic.created_at DESC;
            """,
            (created_by,),
        ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            d["is_claimed"] = d["claimed_username"] is not None
            result.append(d)
        return result

    def update_user_onboarded(self, user_id: int, invite_code: Optional[str] = None) -> None:
        """
        Mark a user as onboarded, optionally with an invite code.
        """
        with self.transaction() as conn:
            if invite_code:
                conn.execute(
                    """
                    UPDATE users
                    SET onboarded = 1, invite_code = ?
                    WHERE user_id = ?;
                    """,
                    (invite_code.upper(), user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET onboarded = 1 WHERE user_id = ?;",
                    (user_id,),
                )

    def get_user_onboarding_status(self, user_id: int) -> tuple[bool, Optional[str]]:
        """
        Check if a user is onboarded. Returns (is_onboarded, invite_code).
        """
        row = self.execute(
            "SELECT onboarded, invite_code FROM users WHERE user_id = ?;",
            (user_id,),
        ).fetchone()
        if not row:
            return False, None
        return bool(row["onboarded"]), row["invite_code"]

    def delete_invite_code(self, code: str) -> bool:
        """
        Permanently delete an invite code and revoke access for users who used it.
        Returns True if deleted.
        """
        code = code.upper()
        with self.transaction() as conn:
            # First, get the claimed_by user_id if any
            cur = conn.execute(
                "SELECT claimed_by FROM invite_codes WHERE code = ?;",
                (code,),
            )
            row = cur.fetchone()

            # Revoke onboarded status for any user who used this invite code
            conn.execute(
                """
                UPDATE users
                SET onboarded = 0, invite_code = NULL
                WHERE invite_code = ?;
                """,
                (code,),
            )

            # Delete the invite code
            cur = conn.execute(
                "DELETE FROM invite_codes WHERE code = ?;",
                (code,),
            )
            return cur.rowcount > 0


