"""
Global market registry — assigns simple numbered IDs (#1, #2, …) to every
market fetched from Polymarket, and stores all metadata needed for trading.

This module is imported by bot_tools.py.  The cache persists across messages
within the same bot session, so users can say "trade #3 Yes $5" and the bot
will resolve market #3 → full CLOB token IDs → execute.
"""

from dataclasses import dataclass, field

@dataclass
class CachedMarket:
    """Everything needed to display and trade a single binary market."""
    market_id: int               # our simple sequential ID (#1, #2, …)
    question: str                # "Will X happen?"
    event_title: str             # parent event title
    condition_id: str            # Polymarket condition ID
    outcomes: list[str]          # ["Yes", "No"]
    clob_token_ids: list[str]    # matching CLOB token IDs for each outcome
    odds: dict[str, int]         # {"Yes": 72, "No": 28} in cents
    end_date: str                # ISO date string


# ── Global state ──
_cache: dict[int, CachedMarket] = {}
_next_id: int = 1


def clear():
    """Reset the cache (e.g. when fetching fresh markets)."""
    global _cache, _next_id
    _cache.clear()
    _next_id = 1


def add(question: str, event_title: str, condition_id: str,
        outcomes: list[str], clob_token_ids: list[str],
        odds: dict[str, int], end_date: str) -> int:
    """Add a market to the cache and return its assigned ID."""
    global _next_id
    mid = _next_id
    _cache[mid] = CachedMarket(
        market_id=mid,
        question=question,
        event_title=event_title,
        condition_id=condition_id,
        outcomes=outcomes,
        clob_token_ids=clob_token_ids,
        odds=odds,
        end_date=end_date,
    )
    _next_id += 1
    return mid


def get(market_id: int) -> CachedMarket | None:
    """Look up a market by its simple ID."""
    return _cache.get(market_id)


def list_all() -> list[CachedMarket]:
    """Return all cached markets in order."""
    return sorted(_cache.values(), key=lambda m: m.market_id)


def format_market(m: CachedMarket) -> str:
    """Pretty-print one market for Telegram display."""
    odds_parts = [f"{o} @ {m.odds.get(o, '?')}¢" for o in m.outcomes]
    odds_str = " | ".join(odds_parts)
    return f"#{m.market_id}  {m.question}\n     [{odds_str}]"


def format_all() -> str:
    """Pretty-print ALL cached markets."""
    if not _cache:
        return "No markets cached. Ask me to show trending markets first."
    lines = ["📊 **Active Markets** (use #ID to trade)\n"]
    for m in list_all():
        lines.append(format_market(m))
    return "\n".join(lines)
