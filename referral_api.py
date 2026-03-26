"""
Referral HTTP API (/api/referral/*).

XP: +300 for the referrer and +200 for the referee on each successful claim.
Codes are 8 alphanumeric characters, stored uppercase; matching is case-insensitive.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import string
import time
from typing import Any

import jwt
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from database_manager import DatabaseManager

logger = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8
REFERRER_XP_PER_CLAIM = 300
REFEREE_XP_PER_CLAIM = 200


class ClaimBody(BaseModel):
    code: str | None = Field(default=None)


def _normalize_code(raw: str) -> str:
    return (raw or "").strip().upper()


def _resolve_user_id(
    db: DatabaseManager,
    credentials: HTTPAuthorizationCredentials | None,
    jwt_secret: str,
) -> int | None:
    if credentials is None:
        return None
    try:
        payload = jwt.decode(
            credentials.credentials, jwt_secret, algorithms=["HS256"]
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

    session_id = payload.get("session_id")
    user_id = payload.get("tg_user_id")
    if not session_id or user_id is None:
        return None

    session = db.get_session(session_id)
    if not session or session.get("revoked_at") is not None:
        return None
    return int(user_id)


def _get_or_create_code(db: DatabaseManager, user_id: int) -> str:
    row = db.execute(
        "SELECT code FROM referral_codes WHERE user_id = ?;",
        (user_id,),
    ).fetchone()
    if row is not None:
        return str(row["code"])

    now = int(time.time())
    for _ in range(64):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        try:
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO referral_codes (user_id, code, created_at)
                    VALUES (?, ?, ?);
                    """,
                    (user_id, code, now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO referral_xp (user_id, xp) VALUES (?, 0);",
                    (user_id,),
                )
            return code
        except sqlite3.IntegrityError:
            continue

    raise RuntimeError("Could not allocate a unique referral code")


def _do_claim(db: DatabaseManager, referee_user_id: int, code: str) -> dict[str, Any]:
    owner = db.execute(
        "SELECT user_id FROM referral_codes WHERE code = ?;",
        (code,),
    ).fetchone()
    if owner is None:
        return {"success": False, "message": "Invalid referral code."}

    referrer_user_id = int(owner["user_id"])
    if referrer_user_id == referee_user_id:
        return {"success": False, "message": "You cannot use your own referral code."}

    existing = db.execute(
        "SELECT 1 AS ok FROM referral_claims WHERE referee_user_id = ?;",
        (referee_user_id,),
    ).fetchone()
    if existing is not None:
        return {"success": False, "message": "You have already claimed a referral code."}

    now = int(time.time())
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO referral_claims (referee_user_id, referrer_user_id, code, claimed_at)
                VALUES (?, ?, ?, ?);
                """,
                (referee_user_id, referrer_user_id, code, now),
            )
            for uid, delta in (
                (referrer_user_id, REFERRER_XP_PER_CLAIM),
                (referee_user_id, REFEREE_XP_PER_CLAIM),
            ):
                conn.execute(
                    """
                    INSERT INTO referral_xp (user_id, xp) VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET xp = referral_xp.xp + excluded.xp;
                    """,
                    (uid, delta),
                )
    except sqlite3.IntegrityError:
        return {"success": False, "message": "You have already claimed a referral code."}

    ref_row = db.execute(
        "SELECT xp FROM referral_xp WHERE user_id = ?;",
        (referrer_user_id,),
    ).fetchone()
    ref2_row = db.execute(
        "SELECT xp FROM referral_xp WHERE user_id = ?;",
        (referee_user_id,),
    ).fetchone()
    return {
        "success": True,
        "message": "Referral applied successfully.",
        "referrerXp": int(ref_row["xp"]) if ref_row else 0,
        "refereeXp": int(ref2_row["xp"]) if ref2_row else 0,
    }


def register_referral_routes(
    app: FastAPI, db: DatabaseManager, bearer_scheme: HTTPBearer
) -> None:
    router = APIRouter(prefix="/api/referral", tags=["referral"])

    @router.get("/my-code")
    async def referral_my_code(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Configuration error",
                    "detail": "JWT_SECRET not configured on server.",
                },
            )
        user_id = _resolve_user_id(db, credentials, jwt_secret)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            code = _get_or_create_code(db, user_id)
            return {"code": code}
        except Exception as e:
            logger.exception("GET /api/referral/my-code failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to get referral code", "detail": str(e)},
            )

    @router.get("/stats")
    async def referral_stats(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Configuration error",
                    "detail": "JWT_SECRET not configured on server.",
                },
            )
        user_id = _resolve_user_id(db, credentials, jwt_secret)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO referral_xp (user_id, xp) VALUES (?, 0);",
                    (user_id,),
                )
            row = db.execute(
                "SELECT xp FROM referral_xp WHERE user_id = ?;",
                (user_id,),
            ).fetchone()
            xp = int(row["xp"]) if row else 0
            rank_row = db.execute(
                "SELECT COUNT(*) AS n FROM referral_xp WHERE xp > ?;",
                (xp,),
            ).fetchone()
            rank = 1 + int(rank_row["n"] if rank_row else 0)
            referral_code = _get_or_create_code(db, user_id)
            count_row = db.execute(
                "SELECT COUNT(*) AS c FROM referral_claims WHERE referrer_user_id = ?;",
                (user_id,),
            ).fetchone()
            referral_count = int(count_row["c"]) if count_row else 0
            return {
                "xp": xp,
                "rank": rank,
                "referralCount": referral_count,
                "referralCode": referral_code,
            }
        except Exception as e:
            logger.exception("GET /api/referral/stats failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to get referral stats", "detail": str(e)},
            )

    @router.get("/leaderboard")
    async def referral_leaderboard():
        try:
            rows = db.execute(
                """
                SELECT
                    rx.user_id,
                    u.username,
                    rx.xp,
                    (SELECT COUNT(*) FROM referral_claims rc
                     WHERE rc.referrer_user_id = rx.user_id) AS referral_count,
                    (1 + (SELECT COUNT(*) FROM referral_xp r2 WHERE r2.xp > rx.xp)) AS rank
                FROM referral_xp rx
                LEFT JOIN users u ON u.user_id = rx.user_id
                ORDER BY rx.xp DESC, rx.user_id ASC
                LIMIT 20;
                """
            ).fetchall()
            leaderboard = [
                {
                    "rank": int(r["rank"]),
                    "userId": int(r["user_id"]),
                    "username": r["username"],
                    "xp": int(r["xp"]),
                    "referralCount": int(r["referral_count"]),
                }
                for r in rows
            ]
            return {"leaderboard": leaderboard}
        except Exception:
            logger.exception("GET /api/referral/leaderboard failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to get leaderboard"},
            )

    @router.post("/claim")
    async def referral_claim(
        body: ClaimBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to claim referral code"},
            )
        user_id = _resolve_user_id(db, credentials, jwt_secret)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

        raw = body.code
        if raw is None or not str(raw).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "Referral code is required"},
            )

        normalized = _normalize_code(str(raw))
        if len(normalized) != CODE_LENGTH:
            return {
                "success": False,
                "message": "Invalid referral code.",
            }

        try:
            return _do_claim(db, user_id, normalized)
        except Exception:
            logger.exception("POST /api/referral/claim failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to claim referral code"},
            )

    app.include_router(router)
