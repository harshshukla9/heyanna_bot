import logging
import time
from typing import Union

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException

from database_manager import DatabaseManager
import market_cache
from bot_tools import approve_usdc_for_trading, get_trading_wallet_address


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_clob_client(private_key: str, use_safe: bool, trading_addr: str) -> ClobClient:
    """Create an authenticated ClobClient with derived API credentials."""
    l1 = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        signature_type=2 if use_safe else 0,
        funder=trading_addr if use_safe else None,
    )
    creds = l1.create_or_derive_api_creds()
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        creds=creds,
        signature_type=2 if use_safe else 0,
        funder=trading_addr if use_safe else None,
    )


def _is_allowance_error(exc: PolyApiException) -> bool:
    """Return True if the CLOB error is about missing balance or allowance."""
    err = getattr(exc, "error_message", {}) or {}
    msg = (err.get("error") or str(exc)).lower()
    return "allowance" in msg or "not enough balance" in msg


def _force_reapprove(db: DatabaseManager, user_id: int, owner_addr: str) -> bool:
    """Reset approval flag, run full gasless approval batch (incl. Safe deploy), return success."""
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE users SET polymarket_approved = 0 WHERE user_id = ?;",
                (user_id,),
            )
        result = approve_usdc_for_trading(owner_addr)
        ok = not str(result).lstrip().startswith("❌")
        if ok:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                    (user_id,),
                )
            logger.info("Forced re-approval succeeded for user %s", user_id)
        else:
            logger.warning("Forced re-approval returned failure: %s", result)
        return ok
    except Exception as e:
        logger.warning("Force re-approval failed: %s", e)
        return False


def _parse_order_response(resp) -> tuple[str, str, str]:
    """Extract (order_id, status, tx_hash) from a CLOB post_order response."""
    if isinstance(resp, dict):
        order_id = resp.get("orderID", resp.get("id", "N/A"))
        status = resp.get("status", "submitted")
        tx_hashes = resp.get("transactionsHashes") or resp.get("transactionHashes") or []
        tx_hash = (
            tx_hashes[0] if tx_hashes else
            resp.get("transactHash") or resp.get("txHash") or resp.get("transactionHash") or "pending"
        )
    else:
        order_id = str(resp)
        status = "submitted"
        tx_hash = "pending"
    return order_id, status, tx_hash


