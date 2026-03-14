import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Mapping
from urllib.parse import parse_qsl

import jwt
import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from database_manager import DatabaseManager
import wallets
import bot_tools
import market_cache
import llm
from trading import execute_trade_for_user, cancel_order_for_user, get_open_orders_for_user

# Real-time copy trading tracker (WebSocket-based)
try:
    from copy_trading import get_manager, stop_daemon as stop_copy_tracking
    COPY_TRACKER_AVAILABLE = True
except ImportError:
    COPY_TRACKER_AVAILABLE = False

# Simple regex-based emoji stripper for API responses (keep Telegram UX unchanged).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FFFF\U00002700-\U000027BF]+",  # broad range of symbols/emojis
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    if not isinstance(text, str):
        return text
    return _EMOJI_RE.sub("", text)


def _format_balance_json_as_summary(balance_json: Dict[str, Any]) -> str:
    """Build human-readable on-chain summary from balance_json (avoids duplicate RPC fetch)."""
    if not balance_json or balance_json.get("error"):
        return "Error fetching Polygon balance."
    lines = ["📊 **Polygon Wallet Portfolio**\n"]
    for token in balance_json.get("tokens", []):
        lines.append(
            f"  • {token['symbol']}: {token['balance']:.4f} (${token['usd_value']:.2f})"
        )
    total = balance_json.get("total_usd", 0.0)
    lines.append(f"\n💰 **Total: ${total:.2f} USD**")
    return "\n".join(lines)


# Simple TTL cache for external API responses (reduces latency on repeat requests)
_CACHE_TTL_SEC = int(os.getenv("API_CACHE_TTL_SEC", "45"))
_response_cache: Dict[str, tuple[float, Any]] = {}
_response_cache_lock = threading.Lock()


def _cache_get(key: str) -> Any | None:
    with _response_cache_lock:
        if key not in _response_cache:
            return None
        expiry, value = _response_cache[key]
        if time.monotonic() > expiry:
            del _response_cache[key]
            return None
        return value


def _cache_set(key: str, value: Any, ttl_sec: int = _CACHE_TTL_SEC) -> None:
    with _response_cache_lock:
        _response_cache[key] = (time.monotonic() + ttl_sec, value)
        # Keep cache bounded (evict oldest if > 500 entries)
        if len(_response_cache) > 500:
            by_expiry = sorted(_response_cache.items(), key=lambda x: x[1][0])
            for k, _ in by_expiry[:100]:
                del _response_cache[k]


def _fetch_url(url: str, params: Dict[str, Any] | None = None, timeout: int = 10) -> Any:
    """Sync HTTP GET; run via asyncio.to_thread. Uses short TTL cache for idempotent GETs."""
    cache_key = f"get:{url}:{json.dumps(sorted((params or {}).items()), default=str)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    resp = requests.get(url, params=params or {}, timeout=timeout)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        _cache_set(cache_key, data)
        return data
    except Exception:
        return None


def _fetch_url_sync(url: str, params: Dict[str, Any], timeout: int) -> Any:
    """Sync HTTP GET that raises on error; for use in to_thread. Uses TTL cache."""
    cache_key = f"get:{url}:{json.dumps(sorted((params or {}).items()), default=str)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    resp = requests.get(url, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    _cache_set(cache_key, data)
    return data


def _fetch_pm_trades_sync(address: str) -> list:
    """Sync fetch of Polymarket trades for one user."""
    data = _fetch_url(
        "https://data-api.polymarket.com/trades",
        params={"user": address, "limit": 100, "takerOnly": "true"},
        timeout=10,
    )
    return data if isinstance(data, list) else []


