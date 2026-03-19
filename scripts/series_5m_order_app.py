"""
Sample client app: place orders for 5m series markets.

This script resolves a Polymarket/Gamma event slug (e.g. a 5m series event)
to the currently tradable market condition_id via the public Gamma API, then
places either a market order (/trade) or a limit order (/limit-order) through
the local API app (api_app.py).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

# Ensure repo root is importable when running as:
#   python scripts/series_5m_order_app.py
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database_manager import DatabaseManager
import market_cache
import bot_tools
from trading import execute_trade_for_user, execute_limit_order_for_user


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_PUBLIC_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"


def _coerce_bool(v: bool) -> str:
    return "true" if v else "false"


def _extract_event_slug_from_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    # slug is last non-empty path segment for common URLs:
    # - https://polymarket.com/event/<slug>
    # - https://gamma-api.polymarket.com/events?slug=<slug> (not a path)
    parts = [p for p in u.replace("?", "/").replace("#", "/").split("/") if p]
    if not parts:
        return ""
    last = parts[-1].strip()
    if last.lower() in ("events", "event") and len(parts) >= 2:
        last = parts[-2].strip()
    # If someone pasted "...slug=<slug>" and our split caught "slug=<slug>".
    if "slug=" in last:
        last = last.split("slug=", 1)[1].strip()
    return last


def _fetch_gamma_event_by_slug(slug: str) -> dict[str, Any]:
    s = (slug or "").strip()
    if not s:
        raise ValueError("event_slug is required")
    resp = requests.get(GAMMA_EVENTS_URL, params={"slug": s}, timeout=12)
    if resp.status_code != 200:
        raise RuntimeError(f"Gamma error HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        evs = data.get("events")
        if isinstance(evs, list) and evs and isinstance(evs[0], dict):
            return evs[0]
    raise RuntimeError("Gamma returned no event for this slug")


def _gamma_search_events(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """
    Best-effort event search helper.
    Tries Gamma public-search first; falls back to /events?slug_contains=... if available.
    """
    q = (query or "").strip()
    if not q:
        return []

    # 1) Public search (q=...) across markets/events/profiles.
    try:
        resp = requests.get(
            GAMMA_PUBLIC_SEARCH_URL,
            params={"q": q, "limit_per_type": int(limit)},
            timeout=12,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Common shapes:
            # - {"events":[...], "markets":[...], ...}
            # - {"data":{"events":[...]}}
            if isinstance(data, dict):
                if isinstance(data.get("events"), list):
                    return [e for e in data["events"] if isinstance(e, dict)][:limit]
                inner = data.get("data")
                if isinstance(inner, dict) and isinstance(inner.get("events"), list):
                    return [e for e in inner["events"] if isinstance(e, dict)][:limit]
    except Exception:
        pass

    # 2) Fallback: /events?slug_contains=<q>
    try:
        resp = requests.get(GAMMA_EVENTS_URL, params={"slug_contains": q, "_limit": int(limit)}, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)][:limit]
            if isinstance(data, dict) and isinstance(data.get("events"), list):
                return [e for e in data["events"] if isinstance(e, dict)][:limit]
    except Exception:
        pass

    return []


def _gamma_public_search_events(query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """
    Use Gamma public-search to retrieve event candidates (best-effort).
    This endpoint has proven more reliable than /events filtering in some deployments.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        resp = requests.get(
            GAMMA_PUBLIC_SEARCH_URL,
            params={"q": q, "limit_per_type": int(limit)},
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if isinstance(data, dict):
            if isinstance(data.get("events"), list):
                return [e for e in data["events"] if isinstance(e, dict)][:limit]
            inner = data.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("events"), list):
                return [e for e in inner["events"] if isinstance(e, dict)][:limit]
    except Exception:
        return []
    return []