def execute_trade_for_user(
    db: DatabaseManager,
    user_id: int,
    side: str,
    amount: Union[float, str],
    condition_id: str,
    order_side: str = "BUY",
    copied_from_user_id: int | None = None,
) -> str:
    """
    Execute a trade for a user. side = outcome (Yes/No), order_side = BUY or SELL.
    Use order_side=SELL to close a position (sell the outcome you hold).
    """
    # Resolve market from cache, or fetch by condition_id from CLOB and add to cache
    market_cache.ensure_market_cached(condition_id)
    m = market_cache.get_by_condition_id(condition_id)
    if not m:
        return (
            f"Market with condition_id {condition_id[:16]}... not found in cache. "
            "Call /markets/trending or /markets/search first to load markets."
        )

    side_clean = side.strip().capitalize()
    if side_clean not in m.outcomes:
        return f"Invalid side '{side}'. Available outcomes: {', '.join(m.outcomes)}"

    # 2. Resolve the CLOB token ID for the chosen side
    side_idx = m.outcomes.index(side_clean)
    token_id = m.clob_token_ids[side_idx]
    odds_cents = m.odds.get(side_clean, 0)

    # 3. Get user's private key from unified DB
    db_user = db.get_user(user_id)
    if not db_user:
        return "CRITICAL: Could not find user for this session."

    private_key = db_user["eth_private_key"]
    if not private_key:
        return "CRITICAL: User has no private key stored."

    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    # Normalize amount
    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        return f"Invalid amount '{amount}'. Must be a number in USD."

    # Auto-approve via Builder relayer if user not yet approved (required for Safe trading)
    approved_flag = db_user.get("polymarket_approved") or 0
    if not approved_flag:
        _force_reapprove(db, user_id, owner_addr)

    order_side_clean = (order_side or "BUY").strip().upper()
    if order_side_clean not in ("BUY", "SELL"):
        order_side_clean = "BUY"

    order_id = status = tx_hash = None

    for attempt in range(2):
        try:
            trade_client = _create_clob_client(private_key, use_safe, trading_addr)

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_value,
                side=order_side_clean,
            )
            signed_order = trade_client.create_market_order(order_args)
            resp = trade_client.post_order(signed_order, orderType=OrderType.FOK)
            order_id, status, tx_hash = _parse_order_response(resp)
            break  # success

        except PolyApiException as e:
            if attempt == 0 and _is_allowance_error(e):
                logger.warning(
                    "Trade attempt 1 failed with allowance error for user %s; "
                    "forcing gasless re-approval and retrying...", user_id,
                )
                _force_reapprove(db, user_id, owner_addr)
                continue

            err = getattr(e, "error_message", {}) or {}
            last_error = err.get("error") or str(e)
            logger.error("Trade execution failed: %s", last_error)
            try:
                db.record_trade(
                    user_id=user_id, market_id=m.market_id, side=side_clean,
                    amount=amount_value, status="error", order_id=None,
                    tx_hash="", executed_at=int(time.time()),
                    copied_from_user_id=copied_from_user_id,
                    condition_id=m.condition_id,
                )
            except Exception:
                pass
            return (
                "❌ TRADE FAILED: not enough balance or allowance on your trading wallet.\n"
                "Make sure you have USDC.e in your Safe trading wallet (see /balance)."
            )

        except Exception as e:
            logger.error("Trade execution failed: %s", e)
            try:
                db.record_trade(
                    user_id=user_id, market_id=m.market_id, side=side_clean,
                    amount=amount_value, status="error", order_id=None,
                    tx_hash="", executed_at=int(time.time()),
                    copied_from_user_id=copied_from_user_id,
                    condition_id=m.condition_id,
                )
            except Exception:
                pass
            return f"❌ TRADE FAILED: {e}"
    else:
        return (
            "❌ TRADE FAILED after automatic re-approval.\n"
            "Ensure you have USDC.e in your Safe wallet and try again."
        )

    # Record trade in on-server feed only when status=matched and tx_hash is valid
    tx_valid = (
        tx_hash
        and str(tx_hash).strip()
        and str(tx_hash).strip().lower() != "pending"
    )
    if status == "matched" and tx_valid:
        try:
            price_frac = float(odds_cents) / 100.0 if odds_cents else None
            size_val = amount_value / price_frac if price_frac and price_frac > 0 else None
            db.record_trade(
                user_id=user_id,
                market_id=m.market_id,
                side=side_clean,
                amount=amount_value,
                status=status,
                order_id=order_id,
                tx_hash=tx_hash.strip(),
                executed_at=int(time.time()),
                copied_from_user_id=copied_from_user_id,
                condition_id=m.condition_id,
                size=size_val,
                price=price_frac,
                order_side=order_side_clean,
            )
        except Exception as e:
            logger.error(f"Failed to record trade in DB: {e}")

    return (
        f"✅ TRADE EXECUTED\n"
        f"Market: {m.question}\n"
        f"Side: {side_clean} @ {odds_cents}¢\n"
        f"Amount: ${amount_value}\n"
        f"Order ID: {order_id}\n"
        f"Status: {status}\n"
        f"TX Hash: {tx_hash}\n"
        f"Token ID: {token_id}"
    )


