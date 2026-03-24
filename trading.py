import json
import logging
import time
from typing import Union

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL

from database_manager import DatabaseManager
import market_cache
from bot_tools import (
    approve_usdc_for_trading,
    get_trading_wallet_address,
    get_usdc_e_balance_on_polygon,
)


logger = logging.getLogger(__name__)


def _poly_err_payload(e: PolyApiException):
    """py_clob_client uses `error_msg`; older code referenced `error_message`."""
    if hasattr(e, "error_msg"):
        return e.error_msg
    return getattr(e, "error_message", None)


def _log_clob_error(
    e: PolyApiException,
    context: str,
    user_id: int | None = None,
    request_ctx: dict | None = None,
) -> None:
    """Log CLOB API error with status code, response body, and request context for debugging 400s."""
    status_code = getattr(e, "status_code", None)
    err_body = _poly_err_payload(e)
    try:
        body_str = json.dumps(err_body, default=str) if err_body is not None else repr(err_body)
    except Exception:
        body_str = repr(err_body)
    logger.error(
        "CLOB %s: HTTP %s | user_id=%s | response=%s",
        context,
        status_code if status_code is not None else "?",
        user_id,
        body_str,
    )
    if request_ctx:
        try:
            ctx_str = json.dumps(request_ctx, default=str)
        except Exception:
            ctx_str = repr(request_ctx)
        logger.error("CLOB 400 DEBUG request_ctx=%s", ctx_str)
    if status_code == 400 and err_body is not None and err_body != "":
        err_msg = err_body.get("error", str(err_body)) if isinstance(err_body, dict) else str(err_body)
        logger.error(
            "CLOB HTTP 400: %s | Check: balance/allowance for trading wallet, funder=Safe for sells, token_id/amount valid.",
            err_msg,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_clob_client(private_key: str, use_safe: bool, trading_addr: str) -> ClobClient:
    """
    Create an authenticated ClobClient per Polymarket skill: one client with funder,
    derive creds on it, then set_api_creds so creds are bound to that funder (Safe).
    """
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        signature_type=2 if use_safe else 0,
        funder=trading_addr if use_safe else None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _is_allowance_error(exc: PolyApiException) -> bool:
    """Return True if the CLOB error is about missing balance or allowance."""
    err = _poly_err_payload(exc)
    if isinstance(err, dict):
        msg = (err.get("error") or err.get("message") or json.dumps(err, default=str) or str(exc)).lower()
    elif err is not None and err != "":
        msg = str(err).lower()
    else:
        msg = str(exc).lower()
    needles = (
        "allowance",
        "not enough balance",
        "insufficient balance",
        "insufficient funds",
        "balance too low",
    )
    return any(n in msg for n in needles)


def _not_enough_usdc_message(trading_addr: str, balance: float | None, need_usd: float) -> str:
    bal_part = f"${balance:.4f}" if balance is not None else "unknown"
    return (
        "❌ TRADE FAILED: not enough USDC.e in your trading wallet "
        f"(balance {bal_part}, need ~${need_usd:.2f}).\n"
        "Deposit USDC.e to your Safe trading wallet and check /balance."
    )


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
        order_id = (
            resp.get("orderID")
            or resp.get("orderId")
            or resp.get("order_id")
            or resp.get("id")
            or resp.get("hash")
            or "N/A"
        )
        if order_id and order_id != "N/A" and not isinstance(order_id, str):
            order_id = str(order_id)
        status = (
            resp.get("status")
            or resp.get("orderStatus")
            or resp.get("order_status")
            or "submitted"
        )
        tx_hashes = resp.get("transactionsHashes") or resp.get("transactionHashes") or resp.get("transaction_hashes") or []
        tx_hash = (
            (tx_hashes[0] if tx_hashes else None)
            or resp.get("transactHash")
            or resp.get("txHash")
            or resp.get("transactionHash")
            or resp.get("transaction_hash")
            or "pending"
        )
        if tx_hash and not isinstance(tx_hash, str):
            tx_hash = str(tx_hash)
    else:
        order_id = str(resp)
        status = "submitted"
        tx_hash = "pending"
    return (order_id or "N/A"), (status or "submitted"), (tx_hash or "pending")


def execute_trade_for_user(
    db: DatabaseManager,
    user_id: int,
    side: str,
    amount: Union[float, str],
    condition_id: str,
    order_side: str = "BUY",
    copied_from_user_id: int | None = None,
    token_id: str | None = None,
    outcome_index: int | None = None,
    max_slippage: float = 0.05,
) -> str:
    """
    Execute a trade for a user. side = outcome (e.g. Yes/No, NSH, Under).
    Use order_side=SELL to close a position. Resolves generic labels (Yes/Under) to
    market's actual outcome name (e.g. NSH) when token_id or outcome_index is provided (copy-trade).

    Args:
        max_slippage: Maximum allowed slippage as decimal (default 5%). Used when falling back from FOK to market order.
    """
    # Resolve market from cache, or fetch by condition_id from CLOB and add to cache
    market_cache.ensure_market_cached(condition_id)
    m = market_cache.get_by_condition_id(condition_id)
    if not m:
        return (
            f"Market with condition_id {condition_id[:16]}... not found in cache. "
            "Call /markets/trending or /markets/search first to load markets."
        )

    # Resolve to market's actual outcome name (e.g. NSH not Yes/Under) for copy-trade
    side_raw = (side or "").strip()
    side_clean = None
    if token_id and m.clob_token_ids:
        try:
            tid = str(token_id).strip()
            idx = m.clob_token_ids.index(tid)
            if 0 <= idx < len(m.outcomes):
                side_clean = m.outcomes[idx]
        except (ValueError, AttributeError):
            pass
    if side_clean is None and outcome_index is not None and m.outcomes:
        try:
            i = int(outcome_index)
            if 0 <= i < len(m.outcomes):
                side_clean = m.outcomes[i]
        except (TypeError, ValueError):
            pass
    if side_clean is None and len(m.outcomes) >= 2:
        low = side_raw.lower()
        if low in ("yes", "1", "long"):
            side_clean = m.outcomes[0]
        elif low in ("no", "0", "short"):
            side_clean = m.outcomes[1]
    if side_clean is None:
        side_lower = side_raw.lower()
        side_clean = next((o for o in m.outcomes if o.lower() == side_lower), None)
    if not side_clean:
        market_cache.clear_by_condition_id(condition_id)
        market_cache.ensure_market_cached(condition_id)
        m = market_cache.get_by_condition_id(condition_id)
        if m:
            if token_id and m.clob_token_ids:
                try:
                    idx = m.clob_token_ids.index(str(token_id).strip())
                    if 0 <= idx < len(m.outcomes):
                        side_clean = m.outcomes[idx]
                except (ValueError, AttributeError):
                    pass
            if side_clean is None and outcome_index is not None:
                try:
                    i = int(outcome_index)
                    if 0 <= i < len(m.outcomes):
                        side_clean = m.outcomes[i]
                except (TypeError, ValueError):
                    pass
            if side_clean is None:
                side_lower = side_raw.lower()
                side_clean = next((o for o in m.outcomes if o.lower() == side_lower), None)
        if not side_clean or not m:
            return f"Invalid side '{side}'. Available outcomes: {', '.join(m.outcomes) if m else 'unknown'}"

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

    # BUY market orders spend USDC.e from the trading (Safe) wallet; re-approval cannot create funds.
    if order_side_clean == "BUY" and amount_value > 0 and trading_addr:
        try:
            bal0 = get_usdc_e_balance_on_polygon(trading_addr)
            if bal0 is not None and bal0 + 1e-6 < amount_value:
                return _not_enough_usdc_message(trading_addr, bal0, amount_value)
        except Exception:
            pass

    order_id = status = tx_hash = None
    fok_failed = False

    for attempt in range(3):
        try:
            trade_client = _create_clob_client(private_key, use_safe, trading_addr)

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_value,
                side=order_side_clean,
            )
            signed_order = trade_client.create_market_order(order_args)

            # Try FOK first, fall back to FAK if FOK fails due to liquidity
            order_type = OrderType.FOK if not fok_failed else OrderType.FAK

            resp = trade_client.post_order(signed_order, orderType=order_type)
            order_id, status, tx_hash = _parse_order_response(resp)

            # If using IOC and got partial/failed status, log warning but continue
            if order_type == OrderType.IOC and status not in ("matched", "filled"):
                logger.warning("IOC order returned status %s for user %s", status, user_id)

            break  # success

        except PolyApiException as e:
            _log_clob_error(
                e,
                "post_order (market)",
                user_id=user_id,
                request_ctx={
                    "token_id": token_id,
                    "amount": amount_value,
                    "side": side_clean,
                    "order_side": order_side_clean,
                    "trading_addr": trading_addr,
                    "use_safe": use_safe,
                    "condition_id": m.condition_id[:20] + "..." if m.condition_id else None,
                    "order_type": "FOK" if not fok_failed else "IOC",
                },
            )

            payload = _poly_err_payload(e)
            if isinstance(payload, dict):
                last_error = payload.get("error") or payload.get("message") or json.dumps(payload, default=str)
            elif payload is not None and payload != "":
                last_error = str(payload)
            else:
                last_error = str(e)

            # Check if this is an allowance/balance error
            if _is_allowance_error(e):
                if attempt == 0:
                    usdc_bal = None
                    try:
                        usdc_bal = get_usdc_e_balance_on_polygon(trading_addr)
                        logger.warning(
                            "Trade attempt 1 failed with allowance/balance error for user %s; "
                            "trading_addr=%s USDC.e_balance=%.4f; forcing gasless re-approval and retrying...",
                            user_id, trading_addr, usdc_bal if usdc_bal is not None else 0.0,
                        )
                    except Exception:
                        logger.warning(
                            "Trade attempt 1 failed with allowance/balance error for user %s; "
                            "trading_addr=%s; forcing gasless re-approval and retrying...",
                            user_id, trading_addr,
                        )
                    if (
                        order_side_clean == "BUY"
                        and usdc_bal is not None
                        and usdc_bal + 1e-6 < amount_value
                    ):
                        logger.info(
                            "Skipping re-approval for user %s: BUY notional exceeds USDC.e balance on trading wallet",
                            user_id,
                        )
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
                        return _not_enough_usdc_message(trading_addr, usdc_bal, amount_value)
                    _force_reapprove(db, user_id, owner_addr)
                    time.sleep(5)
                    continue
                else:
                    ub = None
                    try:
                        ub = get_usdc_e_balance_on_polygon(trading_addr)
                    except Exception:
                        pass
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
                    if (
                        order_side_clean == "BUY"
                        and ub is not None
                        and ub + 1e-6 < amount_value
                    ):
                        return _not_enough_usdc_message(trading_addr, ub, amount_value)
                    return (
                        "❌ TRADE FAILED: not enough balance or allowance on your trading wallet.\n"
                        "Make sure you have USDC.e in your Safe trading wallet (see /balance)."
                    )

            # Check if this is a FOK liquidity error - fallback to FAK (partial fills allowed)
            if "FOK" in str(last_error) or "couldn't be fully filled" in str(last_error).lower():
                if not fok_failed:
                    logger.info(
                        "FOK order failed due to liquidity for user %s; "
                        "retrying with FAK (partial fills allowed)...",
                        user_id,
                    )
                    fok_failed = True
                    continue

            # Other error - log and fail
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
            return f"❌ TRADE FAILED: {last_error}"

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
            "❌ TRADE FAILED after automatic retries.\n"
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

    side_raw = (side or "").strip()
    side_lower = side_raw.lower()
    side_clean = next((o for o in m.outcomes if o.lower() == side_lower), None)
    if not side_clean:
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

    # Single attempt - no retry to prevent duplicate orders
    try:
        trade_client = _create_clob_client(private_key, use_safe, trading_addr)

        # Step 1: Create and sign locally with options (tick_size, neg_risk)
        try:
            tick_size = trade_client.get_tick_size(token_id)
        except Exception:
            tick_size = "0.01"
        try:
            neg_risk = trade_client.get_neg_risk(token_id)
        except Exception:
            neg_risk = False
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        side_const = BUY if order_side_clean == "BUY" else SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=price_val,
            size=size_val,
            side=side_const,
        )
        signed_order = trade_client.create_order(order_args, options=options)

        # Step 2: Submit to the CLOB
        resp = trade_client.post_order(signed_order, orderType=OrderType.GTC)
        order_id, status, tx_hash = _parse_order_response(resp)

        # Log successful order creation
        logger.info(
            "Limit order created: user=%s condition_id=%s side=%s price=%.4f size=%.4f order_id=%s status=%s",
            user_id, m.condition_id[:20] if m.condition_id else None, side_clean, price_val, size_val, order_id, status
        )

    except PolyApiException as e:
        _log_clob_error(
            e,
            "post_order (limit)",
            user_id=user_id,
            request_ctx={
                "token_id": token_id,
                "price": price_val,
                "size": size_val,
                "side": side_clean,
                "order_side": order_side_clean,
                "trading_addr": trading_addr,
                "use_safe": use_safe,
                "condition_id": m.condition_id[:20] + "..." if m.condition_id else None,
            },
        )
        payload = _poly_err_payload(e)
        if isinstance(payload, dict):
            msg = payload.get("error") or payload.get("message") or json.dumps(payload, default=str)
        elif payload is not None and payload != "":
            msg = str(payload)
        else:
            msg = str(e)
        logger.error("Limit order failed: %s", msg)
        return (
            "❌ LIMIT ORDER FAILED: not enough balance or allowance on your trading wallet.\n"
            "Make sure you have USDC.e in your Safe (see /balance)."
        )

    except Exception as e:
        logger.error("Limit order execution failed: %s", e)
        return f"❌ LIMIT ORDER FAILED: {e}"

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
    Fetch all open (resting) orders for the user from Polymarket CLOB.
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


