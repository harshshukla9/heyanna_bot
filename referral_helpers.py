"""
Shared referral claim logic (XP + platform access).

Successful claim sets users.onboarded = 1 for the referee when they were not
already onboarded, and stores the code in users.invite_code (same as admin invite flow).
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from database_manager import DatabaseManager

REFERRAL_CODE_LENGTH = 8
REFERRER_XP_PER_CLAIM = 300
REFEREE_XP_PER_CLAIM = 200


def normalize_referral_code(raw: str) -> str:
    return (raw or "").strip().upper()


def execute_referral_claim(
    db: "DatabaseManager", referee_user_id: int, code: str
) -> dict[str, Any]:
    """
    Apply a referral claim: record claim, award XP, grant platform access (onboard) to referee.
    `code` must already be normalized (uppercase, correct length for API callers).
    """
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
            conn.execute(
                """
                UPDATE users
                SET onboarded = 1,
                    invite_code = CASE
                        WHEN IFNULL(onboarded, 0) = 0 THEN ?
                        ELSE invite_code
                    END
                WHERE user_id = ?;
                """,
                (code, referee_user_id),
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