def execute_limit_order_for_user(
    db: DatabaseManager,
    user_id: int,
    side: str,
    price: float,
    size: float,
    condition_id: str,
    order_side: str = "BUY",
    copied_from_user_id: int | None = None,
) -> str:
    """
    Place a LIMIT order for a user using Safe-aware CLOB client.

    side: outcome (Yes/No).
    price: limit price per share in USD (0.01–0.99).
    size: number of shares to buy/sell.
    order_side: BUY or SELL.
    """
    market_cache.ensure_market_cached(condition_id)
    m = market_cache.get_by_condition_id(condition_id)
    if not m:
        return (
            f"Market with condition_id {condition_id[:16]}... not found in cache. "
            "Call /markets/trending or /markets/search first to load markets."
        )

    side_clean = side.strip().capitalize()
    if side_clean not in m.outcomes:
        return f"Invalid side '{side}'. Available outcomes: {', '.join(m.outcomes)}"

    try:
        price_val = float(price)
        size_val = float(size)
    except (TypeError, ValueError):
        return "Invalid price or size for limit order."
    if price_val <= 0 or price_val >= 1:
        return "Limit price must be between 0 and 1 (e.g. 0.45 for 45¢)."
    if size_val <= 0:
        return "Limit order size must be positive."

    side_idx = m.outcomes.index(side_clean)
    token_id = m.clob_token_ids[side_idx]
    odds_cents = m.odds.get(side_clean, 0)

    db_user = db.get_user(user_id)
    if not db_user:
        return "CRITICAL: Could not find user for this session."

    private_key = db_user["eth_private_key"]
    if not private_key:
        return "CRITICAL: User has no private key stored."

    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    # Auto-approve via Builder relayer if user not yet approved
    approved_flag = db_user.get("polymarket_approved") or 0
    if not approved_flag:
        _force_reapprove(db, user_id, owner_addr)

    order_side_clean = (order_side or "BUY").strip().upper()
    if order_side_clean not in ("BUY", "SELL"):
        order_side_clean = "BUY"

    order_id = status = tx_hash = None

    for attempt in range(2):
        try:
            trade_client = _create_clob_client(private_key, use_safe, trading_addr)

            order_args = OrderArgs(
                token_id=token_id,
                price=price_val,
                size=size_val,
                side=order_side_clean,
            )
            signed = trade_client.create_order(order_args)
            resp = trade_client.post_order(signed, orderType=OrderType.GTC)
            order_id, status, tx_hash = _parse_order_response(resp)
            break

        except PolyApiException as e:
            if attempt == 0 and _is_allowance_error(e):
                logger.warning(
                    "Limit order attempt 1 failed with allowance error for user %s; "
                    "forcing gasless re-approval and retrying...", user_id,
                )
                _force_reapprove(db, user_id, owner_addr)
                continue

            err = getattr(e, "error_message", {}) or {}
            msg = err.get("error") or str(e)
            logger.error("Limit order failed: %s", msg)
            return (
                "❌ LIMIT ORDER FAILED: not enough balance or allowance on your trading wallet.\n"
                "Make sure you have USDC.e in your Safe (see /balance)."
            )

        except Exception as e:
            logger.error("Limit order execution failed: %s", e)
            return f"❌ LIMIT ORDER FAILED: {e}"
    else:
        return (
            "❌ LIMIT ORDER FAILED after automatic re-approval.\n"
            "Ensure you have USDC.e in your Safe wallet and try again."
        )

    return (
        "✅ LIMIT ORDER PLACED\n"
        f"Market: {m.question}\n"
        f"Side: {side_clean}\n"
        f"Price: ${price_val:.4f}\n"
        f"Size: {size_val:.4f} shares\n"
        f"Est. notional: ${price_val * size_val:.4f}\n"
        f"Order ID: {order_id}\n"
        f"Status: {status}\n"
        f"TX Hash: {tx_hash}\n"
        f"Token ID: {token_id}"
    )


def cancel_order_for_user(
    db: DatabaseManager,
    user_id: int,
    order_id: str,
) -> str:
    """
    Cancel an existing Polymarket order for a given user.
    """
    db_user = db.get_user(user_id)
    if not db_user:
        return "CRITICAL: Could not find user for this session."

    private_key = db_user["eth_private_key"]
    if not private_key:
        return "CRITICAL: User has no private key stored."

    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    try:
        client = _create_clob_client(private_key, use_safe, trading_addr)
        resp = client.cancel(order_id=order_id)
        return f"Order {order_id} cancel response: {resp}"
    except Exception as e:
        logger.error("Order cancellation failed: %s", e)
        return f"CANCEL FAILED: {e}"


def get_open_orders_for_user(db: DatabaseManager, user_id: int):
    """
    Fetch open (resting) orders for the user from Polymarket CLOB.
    Returns list of orders; each has 'id' which is the order_id to use for cancel.
    """
    db_user = db.get_user(user_id)
    if not db_user:
        return None

    private_key = db_user["eth_private_key"]
    if not private_key:
        return None

    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    try:
        client = _create_clob_client(private_key, use_safe, trading_addr)
        return client.get_orders()
    except Exception as e:
        logger.error("Get open orders failed: %s", e)
        return None

