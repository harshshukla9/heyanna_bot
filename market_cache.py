"""
Global market registry — assigns simple numbered IDs (#1, #2, …) to every
market fetched from Polymarket, and stores all metadata needed for trading.

This module is imported by bot_tools.py.  The cache persists across messages
within the same bot session, so users can say "trade #3 Yes $5" and the bot
will resolve market #3 → full CLOB token IDs → execute.
"""

from dataclasses import dataclass

from py_clob_client.client import ClobClient


@dataclass
class CachedMarket:
    """Everything needed to display and trade a single binary market."""
    market_id: int                # our simple sequential ID (#1, #2, …)
    question: str                 # "Will X happen?"
    event_title: str              # parent event title
    condition_id: str             # Polymarket condition ID
    outcomes: list[str]           # ["Yes", "No"]
    clob_token_ids: list[str]     # matching CLOB token IDs for each outcome
    odds: dict[str, int]          # {"Yes": 72, "No": 28} in cents
    end_date: str                 # ISO date string
    volume_24h: float = 0.0       # 24h volume in USD (if available)
    liquidity: float = 0.0        # liquidity in USD (if available)
    image_url: str | None = None  # Optional market/event image URL
    url: str | None = None        # Optional deep link to Polymarket UI


# ── Global state ──
_cache: dict[int, CachedMarket] = {}
_by_condition: dict[str, CachedMarket] = {}  # Polymarket unique key -> market
_next_id: int = 1


def clear():
    """Reset the cache (e.g. when fetching fresh markets)."""
    global _cache, _by_condition, _next_id
    _cache.clear()
    _by_condition.clear()
    _next_id = 1


def add(
    question: str,
    event_title: str,
    condition_id: str,
    outcomes: list[str],
    clob_token_ids: list[str],
    odds: dict[str, int],
    end_date: str,
    image_url: str | None = None,
    volume_24h: float = 0.0,
    liquidity: float = 0.0,
    url: str | None = None,
) -> int:
    """Add a market to the cache. Uses Polymarket condition_id as unique key; internal id is for display only."""
    global _next_id
    mid = _next_id
    m = CachedMarket(
        market_id=mid,
        question=question,
        event_title=event_title,
        condition_id=condition_id,
        outcomes=outcomes,
        clob_token_ids=clob_token_ids,
        odds=odds,
        end_date=end_date,
        volume_24h=volume_24h,
        liquidity=liquidity,
        image_url=image_url,
        url=url,
    )
    _cache[mid] = m
    if condition_id:
        _by_condition[condition_id] = m
    _next_id += 1
    return mid


def get(market_id: int) -> CachedMarket | None:
    """Look up a market by internal cache ID (legacy). Prefer get_by_condition_id for APIs."""
    return _cache.get(market_id)


def get_by_condition_id(condition_id: str) -> CachedMarket | None:
    """Look up a market by Polymarket unique key (condition_id). Use this as the canonical market id."""
    return _by_condition.get(condition_id) if condition_id else None


def clear_by_condition_id(condition_id: str) -> None:
    """Remove a single market from cache by condition_id (e.g. to force re-fetch with fresh outcomes)."""
    if not condition_id:
        return
    m = _by_condition.pop(condition_id, None)
    if m is not None:
        _cache.pop(m.market_id, None)


def ensure_market_cached(condition_id: str) -> CachedMarket | None:
    """
    If the market is already in cache, return it. Otherwise fetch from Polymarket CLOB
    by condition_id, add to cache, and return. Returns None if fetch fails.
    """
    if not condition_id:
        return None
    m = _by_condition.get(condition_id)
    if m:
        return m
    try:
        client = ClobClient("https://clob.polymarket.com")
        raw = client.get_market(condition_id)
        if not raw or not isinstance(raw, dict):
            return None
        tokens = raw.get("tokens") or []
        if len(tokens) < 2:
            return None
        # Use each token's outcome string when present (supports Jazz/Kings, etc.); only default to Yes/No when missing.
        def _outcome(t: dict, i: int) -> str:
            o = t.get("outcome")
            if o is not None and str(o).strip():
                return str(o).strip()
            return "Yes" if i == 0 else "No"
        outcomes = [_outcome(t, i) for i, t in enumerate(tokens)]
        clob_token_ids = [str(t.get("token_id", "")) for t in tokens]
        odds = {}
        for i, t in enumerate(tokens):
            o = _outcome(t, i)
            p = float(t.get("price") or 0)
            odds[o] = int(round(p * 100))
        question = raw.get("question") or "Unknown"
        event_title = raw.get("market_slug") or question
        end_date = raw.get("end_date_iso") or ""
        image_url = raw.get("image") or raw.get("coverImageUrl") or raw.get("cover_image_url")
        try:
            vol_24h = float(raw.get("volume24hr", 0) or 0)
        except Exception:
            vol_24h = 0.0
        try:
            liquidity = float(raw.get("liquidity", 0) or 0)
        except Exception:
            liquidity = 0.0
        mid = add(
            question=question,
            event_title=event_title,
            condition_id=condition_id,
            outcomes=outcomes,
            clob_token_ids=clob_token_ids,
            odds=odds,
            end_date=end_date,
            image_url=image_url,
            volume_24h=vol_24h,
            liquidity=liquidity,
            url=None,
        )
        return _cache.get(mid)
    except Exception:
        return None


