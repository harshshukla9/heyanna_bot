"""
Referral HTTP API (/api/referral/*).

XP: +300 for the referrer and +200 for the referee on each successful claim.
Codes are 8 alphanumeric characters, stored uppercase; matching is case-insensitive.
A successful claim also grants platform access (same as invite onboarding) for users
who were not already onboarded.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import string
import time

import jwt
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from database_manager import DatabaseManager
from referral_helpers import (
    REFERRAL_CODE_LENGTH,
    execute_referral_claim,
    normalize_referral_code,
)

logger = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_uppercase + string.digits


class ClaimBody(BaseModel):
    code: str | None = Field(default=None)


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
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
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

        normalized = normalize_referral_code(str(raw))
        if len(normalized) != REFERRAL_CODE_LENGTH:
            return {
                "success": False,
                "message": "Invalid referral code.",
            }

        try:
            return execute_referral_claim(db, user_id, normalized)
        except Exception:
            logger.exception("POST /api/referral/claim failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to claim referral code"},
            )

    app.include_router(router)