def _fetch_pm_trades_global_sync(limit: int, offset: int) -> list:
    """Sync fetch of Polymarket global trades (cached)."""
    data = _fetch_url(
        "https://data-api.polymarket.com/trades",
        params={"limit": limit, "offset": offset, "takerOnly": "true"},
        timeout=12,
    )
    if data is None:
        resp = requests.get(
            "https://data-api.polymarket.com/trades",
            params={"limit": limit, "offset": offset, "takerOnly": "true"},
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else data


def _fetch_positions_sync(addr: str) -> list:
    """Sync fetch of Polymarket positions for one address (cached)."""
    data = _fetch_url(f"https://data-api.polymarket.com/positions?user={addr}", timeout=10)
    return data if isinstance(data, list) else []


def _fetch_closed_positions_sync(addr: str) -> list:
    """Sync fetch of Polymarket closed positions for one address (cached)."""
    data = _fetch_url(
        f"https://data-api.polymarket.com/closed-positions?user={addr}&limit=50",
        timeout=10,
    )
    return data if isinstance(data, list) else []


def _fetch_public_profile_sync(address: str) -> Dict[str, Any] | None:
    """Sync fetch of Polymarket public profile by wallet address (Gamma API)."""
    addr = (address or "").strip()
    if not addr or not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
        return None
    data = _fetch_url(
        "https://gamma-api.polymarket.com/public-profile",
        params={"address": addr},
        timeout=10,
    )
    return data if isinstance(data, dict) else None


def _get_balance_json_cached(addr: str) -> Dict[str, Any]:
    """Return Polygon balance JSON for address; use short TTL cache."""
    key = f"balance:{addr}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    out = bot_tools.get_polygon_balance_json(addr)
    _cache_set(key, out)
    return out


def _get_usdc_balance_usd(balance_json: Dict[str, Any]) -> float:
    """Extract total USDC / USDC.e balance in USD terms from a balance_json blob."""
    if not isinstance(balance_json, dict):
        return 0.0
    total = 0.0
    for tok in balance_json.get("tokens", []) or []:
        try:
            sym = str(tok.get("symbol") or "").upper()
            if "USDC" not in sym:
                continue
            total += float(tok.get("usd_value") or 0.0)
        except Exception:
            continue
    return total


def _compute_fractional_amount_for_hook(
    leader_address: str,
    follower_user_id: int,
    leader_trade_amount_usd: float,
    cfg: Dict[str, Any],
) -> float | None:
    """
    Compute follower trade amount based on fraction of USDC.e balance.

    Example:
      - Leader balance: $150, trade: $15 → 10%
      - Follower balance: $200 → follower_amount = 10% of 200 = $20
      - size_multiplier scales this result.
    """
    if leader_trade_amount_usd <= 0:
        return None

    try:
        leader_bal = _get_usdc_balance_usd(_get_balance_json_cached(leader_address))
    except Exception:
        leader_bal = 0.0
    if leader_bal <= 0:
        return None

    follower = db.get_user(follower_user_id)
    if not follower:
        return None
    follower_addr = follower.get("eth_address") or ""
    if not follower_addr:
        return None

    try:
        follower_bal = _get_usdc_balance_usd(_get_balance_json_cached(follower_addr))
    except Exception:
        follower_bal = 0.0
    if follower_bal <= 0:
        return None

    # Fraction of leader's USDC.e balance used for this trade.
    frac = leader_trade_amount_usd / leader_bal
    if frac <= 0:
        return None
    # Cap at 100% to avoid extreme leverage due to stale balances.
    if frac > 1:
        frac = 1.0

    try:
        size_mult = float(cfg.get("size_multiplier") or 1.0)
    except Exception:
        size_mult = 1.0

    amount = follower_bal * frac * size_mult

    max_per = cfg.get("max_usd_per_trade")
    try:
        if max_per is not None:
            max_per_f = float(max_per)
            if max_per_f > 0:
                amount = min(amount, max_per_f)
    except Exception:
        pass

    return amount if amount > 0 else None


def _compute_amount_for_hook_mode(
    *,
    mode: str,
    leader_trade_amount_usd: float,
    follower_user_id: int,
    leader_address: str | None,
    cfg: Dict[str, Any],
) -> float | None:
    """
    Unified sizing logic for copy hooks.

    Modes:
      - "fractional": same % of follower balance as leader (uses USDC.e balances).
      - "one_to_one": same USD amount as leader (optionally capped by max_usd_per_trade).
      - "beginner": fixed small USD amount per trade (default $1).
      - fallback: amount * size_multiplier (simple multiplier).
    """
    m = (mode or "").lower()

    # Beginner: flat small stake per copied trade.
    if m == "beginner":
        try:
            amt = float(cfg.get("fixed_usd_amount") or 1.0)
        except Exception:
            amt = 1.0
        if amt <= 0:
            amt = 1.0
        max_per = cfg.get("max_usd_per_trade")
        try:
            if max_per is not None:
                max_per_f = float(max_per)
                if max_per_f > 0:
                    amt = min(amt, max_per_f)
        except Exception:
            pass
        return amt

    # 1:1 same USD size as leader.
    if m == "one_to_one":
        amt = max(0.0, float(leader_trade_amount_usd or 0.0))
        max_per = cfg.get("max_usd_per_trade")
        try:
            if max_per is not None:
                max_per_f = float(max_per)
                if max_per_f > 0:
                    amt = min(amt, max_per_f)
        except Exception:
            pass
        return amt if amt > 0 else None

    # Fractional: same percentage of balance.
    fractional_flag = bool(cfg.get("fractional")) or m == "fractional"
    if fractional_flag and leader_address:
        return _compute_fractional_amount_for_hook(
            leader_address=leader_address,
            follower_user_id=follower_user_id,
            leader_trade_amount_usd=leader_trade_amount_usd,
            cfg=cfg,
        )

    # Fallback: multiplier on leader amount.
    try:
        mult = float(cfg.get("size_multiplier") or 1.0)
    except Exception:
        mult = 1.0
    amt = max(0.0, float(leader_trade_amount_usd or 0.0)) * mult
    max_per = cfg.get("max_usd_per_trade")
    try:
        if max_per is not None:
            max_per_f = float(max_per)
            if max_per_f > 0:
                amt = min(amt, max_per_f)
    except Exception:
        pass
    return amt if amt > 0 else None


def _get_market_price_for_outcome(condition_id: str, outcome: str) -> float | None:
    """
    Best-effort current market price (decimal, 0-1) for a given condition_id/outcome.
    Uses cached CLOB odds via market_cache.
    """
    try:
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if not m:
            return None
        cents = m.odds.get(outcome.strip().capitalize())
        if cents is None:
            return None
        return float(cents) / 100.0
    except Exception:
        return None


def _build_portfolio_from_fetched(
    addr: str,
    on_chain_summary: str,
    balance_json: Dict[str, Any],
    positions_raw: list,
    closed_raw: list,
) -> Dict[str, Any]:
    """Build portfolio dict from pre-fetched balance, positions, and closed positions."""
    positions_json: list[Dict[str, Any]] = []
    markets_map: Dict[str, Dict[str, Any]] = {}
    total_pnl = 0.0
    portfolio_value = 0.0

    for p in positions_raw:
        try:
            title = p.get("title", "Unknown Market")
            outcome = p.get("outcome", "Unknown")
            condition_id = p.get("conditionId") or p.get("condition_id") or ""
            size = float(p.get("size", 0) or 0)
            avg_price = float(p.get("avgPrice", 0) or 0)
            cur_price = float(p.get("curPrice", 0) or 0)
            cur_val = float(p.get("currentValue", 0) or 0)
            pnl_pct = float(p.get("percentPnl", 0) or 0)
            cash_pnl = float(p.get("cashPnl", 0) or 0)
        except (TypeError, ValueError):
            continue

        positions_json.append(
            {
                "title": title,
                "outcome": outcome,
                "condition_id": condition_id,
                "size": size,
                "avg_price": avg_price,
                "current_price": cur_price,
                "current_value": cur_val,
                "pnl_percent": pnl_pct,
                "pnl_cash": cash_pnl,
            }
        )
        total_pnl += cash_pnl
        portfolio_value += cur_val

        market_key = condition_id or title
        if market_key not in markets_map:
            markets_map[market_key] = {
                "condition_id": condition_id,
                "title": title,
                "pnl_cash": 0.0,
                "current_value": 0.0,
            }
        markets_map[market_key]["pnl_cash"] += cash_pnl
        markets_map[market_key]["current_value"] += cur_val

    markets_pnl = []
    for m in markets_map.values():
        value = m["current_value"]
        pnl_cash = m["pnl_cash"]
        pnl_percent = (pnl_cash / value * 100.0) if value > 0 else 0.0
        markets_pnl.append(
            {
                "condition_id": m["condition_id"],
                "title": m["title"],
                "pnl_cash": pnl_cash,
                "pnl_percent": pnl_percent,
                "current_value": value,
            }
        )

    closed_positions = []
    closed_pnl = 0.0
    win_count = 0
    loss_count = 0
    for p in closed_raw:
        try:
            title = p.get("title", "Unknown Market")
            outcome = p.get("outcome", "Unknown")
            condition_id = p.get("conditionId") or p.get("condition_id") or ""
            total_bought = float(p.get("totalBought", 0) or 0)
            avg_price = float(p.get("avgPrice", 0) or 0)
            realized_pnl = float(p.get("realizedPnl", 0) or p.get("cashPnl", 0) or 0)
            pnl_pct = (realized_pnl / total_bought * 100.0) if total_bought > 0 else 0.0
            ts = p.get("timestamp")
            end_date = p.get("endDate") or p.get("end_date")
            if realized_pnl > 0:
                win_count += 1
            elif realized_pnl < 0:
                loss_count += 1
            closed_positions.append(
                {
                    "title": title,
                    "outcome": outcome,
                    "condition_id": condition_id,
                    "total_bought": total_bought,
                    "avg_price": avg_price,
                    "pnl_percent": pnl_pct,
                    "pnl_cash": realized_pnl,
                    "timestamp": ts,
                    "end_date": end_date,
                }
            )
            closed_pnl += realized_pnl
        except (TypeError, ValueError):
            continue

    total_pnl_all = total_pnl + closed_pnl
    closed_count = len(closed_positions)
    open_count = len(positions_json)
    win_rate = (win_count / closed_count * 100.0) if closed_count > 0 else None

    return {
        "wallet": addr,
        "on_chain_summary": strip_emoji(on_chain_summary),
        "balance": balance_json,
        "positions": positions_json,
        "closed_positions": closed_positions,
        "markets": markets_pnl,
        "summary": {
            "open_positions_count": open_count,
            "closed_positions_count": closed_count,
            "total_realized_pnl": closed_pnl,
            "total_unrealized_pnl": total_pnl,
            "total_pnl_all": total_pnl_all,
            "portfolio_value": portfolio_value,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate_percent": win_rate,
        },
        "totals": {
            "portfolio_value": portfolio_value,
            "total_pnl": total_pnl,
            "closed_pnl": closed_pnl,
            "total_pnl_all": total_pnl_all,
        },
    }


def _verify_telegram_init_data(init_data: str, bot_token: str) -> Dict[str, Any]:
    """
    Verify Telegram Mini App initData using HMAC-SHA256 as per Telegram docs.
    Returns the parsed `user` object if verification succeeds.
    """
    params = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=400, detail="Missing hash in initData.")

    # Build data-check-string
    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params.keys()))

    # secret key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature.")

    user_str = params.get("user")
    if not user_str:
        raise HTTPException(status_code=400, detail="Missing user field in initData.")

    try:
        user_obj = json.loads(user_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid user JSON in initData.")

    return user_obj


def _verify_telegram_login_widget(params: Mapping[str, str], bot_token: str) -> Dict[str, Any]:
    """
    Verify data sent by the classic Telegram Login Widget.
    Uses SHA256(bot_token) as secret key per Telegram docs.
    """
    received_hash = params.get("hash")
    if not received_hash:
        raise HTTPException(status_code=400, detail="Missing hash in Telegram login data.")

    data_check_params = {k: v for k, v in params.items() if k != "hash" and v is not None}
    data_check_string = "\n".join(
        f"{k}={data_check_params[k]}" for k in sorted(data_check_params.keys())
    )

    # secret key = SHA256(bot_token)
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram login signature.")

    # Build a user-like object from the fields login widget sends
    user_obj: Dict[str, Any] = {
        "id": int(data_check_params["id"]) if "id" in data_check_params else None,
        "first_name": data_check_params.get("first_name"),
        "last_name": data_check_params.get("last_name"),
        "username": data_check_params.get("username"),
        "photo_url": data_check_params.get("photo_url"),
        "auth_date": int(data_check_params.get("auth_date", "0") or "0"),
    }
    return user_obj


def create_api_app(db: DatabaseManager) -> FastAPI:
    """
    Build and return the FastAPI application.
    Provides:
      - /health          : basic readiness check
      - /auth/telegram   : login/signup via Telegram initData, create DB user+session, mint JWT
      - /auth/manual     : DEV-ONLY manual login for testing (bypasses Telegram)
      - /auth/logout     : revoke current session (logout)
      - /test/login      : simple HTML page with Telegram Login Widget for manual testing
      - /trade           : unified trading endpoint (JWT-protected)
    """
    app = FastAPI(title="Polymarket Hybrid Trading API")

    # CORS: allow frontend at beta.heyanna.trade to call the API directly.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    bearer_scheme = HTTPBearer(auto_error=False)

    # ── Background task: real-time copy trading via WebSocket ──
    async def _execute_copy_trade_callback(
        follower_user_id: int,
        outcome: str,
        amount_usd: float,
        condition_id: str,
        order_side: str,
        leader_address: str = "",
        hook_id: int = None
    ):
        """
        Callback for executing copy trades.
        This is called by the WebSocket tracker when a tracked wallet makes a trade.
        """
        try:
            logging.getLogger(__name__).info(
                f"[COPY TRADE] Follower={follower_user_id} Outcome={outcome} "
                f"Amount=${amount_usd:.2f} Condition={condition_id[:20]}... Side={order_side}"
            )

            # Execute the trade using existing function
            result = await asyncio.to_thread(
                execute_trade_for_user,
                db,
                follower_user_id,
                outcome,
                amount_usd,
                condition_id,
                order_side,
                copied_from_user_id=None
            )

            logging.getLogger(__name__).info(f"[COPY TRADE] Result: {result}")

        except Exception as e:
            logging.getLogger(__name__).error(f"Copy trade execution error: {e}", exc_info=True)

    async def _global_copy_trading_loop_websocket():
        """
        Real-time copy trading using WebSocket stream.
        Instantly mirrors trades from tracked wallets.
        """
        if os.getenv("DISABLE_GLOBAL_COPY_TRADING_LOOP", "").strip().lower() in ("1", "true", "yes"):
            logging.getLogger(__name__).info("Global copy trading loop disabled via env var")
            return

        if not COPY_TRACKER_AVAILABLE:
            logging.getLogger(__name__).warning("Copy tracker not available, using polling fallback")
            asyncio.create_task(_global_copy_trading_loop_polling())
            return

        try:
            tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
            tracker.on_trade = _execute_copy_trade_callback
            logging.getLogger(__name__).info("[COPY TRADING] Starting WebSocket tracker...")
            await tracker.run()
        except Exception as e:
            logging.getLogger(__name__).error(f"WebSocket copy-trading loop error: {e}", exc_info=True)

    async def _global_copy_trading_loop_polling():
        """
        Fallback polling-based copy-trading indexer.
        Used when WebSocket is unavailable.
        """
        interval_sec = float(os.getenv("GLOBAL_COPY_TRADING_INTERVAL_SEC", "0.05") or "0.05")
        if interval_sec < 0.05:
            interval_sec = 0.05

        while True:
            try:
                await _run_global_copy_trading_tick(limit_per_leader=50)
                await admin_run_stop_loss_tick()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Polling copy-trading loop error: {e}")
            await asyncio.sleep(interval_sec)

    @app.on_event("startup")
    async def _start_background_tasks():
        # Initialize copy trading schema
        if COPY_TRACKER_AVAILABLE:
            try:
                tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                tracker.init_schema()
                logging.getLogger(__name__).info("[COPY TRADING] Schema initialized")
            except Exception as e:
                logging.getLogger(__name__).error(f"[COPY TRADING] Schema init error: {e}")

        # Prefer WebSocket for real-time, fallback to polling
        websocket_enabled = COPY_TRACKER_AVAILABLE and not os.getenv("DISABLE_WEBSOCKET_TRACKER", "").strip().lower() in ("1", "true", "yes")

        if websocket_enabled:
            logging.getLogger(__name__).info("[COPY TRADING] WebSocket mode enabled")
            asyncio.create_task(_global_copy_trading_loop_websocket())
        else:
            logging.getLogger(__name__).info("[COPY TRADING] Using polling mode")
            asyncio.create_task(_global_copy_trading_loop_polling())

    @app.get("/health", tags=["system"], summary="Health check")
    async def health():
        return {"status": "ok"}

    @app.post(
        "/admin/trades/flush-invalid",
        tags=["system"],
        summary="Flush trades without valid tx_hash",
    )
    async def flush_invalid_trades():
        """
        Delete all trades that have no tx_hash, empty tx_hash, or tx_hash = 'pending'.
        Use this once to clean the feed so only on-chain settled trades remain.
        """
        deleted = db.delete_trades_without_valid_tx_hash()
        return {"deleted": deleted, "message": f"Removed {deleted} trades without valid tx_hash."}

    @app.get(
        "/test/login",
        response_class=HTMLResponse,
        tags=["auth"],
        summary="Test page with Telegram Login Widget",
    )
    async def test_login():
        """
        Simple HTML page that shows the Telegram Login Widget and
        sends the user to /auth/telegram-widget on successful login.
        """
        bot_username = os.getenv("BOT_USERNAME", "")
        if not bot_username:
            return HTMLResponse(
                "<h1>BOT_USERNAME not configured</h1>"
                "<p>Set BOT_USERNAME in your .env to use this test page.</p>",
                status_code=500,
            )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Telegram Login Test</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #050816;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      margin: 0;
    }}
    .card {{
      background: #0f172a;
      padding: 32px 40px;
      border-radius: 16px;
      box-shadow: 0 20px 40px rgba(15, 23, 42, 0.7);
      max-width: 420px;
      text-align: center;
      border: 1px solid rgba(148, 163, 184, 0.25);
    }}
    h1 {{
      font-size: 1.6rem;
      margin-bottom: 0.75rem;
    }}
    p {{
      font-size: 0.95rem;
      color: #9ca3af;
      margin-bottom: 1.5rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Telegram Login (Test)</h1>
    <p>Use the Telegram Login button below to authenticate and receive a JWT for the API.</p>
    <script async src="https://telegram.org/js/telegram-widget.js?22"
            data-telegram-login="{bot_username}"
            data-size="large"
            data-userpic="false"
            data-auth-url="/auth/telegram-widget"
            data-request-access="write">
    </script>
  </div>
</body>
</html>
"""
        return HTMLResponse(html)

    @app.post(
        "/auth/telegram",
        tags=["auth"],
        summary="Login via Telegram Mini App initData",
    )
    async def auth_telegram(init_data: str):
        """
        Verify Telegram Mini App initData and return a short-lived JWT that
        encodes the permanent Telegram user_id and a server-side session_id.
        """
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise HTTPException(
                status_code=500, detail="BOT_TOKEN not configured on server."
            )

        user_obj = _verify_telegram_init_data(init_data, bot_token)
        tg_user_id = user_obj.get("id")
        if tg_user_id is None:
            raise HTTPException(
                status_code=400, detail="Telegram user id missing in initData."
            )

        # Ensure user exists; if not, create with a fresh wallet (unencrypted private key for now).
        db_user = db.get_user(tg_user_id)
        if not db_user:
            eth_wallet = wallets.generate_eth_wallet()
            username = user_obj.get("username") or ""
            db.create_user(
                user_id=tg_user_id,
                username=username,
                eth_data=eth_wallet,
                sol_data=("", ""),
            )

        # Create a login session row
        session_id = db.create_session(tg_user_id)

        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            raise HTTPException(
                status_code=500, detail="JWT_SECRET not configured on server."
            )

        ttl_seconds = int(os.getenv("JWT_TTL_SECONDS", "604800"))  # default 7 days
        now = int(time.time())
        payload = {
            "sub": str(tg_user_id),
            "tg_user_id": tg_user_id,
            "session_id": session_id,
            "iat": now,
            "exp": now + ttl_seconds,
        }

        token = jwt.encode(payload, jwt_secret, algorithm="HS256")
        return {"token": token}

    @app.post(
        "/auth/manual",
        tags=["auth"],
        summary="Dev-only manual login to obtain JWT",
    )
    async def auth_manual(user_id: int):
        """
        DEV-ONLY: Manually create a session and JWT for a given Telegram user_id.
        Useful for local testing without Telegram login.
        """
        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            raise HTTPException(
                status_code=500, detail="JWT_SECRET not configured on server."
            )

        db_user = db.get_user(user_id)
        if not db_user:
            # Auto-create a wallet for testing if user does not exist
            eth_wallet = wallets.generate_eth_wallet()
            db.create_user(
                user_id=user_id,
                username="manual-test",
                eth_data=eth_wallet,
                sol_data=("", ""),
            )

        session_id = db.create_session(user_id)

        ttl_seconds = int(os.getenv("JWT_TTL_SECONDS", "604800"))  # default 7 days
        now = int(time.time())
        payload = {
            "sub": str(user_id),
            "tg_user_id": user_id,
            "session_id": session_id,
            "iat": now,
            "exp": now + ttl_seconds,
        }
        token = jwt.encode(payload, jwt_secret, algorithm="HS256")
        return {"token": token, "session_id": session_id}

    def _get_current_session(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> Dict[str, Any]:
        """
        Decode JWT, validate signature and expiry, and ensure the DB session is active.
        """
        if credentials is None:
            raise HTTPException(status_code=401, detail="Missing Authorization header.")

        token = credentials.credentials
        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            raise HTTPException(
                status_code=500, detail="JWT_SECRET not configured on server."
            )

        try:
            payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired.")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token.")

        session_id = payload.get("session_id")
        user_id = payload.get("tg_user_id")
        if not session_id or user_id is None:
            raise HTTPException(status_code=401, detail="Invalid session payload.")

        session = db.get_session(session_id)
        if not session or session.get("revoked_at") is not None:
            raise HTTPException(status_code=401, detail="Session is revoked or invalid.")

        return {"user_id": int(user_id), "session_id": session_id, "payload": payload}

    @app.post(
        "/auth/logout",
        tags=["auth"],
        summary="Logout and revoke current session",
    )
    async def logout(current=Depends(_get_current_session)):
        """
        Log out the current session by marking it revoked in the database.
        """
        db.revoke_session(current["session_id"])
        return {"success": True}

    @app.post(
        "/me/copy-trading/enable",
        tags=["copy-trading"],
        summary="Enable copy trading (activates all your hooks as follower)",
    )
    async def enable_copy_trading(current=Depends(_get_current_session)):
        """
        Enable copy trading for your account.

        When enabled:
        - All your follow hooks become active
        - You will receive copy trades when followed leaders trade
        - Requires at least one hook to be set up via /copy-trading/follow

        Example:
        ```bash
        curl -X POST http://localhost:8000/me/copy-trading/enable \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {"copy_trading_enabled": true}
        ```
        """
        with db.transaction() as conn:
            conn.execute(
                "UPDATE users SET copy_trading_enabled = 1 WHERE user_id = ?;",
                (current["user_id"],),
            )
            # Also mark all hooks for this follower as enabled so the global
            # copy-trading indexer (_run_global_copy_trading_tick) will pick them up.
            conn.execute(
                "UPDATE copy_hooks SET enabled = 1 WHERE follower_user_id = ?;",
                (current["user_id"],),
            )
        return {"copy_trading_enabled": True}

    @app.post(
        "/me/copy-trading/disable",
        tags=["copy-trading"],
        summary="Disable copy trading (deactivates all your hooks)",
    )
    async def disable_copy_trading(current=Depends(_get_current_session)):
        """
        Disable copy trading for your account.

        When disabled:
        - All your follow hooks become inactive
        - You will NOT receive any new copy trades
        - Your hooks remain saved and can be re-enabled later

        Example:
        ```bash
        curl -X POST http://localhost:8000/me/copy-trading/disable \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {"copy_trading_enabled": false}
        ```
        """
        with db.transaction() as conn:
            conn.execute(
                "UPDATE users SET copy_trading_enabled = 0 WHERE user_id = ?;",
                (current["user_id"],),
            )
            # Disable all hooks for this follower so the global indexer
            # stops mirroring trades for them.
            conn.execute(
                "UPDATE copy_hooks SET enabled = 0 WHERE follower_user_id = ?;",
                (current["user_id"],),
            )
        return {"copy_trading_enabled": False}

    @app.get(
        "/me/copy-trading",
        tags=["copy-trading"],
        summary="Get current copy-trading state and followed leaders",
    )
    async def get_copy_trading_state(current=Depends(_get_current_session)):
        """
        Get your current copy-trading configuration.

        Returns:
        - copy_trading_enabled: Whether copy trading is active
        - following: List of wallets you are following
        - following_count: Number of followed wallets

        Example:
        ```bash
        curl http://localhost:8000/me/copy-trading \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {
          "copy_trading_enabled": true,
          "following": [
            {
              "hook_id": 1,
              "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
              "display_name": "0xff2a...cde0",
              "config": {"mode": "percentage", "percentage": 50.0}
            }
          ],
          "following_count": 1
        }
        ```
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        rows = db.execute(
            """
            SELECT
                h.id AS hook_id,
                h.leader_address,
                h.config,
                h.enabled
            FROM copy_hooks h
            WHERE h.follower_user_id = ? AND h.enabled = 1;
            """,
            (current["user_id"],),
        ).fetchall()

        leaders: list[Dict[str, Any]] = []
        for r in rows:
            r = dict(r)
            config = json.loads(r.get("config") or "{}")
            leader_address = r.get("leader_address") or config.get("leader_address")
            if leader_address:
                leaders.append(
                    {
                        "hook_id": int(r["hook_id"]),
                        "leader_address": leader_address,
                        "display_name": config.get("display_name") or leader_address[:10] + "...",
                        "config": config,
                    }
                )

        return {
            "copy_trading_enabled": bool(user.get("copy_trading_enabled") or 0),
            "following": leaders,
            "following_count": len(leaders),
        }

    @app.get(
        "/me/copy-trading/notifications",
        tags=["copy-trading"],
        summary="Recent copy-trade executions for current user",
    )
    async def get_copy_trading_notifications(
        limit: int = 20,
        current=Depends(_get_current_session),
    ):
        """
        Get your recent copy-trade notifications.

        Returns trades that were automatically executed by following other traders.

        Parameters:
        - limit: Maximum number of notifications to return (1-100, default 20)

        Example:
        ```bash
        curl "http://localhost:8000/me/copy-trading/notifications?limit=10" \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {
          "notifications": [
            {
              "market_id": 12345,
              "condition_id": "0x9c414...",
              "side": "Yes",
              "amount": 10.50,
              "size": 20.19,
              "price": 0.52,
              "order_side": "BUY",
              "status": "matched",
              "tx_hash": "0xabc123...",
              "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
              "executed_at": 1773412919
            }
          ],
          "limit": 10
        }
        ```
        """
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        rows = db.execute(
            """
            SELECT
                t.executed_at,
                t.market_id,
                t.condition_id,
                t.side,
                t.amount,
                t.size,
                t.price,
                t.order_side,
                t.status,
                t.tx_hash,
                t.copied_from_user_id,
                u.username AS leader_username,
                u.eth_address AS leader_address
            FROM trades t
            LEFT JOIN users u ON u.user_id = t.copied_from_user_id
            WHERE t.user_id = ? AND t.copied_from_user_id IS NOT NULL
            ORDER BY t.executed_at DESC
            LIMIT ?;
            """,
            (user["user_id"], limit),
        ).fetchall()

        notifications: list[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            notifications.append(
                {
                    "executed_at": d.get("executed_at"),
                    "market_id": d.get("market_id"),
                    "condition_id": d.get("condition_id"),
                    "side": d.get("side"),
                    "amount": float(d.get("amount") or 0.0),
                    "size": float(d.get("size") or 0.0) if d.get("size") is not None else None,
                    "price": float(d.get("price") or 0.0) if d.get("price") is not None else None,
                    "order_side": d.get("order_side"),
                    "status": d.get("status"),
                    "tx_hash": d.get("tx_hash"),
                    "leader_user_id": d.get("copied_from_user_id"),
                    "leader_username": d.get("leader_username"),
                    "leader_address": d.get("leader_address"),
                }
            )

        return {
            "notifications": notifications,
            "limit": limit,
        }

    class FollowRequest(BaseModel):
        """Request body for following a copy trading leader."""
        # Leader identity (provide one)
        leader_username: str = ""      # Local username (e.g., "trader123")
        leader_address: str = ""       # Or wallet address (e.g., "0x...")
        # Copy sizing / risk settings
        size_multiplier: float = 1.0   # Multiplier for computed trade size
        max_usd_per_trade: float = 0.0 # Max USD per trade (0 = no cap)
        fractional: bool = True        # Fractional sell mode
        mode: str = "fractional"       # "fractional", "one_to_one", "beginner"
        fixed_usd_amount: float = 1.0  # Fixed USD amount (used when mode="beginner")
        # Risk controls
        max_loss_pct: float = 0.0      # Stop-loss % (0 = disabled)
        slippage_pct: float = 0.0      # Max price slippage % (0 = disabled)

    @app.post(
        "/copy-trading/follow",
        tags=["copy-trading"],
        summary="Follow a leader by username (creates a hook)",
    )
    async def follow_copy_trading(
        body: FollowRequest, current=Depends(_get_current_session)
    ):
        """
        Start following a leader for copy trading.

        Creates a hook that automatically mirrors trades when the leader trades.

        Parameters:
        - leader_username: Follow a local user by username
        - leader_address: Follow a wallet address (global leader)
        - mode: Trade sizing mode ("fractional", "one_to_one", "beginner")
        - size_multiplier: Scale factor for trade amounts
        - max_usd_per_trade: Maximum USD per trade (0 = unlimited)
        - fractional: Enable fractional sell (sell same % as leader)
        - fixed_usd_amount: Fixed USD amount per trade (for "beginner" mode)
        - max_loss_pct: Stop-loss percentage (0 = disabled)
        - slippage_pct: Maximum allowed slippage (0 = disabled)

        Example:
        ```bash
        curl -X POST http://localhost:8000/copy-trading/follow \\
             -H "Authorization: Bearer YOUR_TOKEN" \\
             -H "Content-Type: application/json" \\
             -d '{"leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0", "mode": "beginner"}'
        ```

        Response:
        ```json
        {
          "hook_id": 1,
          "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
          "global": true,
          "following": true
        }
        ```
        """
        leader_username = (body.leader_username or "").strip()
        leader_address = (body.leader_address or "").strip()
        mode = (body.mode or "").strip().lower() or ("fractional" if body.fractional else "multiplier")

        cfg_base: Dict[str, Any] = {
            "size_multiplier": body.size_multiplier,
            "max_usd_per_trade": body.max_usd_per_trade,
            "fractional": bool(body.fractional),
            "mode": mode,
            "fixed_usd_amount": body.fixed_usd_amount,
            "max_loss_pct": body.max_loss_pct,
            "slippage_pct": body.slippage_pct,
        }

        if not leader_username and not leader_address:
            raise HTTPException(
                status_code=400,
                detail="leader_username or leader_address is required.",
            )

        # Path 1: follow by wallet address (global profile).
        if leader_address:
            if not re.fullmatch(r"0x[a-fA-F0-9]{40}", leader_address):
                raise HTTPException(status_code=400, detail="Invalid leader_address format.")

            # If this address already belongs to a local app user, fall back to the
            # existing local-follow path so hooks fire via _fire_copy_hooks.
            leader = db.get_user_by_address(leader_address)
            if leader:
                if leader["user_id"] == current["user_id"]:
                    raise HTTPException(status_code=400, detail="Cannot follow yourself.")

                hook_id = db.add_global_copy_hook(
                    follower_user_id=current["user_id"],
                    leader_address=leader["eth_address"],
                    config={
                        **cfg_base,
                        "leader_address": leader["eth_address"],
                        "display_name": leader["username"] or leader["eth_address"],
                    },
                )
                if COPY_TRACKER_AVAILABLE:
                    try:
                        tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                        tracker.reload()
                    except Exception as e:
                        logging.getLogger(__name__).warning(f"Failed to reload hooks after follow: {e}")
                return {
                    "hook_id": hook_id,
                    "leader_user_id": leader["user_id"],
                    "leader_username": leader["username"],
                    "leader_address": leader["eth_address"],
                    "global": False,
                    "following": True,
                }

            # Global-only profile: create a config-based hook keyed by leader_address.
            # These hooks are meant to be picked up by a global trades indexer rather
            # than _fire_copy_hooks (which is local-user based).
            cfg = {
                **cfg_base,
                "leader_address": leader_address,
                "display_name": leader_username or leader_address,
            }

            hook_id = db.add_global_copy_hook(
                follower_user_id=current["user_id"],
                leader_address=leader_address,
                config=cfg,
            )

            if COPY_TRACKER_AVAILABLE:
                try:
                    tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                    tracker.reload()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to reload hooks after follow: {e}")

            return {
                "hook_id": hook_id,
                "leader_user_id": None,
                "leader_username": cfg["display_name"],
                "leader_address": leader_address,
                "global": True,
                "following": True,
            }

        # Path 2: follow by local username (existing behavior, extended with config).
        leader = db.get_user_by_username(leader_username)
        if not leader:
            raise HTTPException(status_code=404, detail="Leader user not found.")
        if leader["user_id"] == current["user_id"]:
            raise HTTPException(status_code=400, detail="Cannot follow yourself.")

        local_cfg = {
            **cfg_base,
            # Also record leader_address for potential global/indexer use.
            "leader_address": leader.get("eth_address"),
            "display_name": leader.get("username") or leader.get("eth_address"),
        }
        hook_id = db.create_copy_hook(
            follower_user_id=current["user_id"],
            leader_user_id=leader["user_id"],
            config=local_cfg,
        )
        # Keep copy_trading_follows in sync for backward compat
        now = int(time.time())
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO copy_trading_follows (
                    follower_user_id, leader_user_id, created_at
                ) VALUES (?, ?, ?);
                """,
                (current["user_id"], leader["user_id"], now),
            )

        if COPY_TRACKER_AVAILABLE:
            try:
                tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                tracker.reload()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to reload hooks after follow: {e}")

        return {
            "hook_id": hook_id,
            "leader_user_id": leader["user_id"],
            "leader_username": leader["username"],
            "leader_address": leader["eth_address"],
            "global": False,
            "following": True,
        }

    @app.post(
        "/copy-trading/unfollow",
        tags=["copy-trading"],
        summary="Unfollow a leader by username or wallet (removes hook)",
    )
    async def unfollow_copy_trading(
        body: FollowRequest, current=Depends(_get_current_session)
    ):
        """
        Stop following a leader for copy trading.

        Removes the hook so you will no longer receive copy trades from this leader.

        Parameters:
        - leader_address: Wallet address of the leader to unfollow (preferred)
        - leader_username: Username of the leader (deprecated)

        Example:
        ```bash
        curl -X POST http://localhost:8000/copy-trading/unfollow \\
             -H "Authorization: Bearer YOUR_TOKEN" \\
             -H "Content-Type: application/json" \\
             -d '{"leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0"}'
        ```

        Response:
        ```json
        {
          "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
          "following": false
        }
        ```
        """
        leader_username = (body.leader_username or "").strip()
        leader_address = (body.leader_address or "").strip()

        # If an address is provided, try to remove a global hook.
        if leader_address:
            addr_norm = leader_address.lower()

            # Try to find the hook by leader_address
            hook = db.execute(
                """
                SELECT id, config FROM copy_hooks
                WHERE follower_user_id = ? AND lower(leader_address) = lower(?);
                """,
                (current["user_id"], addr_norm),
            ).fetchone()

            if hook:
                # Delete the hook by address
                db.remove_global_copy_hook(
                    follower_user_id=current["user_id"],
                    leader_address=addr_norm,
                )
                display_name = "Global Wallet"
                target_leader_id = None  # Global hooks don't have a leader_user_id
            else:
                raise HTTPException(status_code=404, detail="Hook not found for this address.")

            if COPY_TRACKER_AVAILABLE:
                try:
                    tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                    tracker.reload()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to reload hooks after unfollow: {e}")

            return {
                "leader_user_id": target_leader_id,
                "leader_username": display_name,
                "leader_address": leader_address,
                "following": False,
            }

        # Fallback: unfollow by username (deprecated - use address-based unfollow).
        # This path is kept for backward compatibility but should be removed in future.
        # For now, we just return an error suggesting to use address-based unfollow.
        raise HTTPException(
            status_code=400,
            detail="Unfollow by username is deprecated. Please unfollow by wallet address instead."
        )

        # Unreachable, but kept for type checking:
        leader = None
        if COPY_TRACKER_AVAILABLE:
            try:
                tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                tracker.reload()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to reload hooks after unfollow: {e}")

        return {
            "leader_user_id": leader["user_id"],
            "leader_username": leader["username"],
            "leader_address": leader["eth_address"],
            "following": False,
        }

    @app.get(
        "/copy-trading/following",
        tags=["copy-trading"],
        summary="List leaders the current user is following",
    )
    async def list_following(current=Depends(_get_current_session)):
        """
        Get list of all leaders you are currently following.

        Returns active hooks (enabled=1) with leader wallet addresses.

        Example:
        ```bash
        curl http://localhost:8000/copy-trading/following \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {
          "following": [
            {
              "hook_id": 1,
              "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
              "display_name": "0xff2a...cde0",
              "enabled": true,
              "config": {"mode": "beginner", "fixed_usd_amount": 1.0}
            }
          ],
          "following_count": 1
        }
        ```
        """
        rows = db.execute(
            """
            SELECT
                h.id AS hook_id,
                h.follower_user_id,
                h.leader_address,
                h.config,
                h.enabled
            FROM copy_hooks h
            WHERE h.follower_user_id = ? AND h.enabled = 1;
            """,
            (current["user_id"],),
        ).fetchall()

        leaders: list[Dict[str, Any]] = []
        for r in rows:
            row_dict = dict(r)
            config = json.loads(row_dict.get("config") or "{}")

            # Get leader info from config or use the address directly
            leader_address = row_dict.get("leader_address") or config.get("leader_address")
            if leader_address:
                leaders.append(
                    {
                        "hook_id": int(row_dict["hook_id"]),
                        "leader_address": leader_address,
                        "display_name": config.get("display_name") or leader_address[:10] + "...",
                        "enabled": bool(row_dict.get("enabled") or 1),
                        "config": config,
                    }
                )

        return {"following": leaders, "following_count": len(leaders)}

    @app.get(
        "/copy-trading/hooks",
        tags=["copy-trading"],
        summary="List copy-trading hooks",
    )
    async def list_copy_hooks(current=Depends(_get_current_session)):
        """
        Get all your copy-trading hooks (enabled and disabled).

        Each hook represents a follow relationship that triggers copy trades.

        Example:
        ```bash
        curl http://localhost:8000/copy-trading/hooks \\
             -H "Authorization: Bearer YOUR_TOKEN"
        ```

        Response:
        ```json
        {
          "hooks": [
            {
              "hook_id": 1,
              "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
              "display_name": "0xff2a...cde0",
              "enabled": true,
              "created_at": 1773412919,
              "config": {"mode": "beginner", "fixed_usd_amount": 1.0}
            }
          ],
          "hooks_active": true
        }
        ```
        """
        user = db.get_user(current["user_id"])
        rows = db.execute(
            """
            SELECT
                h.id AS hook_id,
                h.follower_user_id,
                h.leader_address,
                h.created_at,
                h.config,
                h.enabled
            FROM copy_hooks h
            WHERE h.follower_user_id = ?;
            """,
            (current["user_id"],),
        ).fetchall()

        hooks: list[Dict[str, Any]] = []
        for r in rows:
            r = dict(r)
            try:
                r["config"] = json.loads(r.get("config") or "{}")
            except Exception:
                r["config"] = {}

            leader_address = r.get("leader_address") or r["config"].get("leader_address")
            hooks.append(
                {
                    "hook_id": r["hook_id"],
                    "leader_address": leader_address,
                    "display_name": r["config"].get("display_name") or (leader_address[:10] + "..." if leader_address else ""),
                    "created_at": r["created_at"],
                    "config": r["config"],
                    "enabled": bool(r.get("enabled") or 1),
                }
            )

        return {
            "hooks": hooks,
            "hooks_active": bool(user.get("copy_trading_enabled") or 0),
        }

    @app.get(
        "/auth/telegram-widget",
        tags=["auth"],
        summary="Telegram Login Widget callback (returns JWT)",
    )
    async def auth_telegram_widget(request: Request):
        """
        Callback for the classic Telegram Login Widget used on /test/login.
        Verifies the signature, creates/loads the user + session, and returns
        a simple HTML page showing the issued JWT.
        """
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise HTTPException(
                status_code=500, detail="BOT_TOKEN not configured on server."
            )

        params = dict(request.query_params)
        user_obj = _verify_telegram_login_widget(params, bot_token)
        tg_user_id = user_obj.get("id")
        if tg_user_id is None:
            raise HTTPException(
                status_code=400, detail="Telegram user id missing in login data."
            )

        # Ensure user exists; if not, create with a fresh wallet (unencrypted private key for now).
        db_user = db.get_user(tg_user_id)
        if not db_user:
            eth_wallet = wallets.generate_eth_wallet()
            username = user_obj.get("username") or ""
            db.create_user(
                user_id=tg_user_id,
                username=username,
                eth_data=eth_wallet,
                sol_data=("", ""),
            )

        session_id = db.create_session(tg_user_id)

        jwt_secret = os.getenv("JWT_SECRET")
        if not jwt_secret:
            raise HTTPException(
                status_code=500, detail="JWT_SECRET not configured on server."
            )

        ttl_seconds = int(os.getenv("JWT_TTL_SECONDS", "604800"))  # default 7 days
        now = int(time.time())
        payload = {
            "sub": str(tg_user_id),
            "tg_user_id": tg_user_id,
            "session_id": session_id,
            "iat": now,
            "exp": now + ttl_seconds,
        }

        token = jwt.encode(payload, jwt_secret, algorithm="HS256")

        # Return JSON so frontend can read token via fetch/XHR.
        return {"token": token}

    @app.get("/me", tags=["user"], summary="Get current user profile and wallet")
    async def get_me(current=Depends(_get_current_session)):
        """
        Return basic profile info and wallet address for the current logged-in user.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "eth_address": user["eth_address"],
            "copy_trading_enabled": bool(user.get("copy_trading_enabled") or 0),
        }

    @app.get(
        "/me/status",
        tags=["user"],
        summary="Get current user status flags",
    )
    async def get_me_status(current=Depends(_get_current_session)):
        """
        Return status flags for the current user: is_copytrading, polymarket_approved, etc.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        return {
            "is_copytrading": bool(user.get("copy_trading_enabled") or 0),
            "polymarket_approved": bool(user.get("polymarket_approved") or 0),
        }

    @app.get(
        "/me/wallet/address",
        tags=["user"],
        summary="Get current user's EVM wallet address",
    )
    async def get_wallet_address(current=Depends(_get_current_session)):
        """
        Return the public EVM/Polygon wallet address for the current user.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        return {"eth_address": user["eth_address"]}

    @app.get(
        "/me/wallet/private-key",
        tags=["user"],
        summary="Get current user's raw private key (unsafe, dev only)",
    )
    async def get_wallet_private_key(current=Depends(_get_current_session)):
        """
        DEV/INTERNAL: Return the raw private key for the current user's wallet.
        WARNING: Exposing this in production is extremely unsafe.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        return {"eth_private_key": user["eth_private_key"]}

    @app.get(
        "/me/balance",
        tags=["user"],
        summary="Get current user's Polygon wallet token balances and total USD value",
    )
    async def get_balance(current=Depends(_get_current_session)):
        """
        Return a JSON summary of the current user's Polygon wallet balances:
          - list of tokens with symbol, balance, and usd_value
          - total_usd across all tracked tokens
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        # Use trading wallet (Safe when available) for balances.
        from bot_tools import get_trading_wallet_address

        trading_addr = get_trading_wallet_address(user["eth_address"])

        data = await asyncio.to_thread(
            _get_balance_json_cached, trading_addr
        )
        data["wallet"] = trading_addr
        return data

    @app.get(
        "/me/portfolio",
        tags=["portfolio"],
        summary="Get full portfolio (positions, PnL, orders) for current user",
    )
    async def get_portfolio(current=Depends(_get_current_session)):
        """
        Return a structured JSON summary of the user's Polymarket portfolio:
          - on-chain funds (as text summary)
          - open positions with numeric fields
          - aggregate portfolio value and total PnL
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        from bot_tools import get_trading_wallet_address

        # Use trading wallet (Safe when available) for portfolio, positions and trades.
        address = get_trading_wallet_address(user["eth_address"])

        # Fetch balance, positions, closed positions, and PM trades in parallel (single balance fetch)
        balance_json, positions_raw, closed_raw, pm_trades = await asyncio.gather(
            asyncio.to_thread(_get_balance_json_cached, address),
            asyncio.to_thread(_fetch_positions_sync, address),
            asyncio.to_thread(_fetch_closed_positions_sync, address),
            asyncio.to_thread(_fetch_pm_trades_sync, address),
        )
        on_chain_summary = _format_balance_json_as_summary(balance_json or {})
        portfolio = _build_portfolio_from_fetched(
            address,
            on_chain_summary,
            balance_json or {},
            positions_raw or [],
            closed_raw or [],
        )

        def _has_valid_tx(t: Dict[str, Any]) -> bool:
            h = (t.get("transactionHash") or t.get("txHash") or t.get("transaction_hash") or "").strip()
            return bool(h and h.lower() != "pending")

        pm_trades = [t for t in pm_trades if _has_valid_tx(t)]

        # Attach to each position the orders (trades) that match by condition_id
        for pos in portfolio["positions"]:
            cid = pos.get("condition_id") or ""
            pos["orders"] = [t for t in pm_trades if (t.get("conditionId") or t.get("condition_id") or "") == cid]

        # Internal DB orders (from our app) + Polymarket on-chain trades as top-level "orders"
        rows = db.execute(
            """
            SELECT
                id,
                market_id,
                condition_id,
                side,
                amount,
                size,
                price,
                order_side,
                status,
                order_id,
                tx_hash,
                executed_at,
                copied_from_user_id
            FROM trades
            WHERE user_id = ?
              AND tx_hash IS NOT NULL
              AND TRIM(COALESCE(tx_hash, '')) != ''
              AND LOWER(TRIM(tx_hash)) != 'pending'
            ORDER BY executed_at DESC
            LIMIT 100;
            """,
            (user["user_id"],),
        ).fetchall()

        internal_orders: list[Dict[str, Any]] = []
        for r in rows:
            r = dict(r)
            internal_orders.append(
                {
                    "id": r["id"],
                    "condition_id": r["condition_id"],
                    "side": r["side"],
                    "amount": (r.get("size") * r.get("price")) if (r.get("size") is not None and r.get("price") is not None) else r["amount"],
                    "shares": r.get("size"),
                    "size": r.get("size"),
                    "price": r.get("price"),
                    "order_side": r.get("order_side"),
                    "order_type": "close" if (r.get("order_side") or "").upper() == "SELL" else "open",
                    "status": r["status"],
                    "order_id": r["order_id"],
                    "tx_hash": r["tx_hash"],
                    "executed_at": r["executed_at"],
                    "copied_from_user_id": r["copied_from_user_id"],
                    "source": "app",
                }
            )

        # Top-level orders: Polymarket on-chain trades (with source) plus internal
        portfolio["orders"] = [{"source": "polymarket", **t} for t in pm_trades] + internal_orders
        return portfolio

    @app.get(
        "/users/{address}/portfolio",
        tags=["portfolio"],
        summary="Get public portfolio for a user by wallet address",
    )
    async def get_user_portfolio(address: str):
        """
        Public endpoint: analyze any user's portfolio by **wallet address**.
        Returns the same structured JSON as /me/portfolio.
        """
        address = address.strip()
        if not address:
            raise HTTPException(status_code=400, detail="Address is required.")

        # Fetch in parallel (same pattern as /me/portfolio)
        balance_json, positions_raw, closed_raw, pm_trades_user = await asyncio.gather(
            asyncio.to_thread(_get_balance_json_cached, address),
            asyncio.to_thread(_fetch_positions_sync, address),
            asyncio.to_thread(_fetch_closed_positions_sync, address),
            asyncio.to_thread(_fetch_pm_trades_sync, address),
        )
        on_chain_summary = _format_balance_json_as_summary(balance_json or {})
        portfolio = _build_portfolio_from_fetched(
            address,
            on_chain_summary,
            balance_json or {},
            positions_raw or [],
            closed_raw or [],
        )

        def _has_valid_tx(t: Dict[str, Any]) -> bool:
            h = (t.get("transactionHash") or t.get("txHash") or t.get("transaction_hash") or "").strip()
            return bool(h and h.lower() != "pending")

        pm_trades_user = [t for t in pm_trades_user if _has_valid_tx(t)]

        for pos in portfolio["positions"]:
            cid = pos.get("condition_id") or ""
            pos["orders"] = [t for t in pm_trades_user if (t.get("conditionId") or t.get("condition_id") or "") == cid]
            for o in pos["orders"]:
                o.pop("conditionId", None)
                o.pop("condition_id", None)

        # Public endpoint: no DB lookup—data comes only from Polymarket Data API
        def _no_cid(d: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in d.items() if k not in ("condition_id", "conditionId")}

        portfolio["orders"] = [{"source": "polymarket", **_no_cid(t)} for t in pm_trades_user]
        return portfolio

    @app.get(
        "/users/{address}/profile",
        tags=["users"],
        summary="Get public Polymarket profile by wallet address",
    )
    async def get_user_profile(address: str):
        """
        Public endpoint: fetch Polymarket public profile by wallet address.
        Proxies to Gamma API: https://gamma-api.polymarket.com/public-profile
        Returns profile info (name, bio, image, verifiedBadge, etc.) or 404 if not found.
        """
        addr = address.strip()
        if not addr:
            raise HTTPException(status_code=400, detail="Address is required.")
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
            raise HTTPException(status_code=400, detail="Invalid address format.")

        profile = await asyncio.to_thread(_fetch_public_profile_sync, addr)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found.")
        return profile

    @app.post(
        "/me/claim-winnings",
        tags=["portfolio"],
        summary="Claim winnings from resolved Polymarket markets via gasless relayer",
    )
    async def claim_winnings(current=Depends(_get_current_session)):
        """
        Trigger a gasless redemption of winnings from resolved Polymarket markets
        for the current user, using the same helper as the Telegram bot.

        This calls bot_tools.claim_polymarket_winnings(address) under the hood
        and returns a short human-readable status string.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        address = user.get("eth_address")
        if not address:
            raise HTTPException(status_code=400, detail="User has no Polygon wallet.")

        result_text = await asyncio.to_thread(
            bot_tools.claim_polymarket_winnings,
            address,
        )
        return {
            "wallet": address,
            "result": strip_emoji(result_text),
        }

    @app.post(
        "/me/approve",
        tags=["portfolio"],
        summary="Run gasless USDC/CTF approvals for Polymarket trading",
    )
    async def approve_trading(current=Depends(_get_current_session)):
        """
        Manually trigger the full gasless approval flow for the current user.

        This deploys the Safe wallet (if needed) and submits all USDC.e + CTF
        allowance transactions through the Polymarket Builder relayer.
        No gas or on-chain balance is required from the user.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        address = user.get("eth_address")
        if not address:
            raise HTTPException(status_code=400, detail="User has no Polygon wallet.")

        result_text = await asyncio.to_thread(
            bot_tools.approve_usdc_for_trading,
            address,
        )
        success = not str(result_text).lstrip().startswith("❌")
        if success:
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                        (current["user_id"],),
                    )
            except Exception:
                pass

        return {
            "wallet": address,
            "approved": success,
            "result": strip_emoji(result_text),
        }

    @app.post(
        "/bridge/deposit",
        tags=["bridge"],
        summary="Get Polymarket bridge deposit addresses for current user",
    )
    async def bridge_deposit(current=Depends(_get_current_session)):
        """
        Return chain-specific Polymarket bridge deposit addresses for the current
        user. Uses the user's Safe (proxy) address as the Polymarket wallet when
        available; falls back to EOA. The Safe address is computed and persisted
        in the database for future use.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        address = user.get("eth_address")
        if not address:
            raise HTTPException(status_code=400, detail="User has no Polygon wallet.")

        polymarket_wallet = await asyncio.to_thread(
            bot_tools.get_safe_address_for_user,
            address,
        )
        if not polymarket_wallet:
            polymarket_wallet = address

        try:
            deposit_resp = await asyncio.to_thread(
                lambda: requests.post(
                    "https://bridge.polymarket.com/deposit",
                    json={"address": polymarket_wallet},
                    timeout=10,
                )
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach Polymarket Bridge API: {e}",
            )

        # Accept any 2xx as success; the Bridge API may return 201/202.
        if deposit_resp.status_code // 100 != 2:
            raise HTTPException(
                status_code=deposit_resp.status_code,
                detail=f"Bridge API error: {deposit_resp.text[:200]}",
            )

        try:
            deposit_data = deposit_resp.json()
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to decode Bridge API response: {e}",
            )

        # Bridge API returns an object with an `address` field containing
        # per-chain deposit addresses (evm, svm, tron, btc, ...).
        bridge_addresses = deposit_data.get("address", deposit_data)

        # Use the same static supported-assets mapping as /bridge/supported-assets
        # to build a compact per-chain view: { chainName, tokens: [symbols] }.
        static_supported = await bridge_supported_assets()
        bridge_options = static_supported.get("supportedAssets", [])

        return {
            "polymarket_wallet": polymarket_wallet,
            "bridge_addresses": bridge_addresses,
            "bridge_options": bridge_options,
        }

    @app.get(
        "/bridge/supported-assets",
        tags=["bridge"],
        summary="List assets supported by the Polymarket Bridge API",
    )
    async def bridge_supported_assets():
        """
        Return a static, curated list of assets supported by the Polymarket
        Bridge, focusing on the most relevant stablecoins and majors per chain.

        The shape is:

        {
          "supportedAssets": [
            { "chainName": "Ethereum", "tokens": ["USDC", "USDT", "DAI", "ETH"] },
            { "chainName": "Polygon",  "tokens": ["USDC", "USDT", "DAI"] },
            { "chainName": "Solana",   "tokens": ["USDC", "USDT", "SOL"] },
            { "chainName": "Arbitrum", "tokens": ["USDC", "USDT", "DAI", "ETH"] },
            { "chainName": "Optimism", "tokens": ["USDC", "USDT", "DAI", "ETH"] },
            { "chainName": "Base",     "tokens": ["USDC", "USDT", "DAI", "ETH"] },
            { "chainName": "BNB Smart Chain", "tokens": ["USDC", "USDT", "DAI", "BNB"] },
            { "chainName": "Bitcoin",  "tokens": ["BTC"] },
            { "chainName": "Tron",     "tokens": ["USDT"] }
          ]
        }
        """
        return {
            "supportedAssets": [
                {
                    "chainName": "Ethereum",
                    "tokens": ["USDC", "USDT", "DAI", "ETH"],
                },
                {
                    "chainName": "Polygon",
                    "tokens": ["USDC", "USDT", "DAI"],
                },
                {
                    "chainName": "Solana",
                    "tokens": ["USDC", "USDT", "SOL"],
                },
                {
                    "chainName": "Arbitrum",
                    "tokens": ["USDC", "USDT", "DAI", "ETH"],
                },
                {
                    "chainName": "Optimism",
                    "tokens": ["USDC", "USDT", "DAI", "ETH"],
                },
                {
                    "chainName": "Base",
                    "tokens": ["USDC", "USDT", "DAI", "ETH"],
                },
                {
                    "chainName": "BNB Smart Chain",
                    "tokens": ["USDC", "USDT", "DAI", "BNB"],
                },
                {
                    "chainName": "Bitcoin",
                    "tokens": ["BTC"],
                },
                {
                    "chainName": "Tron",
                    "tokens": ["USDT"],
                },
            ]
        }

    def _serialize_cached_markets() -> list[Dict[str, Any]]:
        markets_json: list[Dict[str, Any]] = []
        for m in market_cache.list_all():
            markets_json.append(
                {
                    "condition_id": m.condition_id,
                    "question": m.question,
                    "event_title": m.event_title,
                    "outcomes": m.outcomes,
                    "token_ids": m.clob_token_ids,
                    "odds_cents": m.odds,
                    "end_date": m.end_date,
                }
            )
        return markets_json

    @app.get(
        "/markets/trending",
        tags=["markets"],
        summary="List trending Polymarket markets",
    )
    async def get_trending_markets():
        """
        Public endpoint: return a JSON list of trending Polymarket markets.
        Also refreshes the shared market cache used for trading.
        """
        # This call populates market_cache via bot_tools' shared logic
        _ = bot_tools.get_polymarket_markets()
        return {"markets": _serialize_cached_markets()}

    @app.get(
        "/hooklogs",
        tags=["copy-trading"],
        summary="Get hook execution history",
    )
    async def get_hook_logs(
        follower_user_id: int = None,
        hook_id: int = None,
        limit: int = 100,
        status: str = None
    ):
        """
        Get hook execution history for copy trading.

        Returns all executed, skipped, or failed hook executions with details.

        Query Parameters:
        - follower_user_id: Filter logs by specific follower user ID
        - hook_id: Filter logs by specific hook ID
        - status: Filter by status (pending, success, failed, skipped)
        - limit: Maximum number of logs to return (default 100)

        Example:
        ```bash
        curl "http://localhost:8000/hooklogs?limit=10&status=success"
        ```

        Response:
        ```json
        {
          "count": 10,
          "logs": [
            {
              "id": 1,
              "hook_id": 1,
              "follower_user_id": 12345,
              "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
              "trade_side": "BUY",
              "trade_outcome": "Yes",
              "trade_amount": 10.50,
              "trade_price": 0.52,
              "trade_size": 20.19,
              "follower_amount": 10.50,
              "condition_id": "0x9c414...",
              "status": "success",
              "error": null,
              "executed_at": 1773412919,
              "executed_at_iso": "2026-03-13T14:41:59"
            }
          ]
        }
        ```
        """
        if not COPY_TRACKER_AVAILABLE:
            return {"error": "Copy tracker not available"}

        try:
            tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
            logs = tracker.get_logs(
                follower_user_id=follower_user_id,
                hook_id=hook_id,
                limit=limit
            )

            # Filter by status if provided
            if status:
                logs = [l for l in logs if l.get("status") == status]

            # Add human-readable timestamps
            for log in logs:
                ts = log.get("executed_at")
                if ts:
                    log["executed_at_iso"] = datetime.fromtimestamp(ts).isoformat()

            return {
                "count": len(logs),
                "logs": logs
            }
        except Exception as e:
            return {"error": str(e)}

    @app.post(
        "/admin/copy-trading/global-tick",
        tags=["copy-trading"],
        summary="Run global copy-trading indexer tick (admin / cron use)",
        deprecated=True,
    )
    async def admin_run_global_copy_trading_tick(
        limit_per_leader: int = 50,
    ):
        """
        Run a single tick of the global copy-trading indexer.

        This endpoint processes trades for all enabled global hooks and executes
        copy trades for followers. Intended for cron/scheduler use.

        Note: The WebSocket-based tracker (copy_trading.py) handles this automatically
        in real-time. This endpoint is for polling-based fallback.

        Parameters:
        - limit_per_leader: Maximum trades to process per leader (1-200, default 50)

        Example:
        ```bash
        curl -X POST http://localhost:8000/admin/copy-trading/global-tick?limit_per_leader=50
        ```

        Response:
        ```json
        {
          "processed_hooks": 5,
          "mirrored_trades": 12
        }
        ```
        """
        if limit_per_leader < 1:
            limit_per_leader = 1
        if limit_per_leader > 200:
            limit_per_leader = 200
        result = await _run_global_copy_trading_tick(limit_per_leader=limit_per_leader)
        return result

    @app.post(
        "/admin/copy-trading/test-hook",
        tags=["copy-trading"],
        summary="Test a specific copy trading hook (admin / debugging)",
    )
    async def admin_test_copy_hook(
        hook_id: int,
        limit_per_leader: int = 10,
    ):
        """
        Test a specific copy trading hook.

        Fetches recent trades for the hook's leader and returns them for inspection.
        Does NOT execute any trades - useful for debugging hook configuration.

        Parameters:
        - hook_id: ID of the hook to test
        - limit_per_leader: Maximum trades to fetch (default 10)

        Example:
        ```bash
        curl -X POST http://localhost:8000/admin/copy-trading/test-hook?hook_id=1
        ```

        Response:
        ```json
        {
          "success": true,
          "hook_id": 1,
          "leader_address": "0xff2a7b31ec0bdd06812ce22eb10364736a36cde0",
          "trades_found": 5,
          "trades": [...]
        }
        ```
        """
        import json as _json

        row = db.execute(
            "SELECT id, follower_user_id, leader_address, config, enabled FROM copy_hooks WHERE id = ?;",
            (hook_id,),
        ).fetchone()

        if not row:
            return {"error": "Hook not found", "hook_id": hook_id}

        d = dict(row)
        cfg = _json.loads(d.get("config") or "{}")
        leader_address = (d.get("leader_address") or cfg.get("leader_address") or "").strip().lower()

        if not leader_address:
            return {"error": "Hook has no leader_address in config", "hook_id": hook_id}

        if not d.get("enabled"):
            return {"error": "Hook is not enabled", "hook_id": hook_id, "hint": "Enable copy trading for this user"}

        try:
            trades_raw = await asyncio.to_thread(
                _fetch_url_sync,
                "https://data-api.polymarket.com/trades",
                {"user": leader_address, "takerOnly": "true", "limit": limit_per_leader},
                12,
            )
        except Exception as e:
            return {"error": "Failed to fetch trades", "hook_id": hook_id, "detail": str(e)}

        if not isinstance(trades_raw, list):
            return {"error": "Invalid trades response", "hook_id": hook_id}

        return {
            "success": True,
            "hook_id": hook_id,
            "leader_address": leader_address,
            "trades_found": len(trades_raw),
            "trades": trades_raw[:5],  # Return first 5 trades for inspection
        }

    @app.post(
        "/admin/copy-trading/stop-loss-tick",
        tags=["copy-trading"],
        summary="Run stop-loss tick for followers (admin / cron use)",
    )
    async def admin_run_stop_loss_tick(
        default_max_loss_pct: float = 15.0,
    ):
        """
        Run a stop-loss check across all followers with copy-trading hooks.

        For each follower:
        1. Determines effective max_loss_pct from their hook configs
        2. Checks Polymarket positions for losses
        3. Auto-closes positions that exceed the loss threshold

        Parameters:
        - default_max_loss_pct: Default stop-loss threshold % (default 15.0)

        Example:
        ```bash
        curl -X POST http://localhost:8000/admin/copy-trading/stop-loss-tick?default_max_loss_pct=10
        ```

        Response:
        ```json
        {
          "processed_followers": 5,
          "positions_checked": 12,
          "positions_closed": 2,
          "total_loss_prevented": 25.50
        }
        ```
        """
        if default_max_loss_pct <= 0:
            default_max_loss_pct = 15.0

        async def _run_tick() -> Dict[str, Any]:
            import json as _json

            rows = db.execute(
                """
                SELECT follower_user_id, config
                FROM copy_hooks
                WHERE enabled = 1;
                """
            ).fetchall()

            # Determine per-follower stop-loss threshold
            follower_thresholds: Dict[int, float] = {}
            for r in rows:
                d = dict(r)
                try:
                    cfg = _json.loads(d.get("config") or "{}")
                except Exception:
                    cfg = {}
                uid = int(d["follower_user_id"])
                val = cfg.get("max_loss_pct")
                try:
                    loss_pct = float(val) if val is not None else None
                except Exception:
                    loss_pct = None
                if loss_pct is None or loss_pct <= 0:
                    continue
                cur = follower_thresholds.get(uid)
                follower_thresholds[uid] = min(cur, loss_pct) if cur is not None else loss_pct

            closed_trades = 0

            for follower_id, thresh in follower_thresholds.items():
                max_loss = thresh if thresh > 0 else default_max_loss_pct
                user = db.get_user(follower_id)
                if not user:
                    continue
                addr = user.get("eth_address") or ""
                if not addr:
                    continue

                # Fetch current positions from Polymarket Data API
                try:
                    positions = await asyncio.to_thread(_fetch_positions_sync, addr)
                except Exception:
                    continue
                if not isinstance(positions, list):
                    continue

                for p in positions:
                    if not isinstance(p, dict):
                        continue
                    try:
                        pnl_pct = float(
                            p.get("percentPnl")
                            or p.get("pnlPercent")
                            or p.get("pnl_pct")
                            or 0.0
                        )
                    except Exception:
                        pnl_pct = 0.0
                    if pnl_pct >= -max_loss:
                        continue

                    cond_id = (p.get("conditionId") or p.get("condition_id") or "").strip()
                    outcome = (p.get("outcome") or "").strip().capitalize()
                    if not cond_id or not outcome:
                        continue

                    try:
                        cur_val = float(p.get("currentValue") or 0.0)
                    except Exception:
                        cur_val = 0.0
                    if cur_val <= 0:
                        size = float(p.get("size") or 0.0)
                        price = float(p.get("curPrice") or 0.0)
                        cur_val = size * price if size > 0 and price > 0 else 0.0
                    if cur_val <= 0:
                        continue

                    await asyncio.to_thread(
                        execute_trade_for_user,
                        db,
                        follower_id,
                        outcome,
                        cur_val,
                        cond_id,
                        "SELL",
                        copied_from_user_id=None,
                    )
                    closed_trades += 1

            return {
                "followers_with_stop_loss": len(follower_thresholds),
                "positions_closed": closed_trades,
            }

        return await _run_tick()

    @app.get(
        "/markets/search",
        tags=["markets"],
        summary="Search Polymarket markets by keyword",
    )
    async def search_markets(q: str = Query(..., description="Search query string")):
        """
        Public endpoint: search active Polymarket markets by keyword.
        """
        _ = bot_tools.search_polymarket_events(q)
        return {
            "query": q,
            "markets": _serialize_cached_markets(),
        }

    @app.get(
        "/markets/category/{category}",
        tags=["markets"],
        summary="List markets by category (Polymarket tag slug)",
    )
    async def markets_by_category(category: str):
        """
        Public endpoint: list active markets filtered by Polymarket category tag.
        Category uses tag_slug (e.g. finance, crypto, politics).
        """
        _ = bot_tools.get_polymarket_markets_by_category(category)
        return {
            "category": category,
            "markets": _serialize_cached_markets(),
        }

    @app.get(
        "/markets/tag/{tag_id}",
        tags=["markets"],
        summary="List markets by numeric Polymarket tag_id",
    )
    async def markets_by_tag(
        tag_id: int,
        include_related: bool = Query(
            False,
            description="If true, include markets from related tags as well.",
        ),
    ):
        """
        Public endpoint: list active markets filtered by a numeric Polymarket tag_id.

        This wraps Gamma's /events?tag_id=... (&related_tags=true) endpoint via
        bot_tools.get_polymarket_markets_by_tag and then returns the cached markets.
        """
        _ = bot_tools.get_polymarket_markets_by_tag(tag_id, include_related=include_related)
        return {
            "tag_id": tag_id,
            "include_related": include_related,
            "markets": _serialize_cached_markets(),
        }

    @app.get(
        "/data/trades",
        tags=["markets"],
        summary="Get Polymarket trades for a user or market (data API proxy)",
    )
    async def get_polymarket_data_trades(
        user: str | None = Query(None, description="User wallet address (0x...)"),
        market: str | None = Query(None, description="Condition ID(s), comma-separated"),
        event_id: str | None = Query(None, description="Event ID(s), comma-separated. Mutually exclusive with market."),
        limit: int = Query(100, ge=0, le=10000),
        offset: int = Query(0, ge=0, le=10000),
        taker_only: bool = Query(True, description="Only taker trades"),
        side: str | None = Query(None, description="BUY or SELL"),
        filter_type: str | None = Query(None, description="CASH or TOKENS (use with filter_amount)"),
        filter_amount: float | None = Query(None, ge=0, description="Min amount (use with filter_type)"),
    ):
        """
        Public endpoint: Polymarket Data API trades.

        Fetches historical/on-chain trades for a user or market(s).
        See: https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
        """
        url = "https://data-api.polymarket.com/trades"
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "takerOnly": str(taker_only).lower(),
        }
        if user:
            params["user"] = user
        if market:
            params["market"] = market
        if event_id:
            params["eventId"] = event_id
        if side and side.upper() in ("BUY", "SELL"):
            params["side"] = side.upper()
        if filter_type and filter_type.upper() in ("CASH", "TOKENS") and filter_amount is not None:
            params["filterType"] = filter_type.upper()
            params["filterAmount"] = filter_amount

        try:
            data = await asyncio.to_thread(
                _fetch_url_sync, url, params, 12
            )
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Polymarket Data API error: {e}")

        def _get_tx_hash(t: Dict[str, Any]) -> str | None:
            h = (t.get("transactionHash") or t.get("txHash") or t.get("transaction_hash") or "").strip()
            if h and h.lower() != "pending":
                return h
            return None

        def _with_tx(t: Dict[str, Any]) -> Dict[str, Any]:
            tx = _get_tx_hash(t)
            return {**t, "tx_hash": tx, "tx_id": tx}

        # Add tx_hash and tx_id to each trade; keep all trades (Polymarket may omit tx sometimes)
        if isinstance(data, list):
            data = [_with_tx(t) for t in data if isinstance(t, dict)]
        elif isinstance(data, dict):
            inner = data.get("trades") or data.get("data")
            if isinstance(inner, list):
                key = "trades" if "trades" in data else "data"
                data = {**data, key: [_with_tx(t) for t in inner if isinstance(t, dict)]}

        return {"trades": data}

    @app.get(
        "/trades",
        tags=["social"],
        summary="Global trade feed across all users",
    )
    async def get_trades(limit: int = 50, offset: int = 0):
        """
        Public endpoint: trade feed combining local (app-recorded) trades and
        Polymarket on-chain trades for registered users. Ensures app activity
        is always visible.
        """
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        if offset < 0:
            offset = 0

        # 1. Local feed: app-recorded trades with valid tx_hash
        local_rows = db.execute(
            """
            SELECT
                t.id, t.user_id, u.username, u.eth_address, t.condition_id,
                t.side, t.amount, t.size, t.price, t.order_side, t.status, t.order_id, t.tx_hash, t.executed_at
            FROM trades t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.tx_hash IS NOT NULL
              AND TRIM(COALESCE(t.tx_hash, '')) != ''
              AND LOWER(TRIM(t.tx_hash)) != 'pending'
            ORDER BY t.executed_at DESC
            LIMIT ?;
            """,
            (limit + offset,),
        ).fetchall()

        local_trades: list[Dict[str, Any]] = []
        for r in local_rows:
            r = dict(r)
            local_trades.append(
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "eth_address": r["eth_address"],
                    "condition_id": r["condition_id"],
                    "side": r["side"],
                    "amount": (r.get("size") * r.get("price")) if (r.get("size") is not None and r.get("price") is not None) else r["amount"],
                    "shares": r.get("size"),
                    "size": r.get("size"),
                    "price": r.get("price"),
                    "order_side": r.get("order_side"),
                    "order_type": "close" if (r.get("order_side") or "").upper() == "SELL" else "open",
                    "status": r["status"],
                    "order_id": r["order_id"],
                    "tx_hash": r["tx_hash"],
                    "tx_id": r["tx_hash"],
                    "executed_at": r["executed_at"],
                    "source": "local",
                }
            )

        # 2. Polymarket Data API trades for registered users (run in thread to avoid blocking)
        try:
            trades_raw = await asyncio.to_thread(
                _fetch_pm_trades_global_sync, limit, offset
            )
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Polymarket Data API error: {e}")

        # Restrict to trades where the user wallet is one of our registered users.
        # Build a set of known lowercased addresses from the users table.
        addr_rows = db.execute(
            """
            SELECT user_id, username, eth_address
            FROM users
            WHERE eth_address IS NOT NULL AND TRIM(eth_address) != '';
            """
        ).fetchall()
        registered_addrs: Dict[str, Dict[str, Any]] = {}
        for r in addr_rows:
            addr = str(r["eth_address"]).strip().lower()
            registered_addrs[addr] = {
                "user_id": r["user_id"],
                "username": r["username"],
                "eth_address": r["eth_address"],
            }

        filtered: list[Dict[str, Any]] = []
        if isinstance(trades_raw, list):
            def _get_tx_hash(t: Dict[str, Any]) -> str | None:
                h = (t.get("transactionHash") or t.get("txHash") or t.get("transaction_hash") or "").strip()
                if h and h.lower() != "pending":
                    return h
                return None

            for t in trades_raw:
                if not isinstance(t, dict):
                    continue
                # Polymarket trades objects have `user`, `proxyWallet`, `maker`, or `taker` as wallet
                addr = (
                    t.get("user")
                    or t.get("proxyWallet")
                    or t.get("maker")
                    or t.get("taker")
                    or ""
                )
                key = str(addr).strip().lower()
                info = registered_addrs.get(key)
                if info:
                    tx = _get_tx_hash(t)
                    size_val = t.get("size")
                    pm_side = (t.get("side") or "").upper()
                    enriched = {
                        **t,
                        "user_id": info["user_id"],
                        "username": info["username"],
                        "eth_address": info["eth_address"],
                        "tx_hash": tx,
                        "tx_id": tx,
                        "shares": size_val,
                        "amount": (size_val * (t.get("price") or 0)) if size_val else t.get("amount"),
                        "order_side": pm_side or None,
                        "order_type": "close" if pm_side == "SELL" else "open",
                        "source": "polymarket",
                    }
                    filtered.append(enriched)
        else:
            filtered = [trades_raw] if not isinstance(trades_raw, list) else []

        # Merge local + Polymarket; dedupe by tx_hash; sort by time desc; apply limit/offset
        seen_tx: set[str] = {t.get("tx_hash") for t in local_trades if t.get("tx_hash")}
        for t in filtered:
            if isinstance(t, dict):
                tx = t.get("tx_hash")
                if tx and tx not in seen_tx:
                    seen_tx.add(tx)
                    t.setdefault("source", "polymarket")
                    local_trades.append(t)
        merged = sorted(
            local_trades,
            key=lambda x: x.get("executed_at") or x.get("timestamp") or 0,
            reverse=True,
        )
        page = merged[offset : offset + limit]

        return {"trades": page, "limit": limit, "offset": offset}

    class PostTradeRequest(BaseModel):
        condition_id: str
        side: str  # Yes | No
        amount: float  # cost in USD
        order_id: str
        tx_hash: str | None = None
        tx_id: str | None = None
        size: float | None = None  # shares
        price: float | None = None  # per share
        order_side: str | None = None  # BUY (open) | SELL (close)

    @app.post(
        "/trades",
        tags=["social"],
        summary="Record a matched trade in the feed",
    )
    async def post_trade(body: PostTradeRequest, current=Depends(_get_current_session)):
        """
        Add a trade to the on-server feed. Requires status=matched and valid tx_hash.
        Use after a successful close/trade to ensure the trade appears in the feed.
        Dedupes by tx_hash (skips if already recorded).
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        tx = (body.tx_hash or body.tx_id or "").strip()
        if not tx or tx.lower() == "pending":
            raise HTTPException(
                status_code=400,
                detail="tx_hash or tx_id required and must not be empty or 'pending'.",
            )

        db.record_trade(
            user_id=user["user_id"],
            market_id=0,
            side=(body.side or "Yes").strip(),
            amount=float(body.amount),
            status="matched",
            order_id=(body.order_id or "").strip(),
            tx_hash=tx,
            executed_at=int(time.time()),
            copied_from_user_id=None,
            condition_id=(body.condition_id or "").strip(),
            size=body.size,
            price=body.price,
            order_side=body.order_side,
        )

        return {
            "success": True,
            "tx_hash": tx,
            "tx_id": tx,
            "message": "Trade recorded in feed.",
        }

    @app.get(
        "/social/feed",
        tags=["social"],
        summary="Local trade feed (app-recorded trades)",
    )
    async def social_feed(limit: int = 50, offset: int = 0):
        """
        Local trade feed: all app-recorded trades with a valid on-chain tx_hash.
        Every successful trade is included. Use for leader discovery and copy-trading UI.
        """
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        if offset < 0:
            offset = 0

        rows = db.execute(
            """
            SELECT
                t.id,
                t.user_id,
                u.username,
                u.eth_address,
                t.condition_id,
                t.side,
                t.amount,
                t.size,
                t.price,
                t.order_side,
                t.status,
                t.tx_hash,
                t.executed_at
            FROM trades t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.tx_hash IS NOT NULL
              AND TRIM(COALESCE(t.tx_hash, '')) != ''
              AND LOWER(TRIM(t.tx_hash)) != 'pending'
            ORDER BY t.executed_at DESC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        ).fetchall()

        items: list[Dict[str, Any]] = []
        for r in rows:
            r = dict(r)
            items.append(
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "eth_address": r["eth_address"],
                    "condition_id": r["condition_id"],
                    "side": r["side"],
                    "amount": (r.get("size") * r.get("price")) if (r.get("size") is not None and r.get("price") is not None) else r["amount"],
                    "shares": r.get("size"),
                    "size": r.get("size"),
                    "price": r.get("price"),
                    "order_side": r.get("order_side"),
                    "order_type": "close" if (r.get("order_side") or "").upper() == "SELL" else "open",
                    "status": r["status"],
                    "tx_hash": r["tx_hash"],
                    "tx_id": r["tx_hash"],
                    "executed_at": r["executed_at"],
                }
            )

        return {
            "feed": items,
            "source": "local",
            "limit": limit,
            "offset": offset,
        }

    @app.get(
        "/social/leaderboard",
        tags=["social"],
        summary="Top traders leaderboard (local app view)",
    )
    async def social_leaderboard(limit: int = 20):
        """
        Leaderboard of top traders based on app-recorded Polymarket trades.

        This is *local* to this application:
          - Only trades that have been recorded via /trades (with a valid on-chain tx_hash)
            are counted.
          - Metrics are aggregated per user across all markets:
              * trade_count  : number of recorded trades
              * total_volume : sum of notional traded in USD (open + close)
              * open_volume  : notional for BUY / open trades
              * close_volume : notional for SELL / close trades

        Use this for discovering active traders to follow / copy-trade.
        """
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        rows = db.execute(
            """
            SELECT
                u.user_id,
                u.username,
                u.eth_address,
                COUNT(*) AS trade_count,
                SUM(
                    CASE
                        WHEN t.size IS NOT NULL AND t.price IS NOT NULL
                            THEN t.size * t.price
                        ELSE t.amount
                    END
                ) AS total_volume,
                SUM(
                    CASE
                        WHEN UPPER(COALESCE(t.order_side, '')) = 'BUY'
                            THEN
                                CASE
                                    WHEN t.size IS NOT NULL AND t.price IS NOT NULL
                                        THEN t.size * t.price
                                    ELSE t.amount
                                END
                        ELSE 0
                    END
                ) AS open_volume,
                SUM(
                    CASE
                        WHEN UPPER(COALESCE(t.order_side, '')) = 'SELL'
                            THEN
                                CASE
                                    WHEN t.size IS NOT NULL AND t.price IS NOT NULL
                                        THEN t.size * t.price
                                    ELSE t.amount
                                END
                        ELSE 0
                    END
                ) AS close_volume,
                MIN(t.executed_at) AS first_trade_at,
                MAX(t.executed_at) AS last_trade_at
            FROM trades t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.tx_hash IS NOT NULL
              AND TRIM(COALESCE(t.tx_hash, '')) != ''
              AND LOWER(TRIM(t.tx_hash)) != 'pending'
            GROUP BY u.user_id, u.username, u.eth_address
            HAVING trade_count > 0
            ORDER BY total_volume DESC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()

        leaders: list[Dict[str, Any]] = []
        for r in rows:
            r = dict(r)
            leaders.append(
                {
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "eth_address": r["eth_address"],
                    "trade_count": int(r["trade_count"] or 0),
                    "total_volume": float(r["total_volume"] or 0.0),
                    "open_volume": float(r["open_volume"] or 0.0),
                    "close_volume": float(r["close_volume"] or 0.0),
                    "first_trade_at": r["first_trade_at"],
                    "last_trade_at": r["last_trade_at"],
                }
            )

        return {
            "leaders": leaders,
            "limit": limit,
            "source": "local_trades",
        }

    @app.get(
        "/social/leaderboard/pnl",
        tags=["social"],
        summary="Top traders leaderboard (global Polymarket Data API)",
    )
    async def social_leaderboard_pnl(
        limit: int = 25,
        category: str = "OVERALL",
        time_period: str = "DAY",
        order_by: str = "PNL",
        offset: int = 0,
    ):
        """
        Global trader leaderboard from Polymarket Data API (not restricted to
        local app users). This proxies:

            GET https://data-api.polymarket.com/v1/leaderboard

        with configurable query parameters:

          - category   : OVERALL, POLITICS, SPORTS, CRYPTO, CULTURE, WEATHER, etc. (default OVERALL)
          - timePeriod : DAY, WEEK, MONTH, ALL (default DAY)
          - orderBy    : PNL or VOL (default PNL)
          - limit      : 1-50 (default 25)
          - offset     : 0-1000 (default 0)

        and returns the raw leaderboard entries (rank, userName, proxyWallet,
        pnl, vol, etc.) along with the parameters used.
        """
        # Clamp parameters to documented bounds
        if limit < 1:
            limit = 1
        if limit > 50:
            limit = 50
        if offset < 0:
            offset = 0
        if offset > 1000:
            offset = 1000

        category = (category or "OVERALL").upper()
        time_period = (time_period or "DAY").upper()
        order_by = (order_by or "PNL").upper()

        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
        }

        url = "https://data-api.polymarket.com/v1/leaderboard"
        try:
            resp = await asyncio.to_thread(
                lambda: requests.get(url, params=params, timeout=10)
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach Polymarket leaderboard API: {e}",
            )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Polymarket leaderboard API error: {resp.text[:200]}",
            )

        try:
            data = resp.json()
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to decode Polymarket leaderboard response: {e}",
            )

        # The API returns an array of trader entries or an object with 'data'
        if isinstance(data, list):
            leaders = data
        else:
            leaders = data.get("data", [])
            if not isinstance(leaders, list):
                leaders = []

        return {
            "leaders": leaders,
            "params": params,
            "source": "polymarket_global_leaderboard",
        }

    def _parse_trade_result(raw: str) -> Dict[str, Any]:
        """
        Parse the human-readable trade result string from execute_trade_for_user
        into a structured dict we can use for status checks and copy trading.
        """
        raw = strip_emoji(raw or "")
        success = raw.startswith("✅") or "TRADE EXECUTED" in raw
        failure = "TRADE FAILED" in raw
        order_id: str | None = None
        status_str: str | None = None
        tx_hash: str | None = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("Order ID:"):
                order_id = line.split(":", 1)[1].strip()
            elif line.startswith("Status:"):
                status_str = line.split(":", 1)[1].strip()
            elif line.startswith("TX Hash:"):
                tx_hash = line.split(":", 1)[1].strip()
        return {
            "raw": raw,
            "success": bool(success and not failure),
            "failure": failure,
            "order_id": order_id,
            "status": status_str,
            "tx_hash": tx_hash,
        }

    async def _fire_copy_hooks(
        leader_user_id: int,
        condition_id: str,
        side: str,
        amount: float,
        order_side: str,
    ) -> None:
        """
        Fire copy-trading hooks: when a leader trades, all hooks for that leader
        execute the same trade for their followers.

        If a hook's config has fractional=True, the follower amount is computed
        as the same percentage of their USDC.e balance that the leader used.
        Otherwise, amount is scaled by size_multiplier.
        """
        hooks = db.get_hooks_for_leader(leader_user_id)
        if not hooks:
            return

        leader = db.get_user(leader_user_id)
        leader_address = (leader or {}).get("eth_address") or ""

        logger = logging.getLogger(__name__)
        logger.info(
            "Firing copy-trading hooks",
            extra={
                "event": "copy_trading_hooks_start",
                "leader_user_id": leader_user_id,
                "leader_address": leader_address,
                "condition_id": condition_id,
                "side": side,
                "amount": amount,
                "order_side": order_side,
                "hook_count": len(hooks),
            },
        )

        tasks = []
        for h in hooks:
            follower_id = int(h["follower_user_id"])
            cfg = h.get("config") or {}

            mode = (cfg.get("mode") or "").lower()
            amt = _compute_amount_for_hook_mode(
                mode=mode,
                leader_trade_amount_usd=amount,
                follower_user_id=follower_id,
                leader_address=leader_address,
                cfg=cfg,
            )
            if not amt:
                continue

            logger.info(
                "Copy-trading hook computed follower amount",
                extra={
                    "event": "copy_trading_hook_amount",
                    "leader_user_id": leader_user_id,
                    "follower_user_id": follower_id,
                    "condition_id": condition_id,
                    "side": side,
                    "amount_usd": amt,
                    "mode": mode,
                },
            )

            tasks.append(
                asyncio.to_thread(
                    execute_trade_for_user,
                    db,
                    follower_id,
                    side,
                    amt,
                    condition_id,
                    order_side,
                    copied_from_user_id=leader_user_id,
                )
            )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(
                "Finished firing copy-trading hooks",
                extra={
                    "event": "copy_trading_hooks_done",
                    "leader_user_id": leader_user_id,
                    "condition_id": condition_id,
                    "side": side,
                    "order_side": order_side,
                    "fired_hooks": len(tasks),
                },
            )

    async def _propagate_cancel_to_followers(
        leader_user_id: int,
        leader_order_id: str,
    ) -> None:
        """
        When a leader cancels an open order, attempt to cancel matching open
        orders for all followers:
          - We look up the leader's open order to get its token_id/side.
          - For each follower, we fetch their open orders and cancel those with
            the same token_id and side.

        This is best-effort and only affects orders placed via this app (since
        we rely on the CLOB API per-user).
        """
        hooks = db.get_hooks_for_leader(leader_user_id)
        if not hooks:
            return

        # Find the leader's order details so we can match by token_id/side.
        leader_orders = get_open_orders_for_user(db=db, user_id=leader_user_id) or {}
        leader_token_id = None
        leader_side = None

        try:
            orders = leader_orders.get("data") if isinstance(leader_orders, dict) else leader_orders
        except Exception:
            orders = leader_orders

        if isinstance(orders, list):
            for o in orders:
                if not isinstance(o, dict):
                    continue
                oid = o.get("id") or o.get("orderID") or o.get("order_id")
                if str(oid) != str(leader_order_id):
                    continue
                leader_token_id = (
                    o.get("token_id")
                    or o.get("tokenId")
                    or o.get("tokenID")
                )
                leader_side = (o.get("side") or o.get("direction") or "").upper()
                break

        if not leader_token_id:
            # Could not resolve the order details; nothing to propagate safely.
            return

        # For each follower, cancel matching open orders.
        for h in hooks:
            follower_id = int(h["follower_user_id"])
            follower_orders = get_open_orders_for_user(db=db, user_id=follower_id) or {}
            try:
                f_orders = (
                    follower_orders.get("data")
                    if isinstance(follower_orders, dict)
                    else follower_orders
                )
            except Exception:
                f_orders = follower_orders

            if not isinstance(f_orders, list):
                continue

            for o in f_orders:
                if not isinstance(o, dict):
                    continue
                tok = (
                    o.get("token_id")
                    or o.get("tokenId")
                    or o.get("tokenID")
                )
                side = (o.get("side") or o.get("direction") or "").upper()
                if str(tok) != str(leader_token_id):
                    continue
                if leader_side and side and side != leader_side:
                    continue
                oid = o.get("id") or o.get("orderID") or o.get("order_id")
                if not oid:
                    continue
                # Best-effort cancel; ignore failures.
                try:
                    await asyncio.to_thread(
                        cancel_order_for_user,
                        db,
                        follower_id,
                        str(oid),
                    )
                except Exception:
                    continue

    async def _run_global_copy_trading_tick(limit_per_leader: int = 50) -> Dict[str, Any]:
        """
        Indexer tick: look at global hooks (config.leader_address) and mirror
        recent Polymarket Data API trades for those addresses.
        """
        import json as _json

        rows = db.execute(
            """
            SELECT id, follower_user_id, leader_address, config
            FROM copy_hooks
            WHERE enabled = 1;
            """
        ).fetchall()

        by_addr: Dict[str, list[Dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            try:
                cfg = _json.loads(d.get("config") or "{}")
            except Exception:
                cfg = {}
            addr = (d.get("leader_address") or cfg.get("leader_address") or "").strip().lower()
            if not addr:
                continue
            d["config"] = cfg
            by_addr.setdefault(addr, []).append(d)

        if not by_addr:
            return {"processed_hooks": 0, "mirrored_trades": 0}

        total_hooks = 0
        total_trades = 0

        def _ts(t: Dict[str, Any]) -> int:
            try:
                return int(t.get("timestamp") or 0)
            except Exception:
                return 0

        for addr_lower, hooks in by_addr.items():
            leader_addr = next(
                (h["config"].get("leader_address") for h in hooks if h.get("config", {}).get("leader_address")),
                addr_lower,
            )
            leader_addr = (leader_addr or "").strip()
            if not leader_addr:
                continue

            # Fetch recent trades for this leader from Data API
            try:
                trades_raw = await asyncio.to_thread(
                    _fetch_url_sync,
                    "https://data-api.polymarket.com/trades",
                    {"user": leader_addr, "takerOnly": "true", "limit": limit_per_leader},
                    12,
                )
            except requests.RequestException:
                continue

            if not isinstance(trades_raw, list):
                continue

            trades_sorted = sorted(
                [t for t in trades_raw if isinstance(t, dict)],
                key=_ts,
            )

            for h in hooks:
                cfg = h.get("config") or {}
                last_seen_ts = int(cfg.get("last_seen_ts") or 0)
                follower_id = int(h["follower_user_id"])

                new_trades = [t for t in trades_sorted if _ts(t) > last_seen_ts]
                if not new_trades:
                    continue

                for t in new_trades:
                    cond_id = (t.get("conditionId") or t.get("condition_id") or "").strip()
                    outcome = (t.get("outcome") or "").strip().capitalize()
                    if not cond_id or not outcome:
                        continue

                    try:
                        leader_amt = float(t.get("amount") or 0.0)
                    except Exception:
                        leader_amt = 0.0
                    if leader_amt <= 0:
                        continue

                    # Optional slippage check: compare current price vs leader's trade price.
                    slippage_pct = cfg.get("slippage_pct")
                    try:
                        slippage_pct_f = float(slippage_pct) if slippage_pct is not None else 0.0
                    except Exception:
                        slippage_pct_f = 0.0
                    if slippage_pct_f > 0:
                        try:
                            leader_price = float(t.get("price") or 0.0)
                        except Exception:
                            leader_price = 0.0
                        if leader_price > 0:
                            follower_price = _get_market_price_for_outcome(cond_id, outcome)
                            if follower_price is not None:
                                diff_pct = abs(follower_price - leader_price) / leader_price * 100.0
                                if diff_pct > slippage_pct_f:
                                    # Price moved too far from leader's fill; skip this copy.
                                    continue

                    pm_side = (t.get("side") or "").upper()
                    order_side = "BUY" if pm_side != "SELL" else "SELL"

                    mode = (cfg.get("mode") or "").lower()
                    follower_amt = _compute_amount_for_hook_mode(
                        mode=mode,
                        leader_trade_amount_usd=leader_amt,
                        follower_user_id=follower_id,
                        leader_address=leader_addr,
                        cfg=cfg,
                    )
                    if not follower_amt:
                        continue

                    await asyncio.to_thread(
                        execute_trade_for_user,
                        db,
                        follower_id,
                        outcome,
                        follower_amt,
                        cond_id,
                        order_side,
                        copied_from_user_id=None,
                    )
                    total_trades += 1

                max_ts = max(( _ts(t) for t in new_trades ), default=last_seen_ts)
                cfg["last_seen_ts"] = max_ts
                db.update_copy_hook_config(int(h["id"]), cfg)
                total_hooks += 1

        return {"processed_hooks": total_hooks, "mirrored_trades": total_trades}

    def _maybe_schedule_copy_trade(
        user: Dict[str, Any],
        parsed: Dict[str, Any],
        condition_id: str,
        side: str,
        amount: float,
        order_side: str,
    ) -> None:
        """
        When a user trades successfully, fire hooks for their followers.
        Only followers with copy_trading_enabled=1 receive copies.
        """
        if not parsed.get("success") or parsed.get("failure"):
            return
        # Copy even when tx_hash is pending; Polymarket often returns that initially for FOK
        try:
            leader_id = int(user["user_id"])
        except Exception:
            return

        asyncio.create_task(
            _fire_copy_hooks(
                leader_user_id=leader_id,
                condition_id=condition_id,
                side=side,
                amount=amount,
                order_side=(order_side or "BUY").strip().upper() or "BUY",
            )
        )

    @app.get(
        "/price/{condition_id}",
        tags=["markets"],
        summary="Get current prices for a market by condition_id",
    )
    async def get_market_price(condition_id: str):
        """
        Return current cached prices for a market by Polymarket condition_id.
        Market is loaded from CLOB if not in cache.
        """
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if not m:
            raise HTTPException(
                status_code=404,
                detail="Market not found in cache. Call /markets/trending or /markets/search first.",
            )

        decimal_prices: Dict[str, float] = {}
        for outcome, cents in m.odds.items():
            try:
                decimal_prices[outcome] = float(cents) / 100.0
            except (TypeError, ValueError):
                decimal_prices[outcome] = 0.0

        return {
            "condition_id": m.condition_id,
            "question": m.question,
            "event_title": m.event_title,
            "prices": decimal_prices,
            "odds_cents": m.odds,
        }

    @app.get(
        "/price/{condition_id}/history",
        tags=["markets"],
        summary="Get historical prices for a market by condition_id",
    )
    async def get_market_price_history(
        condition_id: str,
        interval: str = Query("1d", description="Aggregation: 1h, 6h, 1d, 1w, 1m, all, max"),
        start_ts: int | None = Query(None, description="Start unix timestamp (seconds)"),
        end_ts: int | None = Query(None, description="End unix timestamp (seconds)"),
    ):
        """
        Historical price feed for a market by condition_id from Polymarket CLOB.
        Market is loaded from CLOB if not in cache.
        """
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if not m:
            raise HTTPException(
                status_code=404,
                detail="Market not found in cache. Call /markets/trending or /markets/search first.",
            )

        base_url = "https://clob.polymarket.com/prices-history"
        history_by_outcome: Dict[str, list[Dict[str, Any]]] = {}

        for outcome, token_id in zip(m.outcomes, m.clob_token_ids):
            params: Dict[str, Any] = {"market": token_id, "interval": interval}
            if start_ts is not None:
                params["startTs"] = start_ts
            if end_ts is not None:
                params["endTs"] = end_ts

            try:
                data = _fetch_url(base_url, params=params, timeout=15)
                if data is None:
                    history_by_outcome[outcome] = []
                    continue
                raw = data.get("history") or []
                history_by_outcome[outcome] = [
                    {"t": int(item.get("t", 0)), "p": float(item.get("p", 0))}
                    for item in raw
                ]
            except Exception:
                history_by_outcome[outcome] = []

        return {
            "condition_id": m.condition_id,
            "question": m.question,
            "event_title": m.event_title,
            "interval": interval,
            "history": history_by_outcome,
        }

    @app.get(
        "/markets/by-condition/{condition_id}",
        tags=["markets"],
        summary="Get full market details by condition_id",
    )
    async def get_market_by_condition_id(condition_id: str):
        """
        Get market by Polymarket condition_id (canonical market identifier).
        Market is loaded from CLOB if not in cache.
        """
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if not m:
            raise HTTPException(
                status_code=404,
                detail="Market not found in cache. Call /markets/trending or /markets/search first.",
            )
        decimal_prices = {o: float(m.odds.get(o, 0)) / 100.0 for o in m.outcomes}
        return {
            "condition_id": m.condition_id,
            "question": m.question,
            "event_title": m.event_title,
            "outcomes": m.outcomes,
            "token_ids": m.clob_token_ids,
            "prices": decimal_prices,
            "odds_cents": m.odds,
            "end_date": m.end_date,
        }

    class SwapRequest(BaseModel):
        amount: float | None = None  # in USD; if null or omitted, swap "all"

    @app.post(
        "/swap",
        tags=["trading"],
        summary="Swap USDC.e to bridged USDC on Polygon",
    )
    async def swap(body: SwapRequest, current=Depends(_get_current_session)):
        """
        Swap native USDC.e → bridged USDC on Polygon for the current user.

        Body:
          - { "amount": 10.0 } to swap ~$10 worth of USDC.e
          - { } or { "amount": null } to swap the full balance ("all")
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        from bot_tools import get_trading_wallet_address

        if body.amount is None:
            amount_arg = "all"
        else:
            amount_arg = str(body.amount)

        result = bot_tools.swap_usdc_for_trading(user["eth_address"], amount=amount_arg)
        return {
            # Swap operates on the EOA, but we surface the trading wallet so
            # frontend always keys off the same address.
            "wallet": get_trading_wallet_address(user["eth_address"]),
            "result": strip_emoji(result),
        }

    @app.get(
        "/bridge/deposit-addresses",
        tags=["trading"],
        summary="Get Polymarket Bridge deposit addresses (matches web flow)",
    )
    async def get_bridge_deposit_addresses(current=Depends(_get_current_session)):
        """
        Return deposit addresses from the Polymarket Bridge API for the current
        user's trading wallet. Use these to deposit from Ethereum, Solana, Bitcoin,
        Tron, etc.; assets are bridged and swapped to USDC.e on Polygon.
        See https://docs.polymarket.com/trading/bridge/deposit
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        from bot_tools import get_trading_wallet_address, get_polymarket_bridge_deposit_addresses
        wallet = get_trading_wallet_address(user["eth_address"])
        addresses = await asyncio.to_thread(get_polymarket_bridge_deposit_addresses, wallet)
        if addresses is None:
            raise HTTPException(
                status_code=502,
                detail="Could not fetch bridge deposit addresses from Polymarket.",
            )
        return {
            "wallet": wallet,
            "polygon_direct": wallet,
            "bridge": addresses,
        }

    class TradeRequest(BaseModel):
        condition_id: str
        side: str  # outcome: Yes | No
        amount: float
        order_side: str = "BUY"  # BUY or SELL (SELL = close position)
        auto_prepare: bool = True

    class LimitOrderRequest(BaseModel):
        condition_id: str
        side: str  # outcome: Yes | No
        price: float  # limit price per share in USD (0.01–0.99)
        size: float   # number of shares
        order_side: str = "BUY"  # BUY or SELL
        auto_prepare: bool = True

    class WithdrawRequest(BaseModel):
        amount: float | None = None  # None => withdraw all

    @app.post(
        "/trade",
        tags=["trading"],
        summary="Execute a Polymarket trade (BUY/SELL) by condition_id",
    )
    async def trade(body: TradeRequest, current=Depends(_get_current_session)):
        """
        Execute a Polymarket trade. Market is identified by condition_id only.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        from bot_tools import get_trading_wallet_address
        trading_addr = get_trading_wallet_address(user["eth_address"])

        prep_info: Dict[str, Any] = {}

        if body.auto_prepare:
            swap_result = bot_tools.swap_usdc_for_trading(
                user["eth_address"], amount="all"
            )
            prep_info["swap_result"] = strip_emoji(swap_result)
            approved_flag = user.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(
                    user["eth_address"]
                )
                prep_info["approve_result"] = strip_emoji(approve_result)
                success = not str(approve_result).lstrip().startswith("❌")
                if success:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                            (user["user_id"],),
                        )

        order_side_clean = (body.order_side or "BUY").strip().upper() or "BUY"
        result_text = execute_trade_for_user(
            db=db,
            user_id=user["user_id"],
            side=body.side,
            amount=body.amount,
            condition_id=body.condition_id,
            order_side=order_side_clean,
        )

        parsed = _parse_trade_result(result_text or "")
        _maybe_schedule_copy_trade(
            user=user,
            parsed=parsed,
            condition_id=body.condition_id,
            side=body.side,
            amount=body.amount,
            order_side=order_side_clean,
        )

        return {
            "wallet": trading_addr,
            "auto_prepare": prep_info if body.auto_prepare else None,
            "success": bool(parsed.get("success") and not parsed.get("failure")),
            "order_id": parsed.get("order_id"),
            "status": parsed.get("status"),
            "tx_hash": parsed.get("tx_hash"),
            "message": parsed.get("raw"),
        }

    @app.post(
        "/limit-order",
        tags=["trading"],
        summary="Place a LIMIT order (BUY/SELL) by condition_id, price, and size",
    )
    async def limit_order(body: LimitOrderRequest, current=Depends(_get_current_session)):
        """
        Place a LIMIT order in a Polymarket market identified by condition_id.

        - price: limit price per share in USD (0.01–0.99)
        - size: number of shares to buy/sell
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        from bot_tools import get_trading_wallet_address
        trading_addr = get_trading_wallet_address(user["eth_address"])

        prep_info: Dict[str, Any] = {}

        if body.auto_prepare:
            swap_result = bot_tools.swap_usdc_for_trading(
                user["eth_address"], amount="all"
            )
            prep_info["swap_result"] = strip_emoji(swap_result)
            approved_flag = user.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(
                    user["eth_address"]
                )
                prep_info["approve_result"] = strip_emoji(approve_result)
                success = not str(approve_result).lstrip().startswith("❌")
                if success:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                            (user["user_id"],),
                        )

        order_side_clean = (body.order_side or "BUY").strip().upper() or "BUY"
        result_text = execute_limit_order_for_user(
            db=db,
            user_id=user["user_id"],
            side=body.side,
            price=body.price,
            size=body.size,
            condition_id=body.condition_id,
            order_side=order_side_clean,
        )

        parsed = _parse_trade_result(result_text or "")

        return {
            "wallet": trading_addr,
            "auto_prepare": prep_info if body.auto_prepare else None,
            "raw": result_text,
            "parsed": parsed,
        }

    @app.post(
        "/withdraw",
        tags=["trading"],
        summary="Withdraw USDC.e from Safe trading wallet back to EOA",
    )
    async def withdraw(body: WithdrawRequest, current=Depends(_get_current_session)):
        """
        Transfer funds from the Safe trading wallet back to the user's EOA.

        Body:
          - { "amount": 10.0 } to withdraw ~$10 USDC.e
          - { } or { "amount": null } to withdraw the full Safe balance ("all")
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        amount_arg = "all" if body.amount is None else str(body.amount)

        result = await asyncio.to_thread(
            bot_tools.withdraw_safe_to_eoa,
            user["eth_address"],
            amount_arg,
        )

        from bot_tools import get_trading_wallet_address

        trading_addr = get_trading_wallet_address(user["eth_address"])

        return {
            "wallet_safe": trading_addr,
            "wallet_eoa": user["eth_address"],
            "result": strip_emoji(result),
        }

    class TransferToSafeRequest(BaseModel):
        amount: float | None = None  # None => transfer all

    @app.post(
        "/transfer_to_safe",
        tags=["trading"],
        summary="Transfer USDC.e from EOA to Safe trading wallet",
    )
    async def transfer_to_safe(
        body: TransferToSafeRequest, current=Depends(_get_current_session)
    ):
        """
        Transfer bridged USDC.e from the user's EOA to their Safe trading wallet.

        Body:
          - { "amount": 10.0 } to transfer ~$10 USDC.e
          - { } or { "amount": null } to transfer the full EOA USDC.e balance ("all")
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        amount_arg = "all" if body.amount is None else str(body.amount)

        result = await asyncio.to_thread(
            bot_tools.transfer_usdc_to_safe,
            user["eth_address"],
            amount_arg,
        )

        from bot_tools import get_trading_wallet_address

        trading_addr = get_trading_wallet_address(user["eth_address"])

        return {
            "wallet_safe": trading_addr,
            "wallet_eoa": user["eth_address"],
            "result": strip_emoji(result),
        }

    class OpenPositionRequest(BaseModel):
        condition_id: str
        size: float  # USD amount to buy
        outcome: str = "Yes"  # Yes | No
        auto_prepare: bool = True

    @app.post(
        "/position/open",
        tags=["trading"],
        summary="Open a position (buy) by condition_id and size",
    )
    async def open_position(body: OpenPositionRequest, current=Depends(_get_current_session)):
        """
        Open a position: buy shares in a market by condition_id. Size is USD amount to spend.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        outcome = (body.outcome or "Yes").strip()
        if outcome.upper() not in ("YES", "NO"):
            outcome = "Yes"

        prep_info: Dict[str, Any] = {}
        if body.auto_prepare:
            swap_result = bot_tools.swap_usdc_for_trading(
                user["eth_address"], amount="all"
            )
            prep_info["swap_result"] = strip_emoji(swap_result)
            approved_flag = user.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(
                    user["eth_address"]
                )
                prep_info["approve_result"] = strip_emoji(approve_result)
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                        (user["user_id"],),
                    )

        result = execute_trade_for_user(
            db=db,
            user_id=user["user_id"],
            side=outcome,
            amount=body.size,
            condition_id=body.condition_id,
            order_side="BUY",
        )

        parsed = _parse_trade_result(result or "")
        _maybe_schedule_copy_trade(
            user=user,
            parsed=parsed,
            condition_id=body.condition_id,
            side=outcome,
            amount=body.size,
            order_side="BUY",
        )

        return {
            "condition_id": body.condition_id,
            "outcome": outcome,
            "size": body.size,
            "auto_prepare": prep_info if body.auto_prepare else None,
            "result": parsed.get("raw"),
        }

    class ClosePositionRequest(BaseModel):
        condition_id: str
        size: float | None = None  # shares to sell; if omitted, close full position

    @app.post(
        "/position/close",
        tags=["trading"],
        summary="Close an existing position (sell) by condition_id",
    )
    async def close_position(body: ClosePositionRequest, current=Depends(_get_current_session)):
        """
        Close (sell) an existing position. Fetches your position for the given condition_id,
        then places a SELL order. Use size to partially close, or omit to close full position.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        address = user["eth_address"]
        positions_raw = await asyncio.to_thread(
            lambda: _fetch_url(f"https://data-api.polymarket.com/positions?user={address}", timeout=10)
            or []
        )
        if not isinstance(positions_raw, list):
            positions_raw = []

        cid = (body.condition_id or "").strip().lower()

        position = None
        for p in positions_raw:
            pc = (p.get("conditionId") or p.get("condition_id") or "").strip().lower()
            if pc == cid:
                position = p
                break

        if not position:
            raise HTTPException(
                status_code=404,
                detail="No position found for this market. Check condition_id from /me/portfolio.",
            )

        outcome = (position.get("outcome") or "Yes").strip()
        if outcome.upper() not in ("YES", "NO"):
            outcome = "Yes"
        try:
            position_size = float(position.get("size") or position.get("currentValue") or 0)
        except (TypeError, ValueError):
            position_size = 0.0

        if position_size <= 0:
            raise HTTPException(status_code=400, detail="Position has no size to close.")

        size_to_sell = body.size if body.size is not None and body.size > 0 else position_size
        if size_to_sell > position_size:
            size_to_sell = position_size

        result = execute_trade_for_user(
            db=db,
            user_id=user["user_id"],
            side=outcome,
            amount=size_to_sell,
            condition_id=body.condition_id,
            order_side="SELL",
        )

        parsed = _parse_trade_result(result or "")
        _maybe_schedule_copy_trade(
            user=user,
            parsed=parsed,
            condition_id=body.condition_id,
            side=outcome,
            amount=size_to_sell,
            order_side="SELL",
        )

        return {
            "condition_id": body.condition_id,
            "outcome": outcome,
            "size_closed": size_to_sell,
            "success": bool(parsed.get("success") and not parsed.get("failure")),
            "order_id": parsed.get("order_id"),
            "status": parsed.get("status"),
            "tx_hash": parsed.get("tx_hash"),
            "tx_id": parsed.get("tx_hash"),
            "message": parsed.get("raw"),
        }

    @app.get(
        "/me/orders",
        tags=["trading"],
        summary="List open CLOB orders for current user",
    )
    async def get_my_open_orders(current=Depends(_get_current_session)):
        """
        Get your open (resting) orders on Polymarket. Use the 'order_id' (or 'id') of
        each order in POST /trade/cancel to cancel it. Filled trades in /me/portfolio
        do not have an order_id for cancellation (they are already executed).
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        resp = get_open_orders_for_user(db=db, user_id=user["user_id"])
        if resp is None:
            raise HTTPException(
                status_code=502,
                detail="Failed to fetch open orders from Polymarket.",
            )

        # Normalize: CLOB returns data/next_cursor/count; ensure each order has order_id
        data = resp.get("data") if isinstance(resp, dict) else []
        if not isinstance(data, list):
            data = []
        orders = []
        for o in data:
            if isinstance(o, dict):
                order_id = o.get("id") or o.get("orderID") or o.get("order_id")
                orders.append({"order_id": order_id, **o})
            else:
                orders.append(o)

        return {
            "orders": orders,
            "next_cursor": resp.get("next_cursor", "") if isinstance(resp, dict) else "",
            "count": len(orders),
        }

    class CancelRequest(BaseModel):
        order_id: str

    @app.post(
        "/trade/cancel",
        tags=["trading"],
        summary="Cancel an existing Polymarket order by order_id",
    )
    async def cancel_trade(body: CancelRequest, current=Depends(_get_current_session)):
        """
        Cancel an existing Polymarket order. Use order_id from GET /me/orders (open
        orders only). Orders in /me/portfolio with source 'polymarket' are filled
        trades and have no order_id for cancellation.
        """
        user = db.get_user(current["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        result = cancel_order_for_user(
            db=db,
            user_id=user["user_id"],
            order_id=body.order_id,
        )

        # Best-effort: propagate this cancel to followers who have hooks for this leader.
        try:
            asyncio.create_task(
                _propagate_cancel_to_followers(
                    leader_user_id=user["user_id"],
                    leader_order_id=body.order_id,
                )
            )
        except Exception:
            pass

        return {
            "order_id": body.order_id,
            "result": strip_emoji(result),
        }

    class AnalyzeMarketRequest(BaseModel):
        query: str

    @app.post(
        "/analyze-market",
        tags=["analysis"],
        summary="Analyze a market with news, forecast, and risk score",
    )
    async def analyze_market(body: AnalyzeMarketRequest):
        """
        High-level market analysis helper.

        Given a free-form query (event description, ticker, or question), this endpoint:
          - Fetches recent news about the topic.
          - Finds the most relevant active Polymarket markets.
          - Produces a structured Markdown analysis with
            * news summary
            * market view
            * prediction with probability
            * 0–100 risk score with explanation.
        """
        analysis = await llm.run_market_analysis(body.query)
        return {
            "query": body.query,
            "analysis_markdown": strip_emoji(analysis),
        }

    return app