def _gamma_iter_events(
    *,
    active: bool | None = True,
    closed: bool | None = False,
    order: str | None = None,
    ascending: bool = False,
    series_slug_contains: str | None = None,
    slug_contains: str | None = None,
    limit: int = 200,
    max_events: int = 5000,
) -> list[dict[str, Any]]:
    """
    Crawl Gamma /events with pagination and return raw event dicts.

    Notes:
    - Gamma param names vary slightly; we send both `_limit` and `limit`.
    - We include `offset` for pagination.
    """
    page_size = max(1, min(int(limit), 500))
    hard_cap = max(1, int(max_events))

    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < hard_cap:
        params: dict[str, Any] = {
            "_limit": page_size,
            "limit": page_size,
            "offset": offset,
        }
        if order:
            params["order"] = order
            params["ascending"] = _coerce_bool(bool(ascending))
        if active is not None:
            params["active"] = _coerce_bool(bool(active))
        if closed is not None:
            params["closed"] = _coerce_bool(bool(closed))
        if series_slug_contains:
            # documented param is seriesSlug_contains (camelCase), but some gateways accept series_slug_contains.
            params["seriesSlug_contains"] = series_slug_contains
            params["series_slug_contains"] = series_slug_contains
        if slug_contains:
            params["slug_contains"] = slug_contains

        resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
        if resp.status_code != 200:
            # Some Gamma deployments reject unknown `order` values with 422.
            # Fall back to no-order mode and let the caller sort locally.
            if resp.status_code == 422 and order:
                return _gamma_iter_events(
                    active=active,
                    closed=closed,
                    order=None,
                    ascending=ascending,
                    series_slug_contains=series_slug_contains,
                    slug_contains=slug_contains,
                    limit=limit,
                    max_events=max_events,
                )
            raise RuntimeError(f"Gamma /events HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

        if isinstance(data, list):
            batch = [e for e in data if isinstance(e, dict)]
        elif isinstance(data, dict) and isinstance(data.get("events"), list):
            batch = [e for e in data["events"] if isinstance(e, dict)]
        else:
            batch = []

        if not batch:
            break

        out.extend(batch)
        offset += len(batch)
        if len(batch) < page_size:
            break

    return out[:hard_cap]


def _pick_series_title_from_event(ev: dict[str, Any]) -> str | None:
    try:
        series_arr = ev.get("series") or []
        if isinstance(series_arr, list) and series_arr:
            s0 = series_arr[0] if isinstance(series_arr[0], dict) else None
            if s0:
                title = (s0.get("title") or "").strip()
                return title or None
    except Exception:
        return None
    return None


def _pick_condition_id_from_event(ev: dict[str, Any]) -> str | None:
    try:
        markets = ev.get("markets") or []
        m0 = markets[0] if isinstance(markets, list) and markets and isinstance(markets[0], dict) else None
        if not m0:
            return None
        cid = (m0.get("conditionId") or m0.get("conditionID") or m0.get("condition_id") or "").strip()
        return cid or None
    except Exception:
        return None


def _pick_end_date_from_event(ev: dict[str, Any]) -> str | None:
    try:
        markets = ev.get("markets") or []
        m0 = markets[0] if isinstance(markets, list) and markets and isinstance(markets[0], dict) else None
        if not m0:
            return None
        iso = (m0.get("endDate") or m0.get("end_date") or "").strip()
        return iso or None
    except Exception:
        return None


def _iso_to_ts(iso: str | None) -> int:
    s = (iso or "").strip()
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


@dataclass(frozen=True)
class ResolvedSeriesMarket:
    event_slug: str
    series_slug: str | None
    series_title: str | None
    condition_id: str
    end_date_iso: str | None


def _resolve_series_market(event_slug_or_url: str) -> ResolvedSeriesMarket:
    slug = (event_slug_or_url or "").strip()
    if "://" in slug:
        slug = _extract_event_slug_from_url(slug)
    ev = _fetch_gamma_event_by_slug(slug)

    series_slug = (ev.get("seriesSlug") or "").strip() or None
    series_title = None
    try:
        series_arr = ev.get("series") or []
        if isinstance(series_arr, list) and series_arr:
            s0 = series_arr[0] if isinstance(series_arr[0], dict) else None
            if s0:
                series_title = (s0.get("title") or "").strip() or None
    except Exception:
        series_title = None

    markets = ev.get("markets") or []
    m0 = markets[0] if isinstance(markets, list) and markets and isinstance(markets[0], dict) else None
    if not m0:
        raise RuntimeError("Gamma event has no markets[] to trade")

    condition_id = (m0.get("conditionId") or m0.get("conditionID") or m0.get("condition_id") or "").strip()
    if not condition_id:
        raise RuntimeError("Gamma market missing conditionId")

    end_date_iso = (m0.get("endDate") or m0.get("end_date") or "").strip() or None

    return ResolvedSeriesMarket(
        event_slug=slug,
        series_slug=series_slug,
        series_title=series_title,
        condition_id=condition_id,
        end_date_iso=end_date_iso,
    )


def _resolve_latest_event_for_series_slug(
    series_slug: str,
    *,
    max_events: int = 2000,
    page_size: int = 200,
    active_only: bool = False,
    now_ts: int | None = None,
) -> ResolvedSeriesMarket:
    """
    Resolve a seriesSlug (e.g. 'btc-up-or-down-5m') to the event closest to current system time.
    For time-series markets (5m, 15m, etc.), we construct the expected event slug based on
    the system clock and fetch directly, since Gamma search is unreliable for rolling events.

    Args:
        series_slug: The series slug to resolve (e.g. 'btc-up-or-down-5m')
        now_ts: Current timestamp (defaults to system time)
    """
    import time
    ss = (series_slug or "").strip()
    if not ss:
        raise ValueError("series_slug is required")

    now = now_ts if now_ts is not None else int(time.time())

    # For known time-series patterns, use direct event construction based on system clock
    if "-up-or-down-" in ss:
        try:
            asset = ss.split("-", 1)[0].strip()
        except Exception:
            asset = ""

        time_suffix = "5m"
        interval_seconds = 5 * 60
        if "15m" in ss:
            time_suffix = "15m"
            interval_seconds = 15 * 60
        elif "1h" in ss:
            time_suffix = "1h"
            interval_seconds = 60 * 60
        elif "4h" in ss:
            time_suffix = "4h"
            interval_seconds = 4 * 60 * 60

        # Find the event closest to current time by constructing expected slugs
        aligned_ts = (now // interval_seconds) * interval_seconds

        # Try current interval first, then go backwards until we find a valid event
        best_ev: dict[str, Any] | None = None
        best_ts_diff = float('inf')

        for i in range(10):  # Try up to 10 intervals back
            ts = aligned_ts - (i * interval_seconds)
            event_slug = f"{asset}-updown-{time_suffix}-{ts}"
            try:
                ev = _fetch_gamma_event_by_slug(event_slug)
                ev_ss = (ev.get("seriesSlug") or "").strip()
                if ev_ss != ss:
                    continue

                end_iso = _pick_end_date_from_event(ev)
                ev_end_ts = _iso_to_ts(end_iso)

                # Calculate time difference from now to event end time
                ts_diff = abs(ev_end_ts - now)

                # Accept if:
                # 1. It's the closest event we've found so far, AND
                # 2. Either it's not closed, or we've searched far enough back
                if ts_diff < best_ts_diff:
                    if active_only:
                        if bool(ev.get("active", True)) and not bool(ev.get("closed")):
                            best_ev = ev
                            best_ts_diff = ts_diff
                            slug = (ev.get("slug") or "").strip()
                            if slug:
                                return _resolve_series_market(slug)
                    else:
                        # Prefer non-closed, but accept closed if it's the closest
                        if not bool(ev.get("closed")) or best_ev is None:
                            best_ev = ev
                            best_ts_diff = ts_diff

                # If we found an active non-closed event, we're done
                if best_ev and not bool(best_ev.get("closed")) and bool(best_ev.get("active", True)):
                    slug = (best_ev.get("slug") or "").strip()
                    if slug:
                        return _resolve_series_market(slug)

            except Exception:
                continue

        if best_ev:
            slug = (best_ev.get("slug") or "").strip()
            if slug:
                return _resolve_series_market(slug)

    # Fallback: query candidates via search
    def _query_candidates() -> list[dict[str, Any]]:
        queries = [ss, ss.replace("-", " ")]

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for q in queries:
            for ev in _gamma_public_search_events(q, limit=200):
                slug = (ev.get("slug") or "").strip()
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                merged.append(ev)
        return merged

    events = _query_candidates()
    best_ev_result: dict[str, Any] | None = None
    best_ts = 0
    for ev in events:
        ev_ss = (ev.get("seriesSlug") or "").strip()
        if ev_ss != ss:
            continue
        end_iso = _pick_end_date_from_event(ev)
        ts = _iso_to_ts(end_iso)
        if active_only:
            try:
                if bool(ev.get("closed")) or not bool(ev.get("active", True)):
                    continue
            except Exception:
                pass
        if ts >= best_ts:
            best_ts = ts
            best_ev_result = ev

    if not best_ev_result:
        raise RuntimeError("No events found for this series_slug.")

    slug = (best_ev_result.get("slug") or "").strip()
    if not slug:
        raise RuntimeError("Gamma event missing slug.")
    return _resolve_series_market(slug)


def _get_market_info_by_condition_id(condition_id: str) -> dict[str, Any]:
    market_cache.ensure_market_cached(condition_id)
    m = market_cache.get_by_condition_id(condition_id)
    if not m:
        raise RuntimeError("Market not found/cached. Could not fetch from CLOB.")
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


def main() -> int:
    if callable(load_dotenv):
        try:
            load_dotenv()
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Sample 5m series order app (via local API).")
    p.add_argument(
        "--db-path",
        default=(os.getenv("DB_PATH") or "app_data.sqlite3"),
        help="SQLite DB path (env: DB_PATH, default: app_data.sqlite3)",
    )
    p.add_argument(
        "--user-id",
        default=(os.getenv("USER_ID") or os.getenv("TELEGRAM_USER_ID") or ""),
        help="User id to trade as (env: USER_ID or TELEGRAM_USER_ID)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_interactive = sub.add_parser("interactive", help="Interactive CLI (discover series -> place orders)")
    sp_interactive.add_argument("--contains", default="5m", help="Filter seriesSlug contains (default: 5m)")
    sp_interactive.add_argument("--max-events", type=int, default=5000, help="Max events to scan for series list (default: 5000)")
    sp_interactive.add_argument("--page-size", type=int, default=200, help="Gamma page size for series scan (default: 200)")
    sp_interactive.add_argument("--max-series", type=int, default=200, help="Max series to show (default: 200)")

    sp_series = sub.add_parser("series", help="List unique 5m series slugs discovered from Gamma")
    sp_series.add_argument("--contains", default="5m", help="Filter seriesSlug contains (default: 5m)")
    sp_series.add_argument("--max-events", type=int, default=5000, help="Max events to scan (default: 5000)")
    sp_series.add_argument("--page-size", type=int, default=200, help="Gamma page size (default: 200)")
    sp_series.add_argument("--active-only", action="store_true", help="Only active (default: yes)")

    sp_latest = sub.add_parser("latest", help="For each seriesSlug, pick latest active event and its condition_id")
    sp_latest.add_argument("--contains", default="5m", help="Filter seriesSlug contains (default: 5m)")
    sp_latest.add_argument("--max-events", type=int, default=5000, help="Max events to scan (default: 5000)")
    sp_latest.add_argument("--page-size", type=int, default=200, help="Gamma page size (default: 200)")
    sp_latest.add_argument("--max-series", type=int, default=200, help="Max series to output (default: 200)")
    sp_latest.add_argument("--active-only", action="store_true", help="Only active (default: yes)")

    sp_latest1 = sub.add_parser("latest-one", help="Resolve series_slug -> latest event -> condition_id")
    sp_latest1.add_argument("--series-slug", required=True, help="Exact Gamma seriesSlug (e.g. btc-up-or-down-5m)")
    sp_latest1.add_argument("--max-events", type=int, default=2000, help="Max events to scan (default: 2000)")
    sp_latest1.add_argument("--page-size", type=int, default=200, help="Gamma page size (default: 200)")
    sp_latest1.add_argument("--active-only", action="store_true", help="Only consider active events (may exclude fresh 5m)")

    sp_resolve = sub.add_parser("resolve", help="Resolve event slug/url -> condition_id")
    sp_resolve.add_argument("--event", required=True, help="Gamma/Polymarket event slug or URL (5m series)")

    sp_search = sub.add_parser("search", help="Search Gamma events and print candidate slugs")
    sp_search.add_argument("--q", required=True, help="Search query (e.g. 'up or down 5m', 'btc 5m')")
    sp_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    sp_info = sub.add_parser("info", help="Show market question and prices (via CLOB + cache)")
    sp_info.add_argument("--event", required=True, help="Gamma/Polymarket event slug or URL (5m series)")

    sp_users = sub.add_parser("users", help="List users from local DB (to find a user_id)")
    sp_users.add_argument("--limit", type=int, default=25, help="Max rows (default: 25)")

    sp_market = sub.add_parser("market", help="Place a market order (in-process, via trading.py)")
    sp_market.add_argument("--event", required=True, help="Gamma/Polymarket event slug or URL (5m series)")
    sp_market.add_argument("--side", required=True, choices=["YES", "NO"], help="Outcome side")
    sp_market.add_argument("--amount-usd", type=float, required=True, help="USD amount to spend (market order)")
    sp_market.add_argument("--order-side", default="BUY", choices=["BUY", "SELL"], help="BUY=open, SELL=close")
    sp_market.add_argument("--no-auto-prepare", action="store_true", help="Disable auto swap/approve")

    sp_market_latest = sub.add_parser("market-latest", help="Place a market order by seriesSlug (latest event, in-process)")
    sp_market_latest.add_argument("--series-slug", required=True, help="Exact Gamma seriesSlug (e.g. btc-up-or-down-5m)")
    sp_market_latest.add_argument("--side", required=True, choices=["YES", "NO"], help="Outcome side")
    sp_market_latest.add_argument("--amount-usd", type=float, required=True, help="USD amount to spend (market order)")
    sp_market_latest.add_argument("--order-side", default="BUY", choices=["BUY", "SELL"], help="BUY=open, SELL=close")
    sp_market_latest.add_argument("--no-auto-prepare", action="store_true", help="Disable auto swap/approve")
    sp_market_latest.add_argument("--max-events", type=int, default=2000, help="Max events to scan (default: 2000)")
    sp_market_latest.add_argument("--page-size", type=int, default=200, help="Gamma page size (default: 200)")
    sp_market_latest.add_argument("--active-only", action="store_true", help="Only consider active events (may exclude fresh 5m)")

    sp_limit = sub.add_parser("limit", help="Place a limit order (in-process, via trading.py)")
    sp_limit.add_argument("--event", required=True, help="Gamma/Polymarket event slug or URL (5m series)")
    sp_limit.add_argument("--side", required=True, choices=["YES", "NO"], help="Outcome side")
    sp_limit.add_argument("--price", type=float, required=True, help="Limit price per share (0.01–0.99)")
    sp_limit.add_argument("--size", type=float, required=True, help="Number of shares")
    sp_limit.add_argument("--order-side", default="BUY", choices=["BUY", "SELL"], help="BUY=open, SELL=close")
    sp_limit.add_argument("--no-auto-prepare", action="store_true", help="Disable auto swap/approve")

    sp_limit_latest = sub.add_parser("limit-latest", help="Place a limit order by seriesSlug (latest event, in-process)")
    sp_limit_latest.add_argument("--series-slug", required=True, help="Exact Gamma seriesSlug (e.g. btc-up-or-down-5m)")
    sp_limit_latest.add_argument("--side", required=True, choices=["YES", "NO"], help="Outcome side")
    sp_limit_latest.add_argument("--price", type=float, required=True, help="Limit price per share (0.01–0.99)")
    sp_limit_latest.add_argument("--size", type=float, required=True, help="Number of shares")
    sp_limit_latest.add_argument("--order-side", default="BUY", choices=["BUY", "SELL"], help="BUY=open, SELL=close")
    sp_limit_latest.add_argument("--no-auto-prepare", action="store_true", help="Disable auto swap/approve")
    sp_limit_latest.add_argument("--max-events", type=int, default=2000, help="Max events to scan (default: 2000)")
    sp_limit_latest.add_argument("--page-size", type=int, default=200, help="Gamma page size (default: 200)")
    sp_limit_latest.add_argument("--active-only", action="store_true", help="Only consider active events (may exclude fresh 5m)")

    args = p.parse_args()
    db_path = (args.db_path or "").strip() or "app_data.sqlite3"
    db = DatabaseManager(db_path=db_path)
    db.init_schema()

    user_id_raw = str(args.user_id or "").strip()
    user_id: int | None = None
    if user_id_raw:
        try:
            user_id = int(float(user_id_raw))
        except Exception:
            user_id = None

    def _require_user() -> dict[str, Any]:
        nonlocal user_id
        if not user_id:
            raise RuntimeError("user_id is required (set --user-id or USER_ID env).")
        u = db.get_user(int(user_id))
        if not u:
            raise RuntimeError("User not found in DB.")
        if not (u.get("eth_address") or "").strip():
            raise RuntimeError("User has no eth_address in DB.")
        return u

    def _prompt(msg: str, *, default: str | None = None) -> str:
        if default is not None and default != "":
            q = f"{msg} [{default}]: "
        else:
            q = f"{msg}: "
        try:
            s = input(q).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            raise SystemExit(130)
        return s or (default or "")

    def _prompt_choice(msg: str, choices: list[str], *, default: str | None = None) -> str:
        norm = {c.lower(): c for c in choices}
        while True:
            s = _prompt(msg, default=default).strip()
            if not s:
                continue
            key = s.lower()
            if key in norm:
                return norm[key]
            print(f"Invalid choice. Options: {', '.join(choices)}")

    def _prompt_float(msg: str, *, default: float | None = None) -> float:
        d = f"{default}" if default is not None else None
        while True:
            s = _prompt(msg, default=d).strip()
            try:
                return float(s)
            except Exception:
                print("Please enter a number.")

    def _prompt_int(msg: str, *, default: int | None = None) -> int:
        d = f"{default}" if default is not None else None
        while True:
            s = _prompt(msg, default=d).strip()
            try:
                return int(float(s))
            except Exception:
                print("Please enter an integer.")

    def _interactive() -> int:
        nonlocal user_id
        print("Interactive 5m series order CLI")
        print(f"- DB path: {db_path}")
        if not user_id:
            uid_in = _prompt("Enter user_id to trade as (from users table)", default="").strip()
            try:
                user_id = int(float(uid_in)) if uid_in else None
            except Exception:
                user_id = None

        contains = (getattr(args, "contains", "") or "5m").strip() or "5m"
        max_events = int(getattr(args, "max_events", 5000) or 5000)
        page_size = int(getattr(args, "page_size", 200) or 200)
        max_series = max(1, min(int(getattr(args, "max_series", 200) or 200), 2000))

        # Target BTC 5m and 15m series specifically
        target_series = {"btc-up-or-down-5m", "btc-up-or-down-15m"}

        print("\nFetching BTC 5m and 15m series from Gamma...")
        # Directly fetch events by searching for known BTC 5m/15m patterns
        series_items = []
        seen_series: set[str] = set()

        for series_slug in target_series:
            # Try to find latest event for this series using public search
            try:
                resolved = _resolve_latest_event_for_series_slug(
                    series_slug,
                    max_events=2000,
                    page_size=200,
                    active_only=False
                )
                if resolved and resolved.series_slug not in seen_series:
                    seen_series.add(resolved.series_slug)
                    series_items.append({
                        "series_slug": resolved.series_slug,
                        "series_title": resolved.series_title,
                        "_resolved": resolved,  # Cache the resolved market info
                    })
            except Exception as e:
                print(f"Warning: Could not resolve {series_slug}: {e}", file=sys.stderr)

        if not series_items:
            print("No BTC 5m/15m series discovered.", file=sys.stderr)
            return 2

        while True:
            print("\nAvailable series:")
            for i, it in enumerate(series_items, start=1):
                title = it.get("series_title") or ""
                extra = f" — {title}" if title else ""
                print(f"{i:>3}) {it['series_slug']}{extra}")
            print("  0) Quit")
            sel = _prompt_int("Select a series by number", default=1)
            if sel == 0:
                return 0
            if sel < 1 or sel > len(series_items):
                print("Invalid selection.")
                continue

            # Use cached resolved data if available, otherwise re-resolve
            chosen_item = series_items[sel - 1]
            resolved = chosen_item.get("_resolved")
            if resolved:
                print(f"\nUsing cached market for series: {resolved.series_slug}")
            else:
                chosen = chosen_item["series_slug"]
                print(f"\nResolving latest market for series: {chosen}")
                try:
                    resolved = _resolve_latest_event_for_series_slug(chosen, max_events=2000, page_size=200, active_only=False)
                except Exception as e:
                    print(f"Failed to resolve latest market: {e}", file=sys.stderr)
                    continue

            print(
                json.dumps(
                    {
                        "series_slug": resolved.series_slug,
                        "series_title": resolved.series_title,
                        "event_slug": resolved.event_slug,
                        "condition_id": resolved.condition_id,
                        "end_date": resolved.end_date_iso,
                    },
                    indent=2,
                )
            )

            action = _prompt_choice(
                "Action (info / market / limit / back / quit)",
                ["info", "market", "limit", "back", "quit"],
                default="info",
            )
            if action == "quit":
                return 0
            if action == "back":
                continue

            if action == "info":
                try:
                    info = _get_market_info_by_condition_id(resolved.condition_id)
                    print(json.dumps(info, indent=2))
                except Exception as e:
                    print(f"Info fetch failed: {e}", file=sys.stderr)
                continue

            try:
                u = _require_user()
            except Exception as e:
                print(f"Trading not configured: {e}", file=sys.stderr)
                continue

            side = _prompt_choice("Side", ["YES", "NO"], default="YES")
            order_side = _prompt_choice("Order side (BUY=open / SELL=close)", ["BUY", "SELL"], default="BUY")
            auto_prepare = _prompt_choice("Auto-prepare (swap+approve)?", ["yes", "no"], default="yes") == "yes"

            try:
                if auto_prepare:
                    swap_result = bot_tools.swap_usdc_for_trading(u["eth_address"], amount="all")
                    print(f"swap: {swap_result}")
                    approved_flag = u.get("polymarket_approved") or 0
                    if not approved_flag:
                        approve_result = bot_tools.approve_usdc_for_trading(u["eth_address"])
                        print(f"approve: {approve_result}")
                        ok = not str(approve_result).lstrip().startswith("❌")
                        if ok:
                            with db.transaction() as conn:
                                conn.execute(
                                    "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                                    (int(u["user_id"]),),
                                )
                            u = db.get_user(int(u["user_id"])) or u

                if action == "market":
                    amt = _prompt_float("Amount USD", default=10.0)
                    out = execute_trade_for_user(
                        db=db,
                        user_id=int(u["user_id"]),
                        side=side,
                        amount=float(amt),
                        condition_id=resolved.condition_id,
                        order_side=order_side,
                    )
                    print(out)
                else:
                    price = _prompt_float("Limit price per share (0.01-0.99)", default=0.50)
                    size = _prompt_float("Size (shares)", default=10.0)
                    out = execute_limit_order_for_user(
                        db=db,
                        user_id=int(u["user_id"]),
                        side=side,
                        price=float(price),
                        size=float(size),
                        condition_id=resolved.condition_id,
                        order_side=order_side,
                    )
                    print(out)
            except Exception as e:
                print(f"Order failed: {e}", file=sys.stderr)
                continue

    if args.cmd == "interactive":
        return _interactive()

    if args.cmd == "users":
        lim = max(1, min(int(getattr(args, "limit", 25) or 25), 500))
        rows = db.execute(
            """
            SELECT user_id, username, eth_address, onboarded, polymarket_approved
            FROM users
            WHERE user_id IS NOT NULL
            ORDER BY user_id ASC
            LIMIT ?;
            """,
            (lim,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # trim address for display
            addr = (d.get("eth_address") or "").strip()
            if addr and len(addr) > 14:
                d["eth_address"] = addr[:10] + "..." + addr[-4:]
            out.append(d)
        print(json.dumps({"db_path": db_path, "count": len(out), "users": out}, indent=2))
        return 0

    if args.cmd in ("series", "latest"):
        contains = (getattr(args, "contains", "") or "").strip()
        max_events = int(getattr(args, "max_events", 5000) or 5000)
        page_size = int(getattr(args, "page_size", 200) or 200)
        active = True if getattr(args, "active_only", False) else True
        closed = False

        events = _gamma_iter_events(
            active=active,
            closed=closed,
            order=None,
            ascending=False,
            series_slug_contains=contains or None,
            limit=page_size,
            max_events=max_events,
        )

        by_series: dict[str, dict[str, Any]] = {}
        # Pick the latest event by comparing end_ts locally (do not rely on Gamma ordering).
        for ev in events:
            ss = (ev.get("seriesSlug") or "").strip()
            if not ss:
                continue
            end_iso = _pick_end_date_from_event(ev)
            end_ts = _iso_to_ts(end_iso)
            cur = by_series.get(ss)
            if not cur or int(cur.get("latest_end_ts") or 0) < end_ts:
                by_series[ss] = {
                    "series_slug": ss,
                    "series_title": _pick_series_title_from_event(ev),
                    "latest_event_slug": (ev.get("slug") or "").strip() or None,
                    "latest_condition_id": _pick_condition_id_from_event(ev),
                    "latest_end_date": end_iso,
                    "latest_end_ts": end_ts,
                }

        items = list(by_series.values())
        items.sort(key=lambda x: (x.get("series_slug") or ""))

        if args.cmd == "series":
            out = [{"series_slug": i["series_slug"], "series_title": i.get("series_title")} for i in items]
            print(json.dumps({"contains": contains, "count": len(out), "series": out}, indent=2))
            return 0

        if args.cmd == "latest":
            max_series = max(1, min(int(getattr(args, "max_series", 200) or 200), 2000))
            sliced = items[:max_series]
            print(json.dumps({"contains": contains, "count": len(sliced), "latest": sliced}, indent=2))
            return 0

    if args.cmd == "search":
        items = _gamma_search_events(args.q, limit=max(1, min(int(args.limit), 50)))
        out: list[dict[str, Any]] = []
        for e in items:
            slug = (e.get("slug") or "").strip()
            title = (e.get("title") or e.get("name") or "").strip()
            series_slug = (e.get("seriesSlug") or "").strip() or None
            out.append(
                {
                    "slug": slug,
                    "title": title,
                    "series_slug": series_slug,
                }
            )
        print(json.dumps({"query": args.q, "count": len(out), "events": out}, indent=2))
        return 0

    if args.cmd == "latest-one":
        resolved = _resolve_latest_event_for_series_slug(
            args.series_slug,
            max_events=int(args.max_events or 2000),
            page_size=int(args.page_size or 200),
            active_only=bool(getattr(args, "active_only", False)),
        )
        end_str = None
        if resolved.end_date_iso:
            try:
                end_dt = datetime.fromisoformat(resolved.end_date_iso.replace("Z", "+00:00"))
                end_str = end_dt.isoformat()
            except Exception:
                end_str = resolved.end_date_iso
        print(
            json.dumps(
                {
                    "series_slug": resolved.series_slug,
                    "series_title": resolved.series_title,
                    "event_slug": resolved.event_slug,
                    "condition_id": resolved.condition_id,
                    "end_date": end_str,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd in ("market-latest", "limit-latest"):
        u = _require_user()
        if not bool(getattr(args, "no_auto_prepare", False)):
            swap_result = bot_tools.swap_usdc_for_trading(u["eth_address"], amount="all")
            print(f"swap: {swap_result}")
            approved_flag = u.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(u["eth_address"])
                print(f"approve: {approve_result}")
                ok = not str(approve_result).lstrip().startswith("❌")
                if ok:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                            (int(u["user_id"]),),
                        )
                    u = db.get_user(int(u["user_id"])) or u

        resolved = _resolve_latest_event_for_series_slug(
            args.series_slug,
            max_events=int(args.max_events or 2000),
            page_size=int(args.page_size or 200),
            active_only=bool(getattr(args, "active_only", False)),
        )
        if args.cmd == "market-latest":
            out = execute_trade_for_user(
                db=db,
                user_id=int(u["user_id"]),
                side=args.side,
                amount=float(args.amount_usd),
                condition_id=resolved.condition_id,
                order_side=args.order_side,
            )
            print(json.dumps({"resolved": resolved.__dict__, "result": out}, indent=2))
            return 0
        out = execute_limit_order_for_user(
            db=db,
            user_id=int(u["user_id"]),
            side=args.side,
            price=float(args.price),
            size=float(args.size),
            condition_id=resolved.condition_id,
            order_side=args.order_side,
        )
        print(json.dumps({"resolved": resolved.__dict__, "result": out}, indent=2))
        return 0

    try:
        resolved = _resolve_series_market(args.event)
    except Exception as e:
        msg = str(e) or "resolve failed"
        print(f"ERROR: failed to resolve series event: {msg}", file=sys.stderr)
        print(
            "Tip: paste a Polymarket event URL (https://polymarket.com/event/<slug>) "
            "or run: search --q 'btc 5m' to discover slugs.",
            file=sys.stderr,
        )
        return 2

    if resolved.end_date_iso:
        try:
            end_dt = datetime.fromisoformat(resolved.end_date_iso.replace("Z", "+00:00"))
            end_str = end_dt.isoformat()
        except Exception:
            end_str = resolved.end_date_iso
    else:
        end_str = None

    if args.cmd == "resolve":
        print(
            json.dumps(
                {
                    "event_slug": resolved.event_slug,
                    "series_slug": resolved.series_slug,
                    "series_title": resolved.series_title,
                    "condition_id": resolved.condition_id,
                    "end_date": end_str,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "info":
        info = _get_market_info_by_condition_id(resolved.condition_id)
        print(
            json.dumps(
                {
                    "resolved": {
                        "event_slug": resolved.event_slug,
                        "series_slug": resolved.series_slug,
                        "series_title": resolved.series_title,
                        "condition_id": resolved.condition_id,
                        "end_date": end_str,
                    },
                    "market": info,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "market":
        u = _require_user()
        if not bool(getattr(args, "no_auto_prepare", False)):
            swap_result = bot_tools.swap_usdc_for_trading(u["eth_address"], amount="all")
            print(f"swap: {swap_result}")
            approved_flag = u.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(u["eth_address"])
                print(f"approve: {approve_result}")
                ok = not str(approve_result).lstrip().startswith("❌")
                if ok:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                            (int(u["user_id"]),),
                        )
                    u = db.get_user(int(u["user_id"])) or u
        out = execute_trade_for_user(
            db=db,
            user_id=int(u["user_id"]),
            side=args.side,
            amount=float(args.amount_usd),
            condition_id=resolved.condition_id,
            order_side=args.order_side,
        )
        print(json.dumps({"resolved": resolved.__dict__, "result": out}, indent=2))
        return 0

    if args.cmd == "limit":
        u = _require_user()
        if not bool(getattr(args, "no_auto_prepare", False)):
            swap_result = bot_tools.swap_usdc_for_trading(u["eth_address"], amount="all")
            print(f"swap: {swap_result}")
            approved_flag = u.get("polymarket_approved") or 0
            if not approved_flag:
                approve_result = bot_tools.approve_usdc_for_trading(u["eth_address"])
                print(f"approve: {approve_result}")
                ok = not str(approve_result).lstrip().startswith("❌")
                if ok:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                            (int(u["user_id"]),),
                        )
                    u = db.get_user(int(u["user_id"])) or u
        out = execute_limit_order_for_user(
            db=db,
            user_id=int(u["user_id"]),
            side=args.side,
            price=float(args.price),
            size=float(args.size),
            condition_id=resolved.condition_id,
            order_side=args.order_side,
        )
        print(json.dumps({"resolved": resolved.__dict__, "result": out}, indent=2))
        return 0

    print("Unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