def list_all() -> list[CachedMarket]:
    """Return all cached markets in order."""
    return sorted(_cache.values(), key=lambda m: m.market_id)


def format_all() -> str:
    """
    Pretty-print ALL cached markets in a flat, paged-list style suitable for
    Telegram, similar to:

    1) Question?
    ├ Yes: 53¢ · No: 47¢
    ├ 24h Vol $177K · Liq $2.1M
    └ Exp Nov 7, 2028
    """
    from datetime import datetime

    if not _cache:
        return "No markets cached. Ask me to show trending markets first."

    markets = list_all()
    lines: list[str] = []
    lines.append("🔍 *Markets*\n")

    for idx, m in enumerate(markets, start=1):
        # Odds line
        odds_parts: list[str] = []
        for o in m.outcomes:
            cents = m.odds.get(o)
            if cents is None:
                odds_parts.append(f"{o}: ?")
            else:
                odds_parts.append(f"{o}: {cents}¢")
        odds_str = " · ".join(odds_parts) if odds_parts else "n/a"

        # 24h volume / liquidity (compact formatting)
        def _fmt_usd(v: float) -> str:
            try:
                v = float(v)
            except Exception:
                return "$0"
            if v >= 1_000_000:
                return f"${v/1_000_000:.1f}M"
            if v >= 1_000:
                return f"${v/1_000:.1f}K"
            return f"${v:.0f}"

        vol_str = _fmt_usd(m.volume_24h)
        liq_str = _fmt_usd(m.liquidity)

        # Expiry
        end_raw = m.end_date or ""
        pretty_end = "TBD"
        if end_raw:
            try:
                iso = end_raw
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                dt = datetime.fromisoformat(iso)
                pretty_end = dt.strftime("%b %d, %Y")
            except Exception:
                pretty_end = end_raw

        lines.append(f"{idx}) {m.question}")
        lines.append(f"├ Odds: {odds_str}")
        lines.append(f"├ 24h Vol {vol_str} · Liq {liq_str}")
        if m.url:
            lines.append(f"└ Exp {pretty_end}")
            lines.append(f"   Trade · [View on Polymarket]({m.url})")
        else:
            lines.append(f"└ Exp {pretty_end}")
        lines.append("")  # blank line between markets

    return "\n".join(lines).rstrip()


def format_page(page: int, page_size: int = 5) -> tuple[str, int, int]:
    """
    Pretty-print a single page of cached markets.

    Returns (text, current_page, total_pages).
    """
    from datetime import datetime

    if not _cache:
        return "No markets cached. Ask me to show trending markets first.", 0, 0

    markets = list_all()
    total = len(markets)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    subset = markets[start : start + page_size]

    lines: list[str] = []
    lines.append("🔍 *Markets*\n")

    for offset, m in enumerate(subset, start=start + 1):
        # Odds line
        odds_parts: list[str] = []
        for o in m.outcomes:
            cents = m.odds.get(o)
            if cents is None:
                odds_parts.append(f"{o}: ?")
            else:
                odds_parts.append(f"{o}: {cents}¢")
        odds_str = " · ".join(odds_parts) if odds_parts else "n/a"

        def _fmt_usd(v: float) -> str:
            try:
                v = float(v)
            except Exception:
                return "$0"
            if v >= 1_000_000:
                return f"${v/1_000_000:.1f}M"
            if v >= 1_000:
                return f"${v/1_000:.1f}K"
            return f"${v:.0f}"

        vol_str = _fmt_usd(m.volume_24h)
        liq_str = _fmt_usd(m.liquidity)

        end_raw = m.end_date or ""
        pretty_end = "TBD"
        if end_raw:
            try:
                iso = end_raw
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                dt = datetime.fromisoformat(iso)
                pretty_end = dt.strftime("%b %d, %Y")
            except Exception:
                pretty_end = end_raw

        lines.append(f"{offset}) {m.question}")
        lines.append(f"├ Odds: {odds_str}")
        lines.append(f"├ 24h Vol {vol_str} · Liq {liq_str}")
        if m.url:
            lines.append(f"└ Exp {pretty_end}")
            lines.append(f"   Trade · [View on Polymarket]({m.url})")
        else:
            lines.append(f"└ Exp {pretty_end}")
        lines.append("")

    return "\n".join(lines).rstrip(), page, total_pages