def get_open_orders_page_for_user(
    db: DatabaseManager,
    user_id: int,
    market: str | None = None,
    order_id: str | None = None,
    asset_id: str | None = None,
    next_cursor: str | None = None,
):
    """
    Fetch one page of open orders from Polymarket CLOB (GET /data/orders).
    Returns {"data": list, "next_cursor": str} for pagination.
    """
    from py_clob_client.clob_types import OpenOrderParams, RequestArgs
    from py_clob_client.endpoints import ORDERS
    from py_clob_client.http_helpers.helpers import add_query_open_orders_params, get

    from py_clob_client.headers.headers import create_level_2_headers

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
        client.assert_level_2_auth()
        params = OpenOrderParams(
            id=order_id or "",
            market=market or "",
            asset_id=asset_id or "",
        )
        request_args = RequestArgs(method="GET", request_path=ORDERS)
        headers = create_level_2_headers(client.signer, client.creds, request_args)
        cursor = next_cursor if next_cursor else "MA=="
        url = add_query_open_orders_params(f"{client.host}{ORDERS}", params, cursor)
        response = get(url, headers=headers)
        return {
            "data": response.get("data") or [],
            "next_cursor": response.get("next_cursor") or "",
        }
    except Exception as e:
        logger.error("Get open orders page failed: %s", e)
        return None

