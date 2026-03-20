import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, User
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from api_app import strip_emoji
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import market_cache
import wallets
import bot_tools
import llm
from database_manager import DatabaseManager
from trading import execute_trade_for_user

# AutoTrader manager for signal trading (from scripts/autotrader.py)
try:
    from autotrader_manager import (
        AutoTraderManager,
        TIMEFRAME_TO_SERIES,
        DEFAULT_TRADE_AMOUNT_USD,
    )
    AUTOTRADER_AVAILABLE = True
except ImportError:
    AUTOTRADER_AVAILABLE = False

# Real-time copy trading tracker
try:
    from copy_trading import get_manager
    COPY_TRACKER_AVAILABLE = True
except ImportError:
    COPY_TRACKER_AVAILABLE = False


logger = logging.getLogger(__name__)
TELEGRAM_BOT_USERNAME = (os.getenv("TELEGRAM_BOT_USERNAME", "") or "").strip()
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
WELCOME_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_2-1e20dfcf-cf72-442b-9a6f-2cca14150217.png")
WALLET_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_3-835e668d-805e-40da-b1dd-ccbe015db247.png")
SMART_WALLETS_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_4-75de99a5-8493-4893-aeda-311c5e1b7d1d.png")
ALL_MARKETS_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_5-dc9439a1-7903-4998-98c8-0749493a9ce4.png")
POLITICS_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_7-bc447baa-48a4-4327-810f-7599f2bad8b1.png")
SPORTS_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_8-f9c1b520-0851-40bf-b0ed-c4317b8e3e87.png")
CRYPTO_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_9-036e6965-55d0-4b8a-aac0-a128fc7ee6b6.png")
TRUMP_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_10-4a8bfa86-900b-49bb-8bd4-733ed6305495.png")
FINANCE_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_11-19eb9c2d-b857-4296-9330-978ba2d2afe4.png")
GEOPOLITICS_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_12-ea42a7ae-7696-448f-aeba-dd6e98b65f9a.png")
VOLUME_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_13-431a6694-39e2-495a-9adb-cc02d95624fc.png")
TRENDING_BANNER_PATH = os.path.join(ASSETS_DIR, "Frame_14-490a3a79-7562-4d21-a257-ed21d1b06488.png")

MARKET_BANNERS: dict[str, str] = {
    "all": ALL_MARKETS_BANNER_PATH,
    "trending": TRENDING_BANNER_PATH,
    "volume": VOLUME_BANNER_PATH,
    "politics": POLITICS_BANNER_PATH,
    "sports": SPORTS_BANNER_PATH,
    "crypto": CRYPTO_BANNER_PATH,
    "trump": TRUMP_BANNER_PATH,
    "finance": FINANCE_BANNER_PATH,
    "geopolitics": GEOPOLITICS_BANNER_PATH,
}

def escape_markdown_v2(text: str) -> str:
    """
    Escape special characters for Telegram MarkdownV2 parse mode.
    Characters that need escaping: _ * [ ] ( ) { } # + - = | { } > < -
    """
    if not text:
        return text
    # List of characters that need escaping in MarkdownV2
    special_chars = r"_\*[](){}~`>#+-=|{}.!"
    result = []
    for char in text:
        if char in special_chars:
            result.append("\\")
        result.append(char)
    return "".join(result)

# In-memory session context for conversation history (User ID -> List of Dict messages)
chat_sessions: Dict[int, List[dict]] = {}

# In-memory per-user active market context so the LLM can reference
# the last market / side selected via buttons or commands.
active_market_context: Dict[int, dict] = {}

# In-memory state for one-off typed inputs (e.g., setting custom signal trade amount).
_pending_signal_amount_input: Dict[int, str] = {}  # user_id -> timeframe ("5m", "15m", or "legacy")

# Telegram callback_data has a strict 64-byte limit. Use short aliases for
# markets and resolve back to condition IDs server-side.
_alias_to_condition: Dict[int, str] = {}
_condition_to_alias: Dict[str, int] = {}
_alias_to_snapshot: Dict[int, dict] = {}
_next_alias_id: int = 1


def _mget(m, key: str, default=None):
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def _alias_for_market(market_obj) -> int:
    """Return a short integer alias for a market condition ID."""
    global _next_alias_id
    condition_id = (_mget(market_obj, "condition_id", "") or "").strip()
    if not condition_id:
        try:
            return int(_mget(market_obj, "market_id", 0) or 0)
        except Exception:
            return 0
    existing = _condition_to_alias.get(condition_id)
    if existing:
        if isinstance(market_obj, dict):
            _alias_to_snapshot[existing] = market_obj
        return existing
    alias = _next_alias_id
    _next_alias_id += 1
    _condition_to_alias[condition_id] = alias
    _alias_to_condition[alias] = condition_id
    if isinstance(market_obj, dict):
        _alias_to_snapshot[alias] = market_obj
    return alias


def _resolve_market_identifier(identifier: str):
    """Resolve callback identifier -> (alias_id|None, condition_id|None, market_obj|None)."""
    alias_id = None
    condition_id = None
    market_obj = None
    try:
        alias_id = int(identifier)
        condition_id = _alias_to_condition.get(alias_id)
        market_obj = _alias_to_snapshot.get(alias_id)
        if not market_obj:
            # Backward compatibility: alias might actually be legacy cache market_id.
            legacy = market_cache.get(alias_id)
            if legacy:
                market_obj = legacy
                condition_id = condition_id or getattr(legacy, "condition_id", None)
        if not market_obj and condition_id:
            market_obj = market_cache.ensure_market_cached(condition_id)
    except ValueError:
        condition_id = identifier
        alias_id = _condition_to_alias.get(condition_id)
        if alias_id is not None:
            market_obj = _alias_to_snapshot.get(alias_id)
        if not market_obj and condition_id:
            market_obj = market_cache.ensure_market_cached(condition_id)
    return alias_id, condition_id, market_obj

# Persistent command UI keyboard (menu-first UX)
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🏠 Main Menu"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# Telegram menu commands (shown when user taps menu button)
BOT_COMMANDS = [
    BotCommand("start", "Initialize wallet & show menu"),
    BotCommand("wallet", "Show your Polygon wallet address"),
    BotCommand("balance", "Check token balances"),
    BotCommand("portfolio", "Funds + open positions & PnL"),
    BotCommand("copy", "Manage copy trading"),
    BotCommand("markets", "Browse trending Polymarket events"),
    BotCommand("trending", "Alias for /markets"),
    BotCommand("category", "Markets by category (politics, crypto, etc.)"),
    BotCommand("approve", "Run gasless approvals for trading"),
    BotCommand("swap", "Swap USDC.e → bridged USDC"),
    BotCommand("close", "How to close positions"),
    BotCommand("menu", "Show command buttons"),
    BotCommand("help", "List all commands"),
]


def _get_user_position_in_market(db: DatabaseManager, user_id: int, market_id: int) -> str:
    """Get user's position summary for a specific market."""
    # Query trades for this user and market
    rows = db.execute(
        """
        SELECT side, SUM(amount) as total_amount, COUNT(*) as trade_count
        FROM trades
        WHERE user_id = ? AND market_id = ?
        GROUP BY side
        """,
        (user_id, market_id),
    ).fetchall()

    if not rows:
        return "None"

    positions = []
    for row in rows:
        side = row["side"]
        total = float(row["total_amount"]) if row["total_amount"] else 0
        positions.append(f"{side}: ${total:.2f}")

    return " | ".join(positions) if positions else "None"

def _format_markets_with_trades(page: int = 1, page_size: int = 5, slug: str = "") -> tuple[str, int, int, list[dict]]:
    """
    Format markets as tree-style text.
    Returns (text, current_page, total_pages, markets).
    """
    from datetime import datetime
    import html

    if slug == "closing":
        markets, total_pages = bot_tools.fetch_polymarket_markets_raw(
            order="endDate",
            ascending=True,
            page=page,
            page_size=page_size,
        )
    elif slug in ("", "trending", "volume"):
        markets, total_pages = bot_tools.fetch_polymarket_markets_raw(
            order="volume24hr",
            ascending=False,
            page=page,
            page_size=page_size,
        )
    else:
        markets, total_pages = bot_tools.fetch_polymarket_markets_raw(
            order="volume24hr",
            ascending=False,
            tag_slug=slug,
            page=page,
            page_size=page_size,
        )

    if not markets:
        return "No active markets found right now.", 1, max(1, total_pages), []

    page = max(1, min(page, total_pages))
    subset = markets

    lines: list[str] = []

    start_index = (page - 1) * page_size
    for offset, m in enumerate(subset, start=start_index + 1):
        # Format odds
        odds = _mget(m, "odds", {}) or {}
        yes_odds = odds.get("Yes", 0)
        no_odds = odds.get("No", 0)

        # Truncate question for readability
        question = _mget(m, "question", "")
        if len(question) > 60:
            question = question[:57] + "..."

        # Expiry
        end_raw = _mget(m, "end_date", "") or ""
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

        # Volume and liquidity
        vol_24h = _mget(m, "volume_24h", 0) or 0
        liq = _mget(m, "liquidity", 0) or 0
        vol_str = f"${vol_24h:,.0f}" if vol_24h >= 1000 else f"${vol_24h:.0f}" if vol_24h > 0 else "—"
        liq_str = f"${liq:,.0f}" if liq >= 1000 else f"${liq:.0f}" if liq > 0 else "—"

        # Prefer market URL for external view; Trade link uses bot deep-link when possible.
        market_url = (_mget(m, "url", "") or "").strip() or "https://polymarket.com"
        alias_id = _alias_for_market(m)
        if TELEGRAM_BOT_USERNAME and alias_id:
            # Keep /start payload short (Telegram enforces strict size limits).
            trade_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=trade_{alias_id}"
        else:
            trade_url = market_url

        # Tree-style market entry with HTML-safe content and a clickable Trade link.
        lines.append(f"{offset}) {html.escape(question)}")
        lines.append(f"├ Yes {yes_odds}¢ · No {no_odds}¢")
        lines.append(f"├ 24h Vol {vol_str} · Liq {liq_str} · Exp {pretty_end}")
        lines.append(f"└ <a href=\"{trade_url}\">Trade</a>")
        lines.append("")

    # Pagination info
    lines.append(f"────────────────────")
    lines.append(f"Page {page}/{max(1, total_pages)}")

    return "\n".join(lines).rstrip(), page, max(1, total_pages), subset


def _build_market_detail_keyboard(market) -> InlineKeyboardMarkup:
    """Build inline keyboard for a single market detail page."""
    odds = _mget(market, "odds", {}) or {}
    yes_odds = odds.get("Yes", 0)
    no_odds = odds.get("No", 0)
    alias_id = _alias_for_market(market)
    identifier = str(alias_id) if alias_id else ((_mget(market, "condition_id", "") or "").strip() or str(_mget(market, "market_id", "")))

    buttons: list[list[InlineKeyboardButton]] = []

    # Buy buttons
    buttons.append([
        InlineKeyboardButton(f"✅ Buy Yes {yes_odds}¢", callback_data=f"trade:open:{identifier}:Yes"),
        InlineKeyboardButton(f"❌ Buy No {no_odds}¢", callback_data=f"trade:open:{identifier}:No")
    ])

    # Analysis and actions
    buttons.append([
        InlineKeyboardButton("📊 Analyze", callback_data=f"analyze:{identifier}"),
        InlineKeyboardButton("📈 View on Polymarket", url=_mget(market, "url", "") or "https://polymarket.com")
    ])

    # Back
    buttons.append([
        InlineKeyboardButton("⬅️ Back to Markets", callback_data="markets:back")
    ])

    return InlineKeyboardMarkup(buttons)


def _build_pagination_keyboard(page: int, total_pages: int, slug: str = "", markets: list[dict] = None) -> InlineKeyboardMarkup:
    """Build inline keyboard with pagination and categories."""
    buttons: list[list[InlineKeyboardButton]] = []

    # Pagination row
    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"markets_page:{slug}:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"markets_page:{slug}:{page+1}"))
    buttons.append(nav_row)

    # Quick browse row
    buttons.append([
        InlineKeyboardButton("🔥 Trending", callback_data="markets:trending"),
        InlineKeyboardButton("📊 Volume", callback_data="markets:volume"),
        InlineKeyboardButton("⏰ Closing Soon", callback_data="markets:closing"),
    ])
    # Categories row
    buttons.append([
        InlineKeyboardButton("🏛️ Politics", callback_data="category:politics"),
        InlineKeyboardButton("⚽ Sports", callback_data="category:sports"),
        InlineKeyboardButton("₿ Crypto", callback_data="category:crypto"),
    ])
    buttons.append([
        InlineKeyboardButton("📈 Finance", callback_data="category:finance"),
        InlineKeyboardButton("🌍 Geopolitics", callback_data="category:geopolitics"),
        InlineKeyboardButton("🤖 AI", callback_data="category:ai"),
    ])

    # Back and Home row
    buttons.append([
        InlineKeyboardButton("⬅️ Markets Hub", callback_data="markets:back"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="home:main"),
    ])

    return InlineKeyboardMarkup(buttons)


def _build_markets_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔥 Trending", callback_data="markets:trending"),
                InlineKeyboardButton("📊 Volume", callback_data="markets:volume"),
            ],
            [
                InlineKeyboardButton("⏰ Closing Soon", callback_data="markets:closing"),
                InlineKeyboardButton("🧭 Categories", callback_data="markets:category"),
            ],
            [
                InlineKeyboardButton("🏠 Main Menu", callback_data="home:main"),
            ],
        ]
    )


def _build_categories_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏛️ Politics", callback_data="category:politics"),
                InlineKeyboardButton("₿ Crypto", callback_data="category:crypto"),
            ],
            [
                InlineKeyboardButton("📈 Finance", callback_data="category:finance"),
                InlineKeyboardButton("⚽ Sports", callback_data="category:sports"),
            ],
            [
                InlineKeyboardButton("🌍 Geopolitics", callback_data="category:geopolitics"),
                InlineKeyboardButton("🧢 Trump", callback_data="category:trump"),
            ],
            [
                InlineKeyboardButton("🤖 AI", callback_data="category:ai"),
            ],
            [
                InlineKeyboardButton("⬅️ Markets Hub", callback_data="markets:back"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="home:main"),
            ],
        ]
    )


def create_telegram_application(db: DatabaseManager, bot_token: str) -> Application:
    """
    Build and return a configured Telegram Application instance.
    All DB access goes through the shared DatabaseManager.
    """

    async def _send_long_message(bot, chat_id: int, text: str, parse_mode: str | None = None):
        """Safely send long texts by splitting to respect Telegram's message length limit."""
        if not text:
            return
        max_len = 4000  # slightly under Telegram's hard limit
        remaining = text
        first = True
        while remaining:
            if len(remaining) <= max_len:
                chunk = remaining
                remaining = ""
            else:
                # Try to split on paragraph / line / space boundaries
                split_at = remaining.rfind("\n\n", 0, max_len)
                if split_at == -1:
                    split_at = remaining.rfind("\n", 0, max_len)
                if split_at == -1:
                    split_at = remaining.rfind(" ", 0, max_len)
                if split_at == -1 or split_at < max_len // 2:
                    split_at = max_len
                chunk = remaining[:split_at]
                remaining = remaining[split_at:].lstrip()
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)

    async def _send_banner_with_caption(
        bot,
        chat_id: int,
        image_path: str,
        caption: str,
        parse_mode: str | None = None,
        reply_markup=None,
        max_caption_len: int | None = None,
    ):
        """Send one embedded image+caption message; fallback to text-only message."""
        final_caption = caption or ""
        effective_parse_mode = parse_mode
        if max_caption_len and len(final_caption) > max_caption_len:
            # Keep HTML links clickable by truncating on entry boundaries.
            if parse_mode == "HTML":
                cut = final_caption.rfind("\n\n", 0, max_caption_len - 2)
                if cut < 0:
                    cut = final_caption.rfind("\n", 0, max_caption_len - 2)
                if cut < 0:
                    cut = max_caption_len - 2
                final_caption = final_caption[:cut].rstrip() + "\n\n…"
                effective_parse_mode = "HTML"
            else:
                final_caption = final_caption[: max_caption_len - 1] + "…"
                # Truncation can cut Markdown entities mid-token; disable parse mode.
                effective_parse_mode = None
        image_ref = (image_path or "").strip()
        is_remote_image = image_ref.startswith("http://") or image_ref.startswith("https://")
        try:
            if is_remote_image:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_ref,
                    caption=final_caption,
                    parse_mode=effective_parse_mode,
                    reply_markup=reply_markup,
                )
                return
            if image_ref and os.path.exists(image_ref):
                with open(image_ref, "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=final_caption,
                        parse_mode=effective_parse_mode,
                        reply_markup=reply_markup,
                    )
                    return
        except Exception:
            # Retry without parse mode if Telegram rejects entities.
            try:
                if is_remote_image:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=image_ref,
                        caption=final_caption,
                        parse_mode=None,
                        reply_markup=reply_markup,
                    )
                    return
                if image_ref and os.path.exists(image_ref):
                    with open(image_ref, "rb") as f:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=final_caption,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
                        return
            except Exception:
                pass
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=final_caption,
                parse_mode=effective_parse_mode,
                reply_markup=reply_markup,
            )
        except Exception:
            await bot.send_message(
                chat_id=chat_id,
                text=final_caption,
                parse_mode=None,
                reply_markup=reply_markup,
            )

    def _market_banner_for_slug(slug: str) -> str:
        s = (slug or "").strip().lower()
        if not s:
            return MARKET_BANNERS["all"]
        if s in MARKET_BANNERS:
            return MARKET_BANNERS[s]
        if "trump" in s:
            return MARKET_BANNERS["trump"]
        if "geo" in s:
            return MARKET_BANNERS["geopolitics"]
        return MARKET_BANNERS["all"]

    def _market_banner_for_market(market) -> str:
        """Pick a banner for a market object/dict based on category-like hints."""
        slug = (
            _mget(market, "slug", "")
            or _mget(market, "market_slug", "")
            or _mget(market, "category", "")
            or _mget(market, "event_slug", "")
            or ""
        )
        return _market_banner_for_slug(slug)

    def _market_image_for_market(market) -> str:
        """Use Gamma market image URL when available, else fallback banner."""
        image_url = (
            _mget(market, "image_url", "")
            or _mget(market, "image", "")
            or _mget(market, "icon", "")
            or ""
        )
        if isinstance(image_url, str) and image_url.strip().startswith(("http://", "https://")):
            return image_url.strip()
        return _market_banner_for_market(market)

    async def _safe_edit_message(query, text, parse_mode=None, reply_markup=None, **_ignored_kwargs):
        """Safely edit text/caption messages, with graceful fallback."""
        try:
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except BadRequest as e:
            msg = str(e)
            if "Message is not modified" in msg:
                return
            if "There is no text in the message to edit" in msg:
                try:
                    await query.edit_message_caption(
                        caption=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                    return
                except BadRequest as e2:
                    if "Message is not modified" in str(e2):
                        return
                except Exception:
                    pass
                # Last resort: send as a fresh message.
                try:
                    if query.message:
                        await query.message.reply_text(
                            text,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                        )
                        return
                except Exception:
                    pass
            raise

    def _friendly_error_message(err: Exception) -> str | None:
        """
        Map low-level exceptions to actionable user-facing messages.
        Return None for ignorable errors.
        """
        if err is None:
            return "Something went wrong while handling that action. Please try again."

        msg = str(err)
        low = msg.lower()

        # Telegram API specific noise that users shouldn't see.
        if isinstance(err, BadRequest) and "message is not modified" in low:
            return None

        # Telegram edit/caption quirks.
        if "there is no text in the message to edit" in low:
            return (
                "That menu card cannot be edited in place. Please tap the button again, "
                "or open /menu and retry."
            )
        if "can't parse entities" in low:
            return "Message formatting failed. Please retry; if this keeps happening, refresh the menu."

        # Rate limits and transient network issues.
        if isinstance(err, RetryAfter) or "too many requests" in low:
            return "Too many requests at once. Please wait a few seconds and try again."
        if isinstance(err, (TimedOut, NetworkError)) or "timed out" in low:
            return "The request timed out. Please retry in a few seconds."
        if "connection" in low and ("reset" in low or "aborted" in low or "closed" in low):
            return "Network connection dropped. Please try again."

        # Permissions / bot access.
        if isinstance(err, Forbidden) or "forbidden" in low:
            return "I don’t have permission to complete that action in this chat."

        # Trading and relayer errors users can act on.
        if "allowance" in low or "not enough balance" in low:
            return "Insufficient balance/allowance. Try /approve, then fund your trading wallet and retry."
        if "expected safe" in low and "not deployed" in low:
            return "Your trading Safe is not deployed yet. Run /approve once, then retry."
        if "no orderbook exists for the requested token id" in low:
            return "That market is not currently tradable from the orderbook. Try another market."
        if "market not found" in low:
            return "Market data is stale. Please refresh markets and try again."

        # Generic fallback.
        return "Something went wrong while handling that action. Please try again."

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /start command to initialize a user or handle deep links."""
        user = update.effective_user
        db_user = db.get_user(user.id)

        # Handle deep link arguments: e.g., /start trade_CONDITIONID_Yes or /start invite_CODE
        args = context.args
        if args and len(args) > 0:
            deep_link = args[0]
            if deep_link.startswith("trade_"):
                # Parse: trade_CONDITIONID
                condition_id = deep_link[6:]  # Remove "trade_" prefix
                if condition_id:
                    # Redirect to trade flow
                    await _initiate_trade_from_deep_link(update, context, condition_id, db_user)
                    return
            if deep_link.startswith("follow_"):
                # Parse: follow_0x...
                wallet = deep_link[7:].strip()
                if db_user and re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
                    context.user_data["pending_follow_wallet"] = wallet
                    context.user_data["pending_follow_name"] = _refetch_polymarket_username(wallet) or wallet
                    context.user_data["awaiting_follow_risk"] = True
                    await _send_follow_risk_menu(update.message, wallet)
                    return
            if deep_link.startswith("invite_"):
                # Parse: invite_CODE - handle invite code redemption
                invite_code = deep_link[7:].strip().upper()
                if not db_user:
                    # User doesn't exist yet, create and onboard
                    await _handle_new_user_with_invite(update, context, invite_code, user)
                    return
                else:
                    # Existing user trying to use invite code
                    await _handle_existing_user_with_invite(update, context, invite_code, db_user)
                    return

        # Create user if doesn't exist
        if not db_user:
            await _send_banner_with_caption(
                context.bot,
                update.effective_chat.id,
                WELCOME_BANNER_PATH,
                "Welcome! Generating your Ethereum/Polygon wallet for Polymarket. Please wait a moment...",
            )
            eth_wallet = wallets.generate_eth_wallet()

            db.create_user(
                user_id=user.id,
                username=user.username or "",
                eth_data=eth_wallet,
                sol_data=("", ""),
            )
            db_user = db.get_user(user.id)

        # Check if user is onboarded
        is_onboarded, _ = db.get_user_onboarding_status(user.id)
        if not is_onboarded:
            # Show invite code input button
            button = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔑 Enter Invite Code", callback_data="enter_invite_code")
            ]])
            await _send_banner_with_caption(
                context.bot,
                update.effective_chat.id,
                WELCOME_BANNER_PATH,
                f"Welcome, {user.first_name}!\n\n"
                f"🔒 Your account needs to be activated with an invite code.\n\n"
                f"Click below to enter your invite code:",
                parse_mode="Markdown",
                reply_markup=button,
            )
            return

        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            WELCOME_BANNER_PATH,
            f"Welcome back {user.first_name}! Wallet: `{db_user['eth_address']}`\n\n"
            f"Tap **Main Menu** below to get started.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        await _send_home(update, context, db_user)

    async def _handle_new_user_with_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, invite_code: str, user: User):
        """Handle new user registration with invite code."""
        # Validate invite code
        is_valid, message = db.validate_invite_code(invite_code)
        if not is_valid:
            await _send_banner_with_caption(
                context.bot,
                update.effective_chat.id,
                WELCOME_BANNER_PATH,
                f"❌ {message}\n\n"
                "Please get a valid invite code and try again.\n\n"
                "Use: `/join <CODE>`",
                parse_mode="Markdown",
            )
            return

        # Create user with onboarded status
        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            WELCOME_BANNER_PATH,
            "🔐 Validating invite code...",
        )

        eth_wallet = wallets.generate_eth_wallet()

        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username,
                    eth_address, eth_private_key,
                    sol_address, sol_private_key,
                    onboarded, invite_code
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?);
                """,
                (
                    user.id,
                    user.username or "",
                    eth_wallet["address"],
                    db._encrypt_secret(eth_wallet["private_key"]),
                    "",
                    db._encrypt_secret(""),
                    invite_code.upper(),
                ),
            )

        db_user = db.get_user(user.id)

        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            WELCOME_BANNER_PATH,
            f"✅ Welcome, {user.first_name}!\n\n"
            f"Invite code accepted. Wallet generated.\n\n"
            f"Polygon address:\n`{db_user['eth_address']}`\n\n"
            f"Tap **Main Menu** below to get started.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        await _send_home(update, context, db_user)

    async def _handle_existing_user_with_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, invite_code: str, db_user: dict):
        """Handle existing unonboarded user with invite code."""
        # Check if already onboarded
        is_onboarded, _ = db.get_user_onboarding_status(db_user["user_id"])
        if is_onboarded:
            await update.message.reply_text(
                "You are already onboarded. Use `/menu` to access the bot.",
                parse_mode="Markdown",
            )
            return

        # Validate and claim invite code
        success, message = db.claim_invite_code(invite_code, db_user["user_id"])
        if not success:
            await update.message.reply_text(
                f"❌ {message}\n\nTry another invite code.",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            f"✅ Successfully onboarded! You can now use the bot.\n\n"
            f"Use `/menu` to get started.",
            parse_mode="Markdown",
        )

    async def _on_invite_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Callback for entering invite code."""
        query = update.callback_query
        await query.answer()

        db_user = db.get_user(query.from_user.id)
        if not db_user:
            await query.edit_message_text("Please run /start first.")
            return

        # Check if already onboarded
        is_onboarded, _ = db.get_user_onboarding_status(db_user["user_id"])
        if is_onboarded:
            await query.edit_message_text("You are already onboarded. Use /menu to access the bot.")
            return

        # Ask for invite code - send a new message instead of editing (more reliable)
        button = InlineKeyboardMarkup([[
            InlineKeyboardButton("Cancel", callback_data="cancel_invite_code")
        ]])

        try:
            await query.edit_message_text(
                "🔑 Please enter your invite code:\n\n"
                "Type your invite code and send, or /cancel to go back.",
                reply_markup=button,
            )
        except Exception:
            # If edit fails (e.g., media message), send a new message instead
            await query.message.reply_text(
                "🔑 Please enter your invite code:\n\n"
                "Type your invite code and send, or /cancel to go back.",
                reply_markup=button,
            )

        context.user_data["awaiting_invite_code"] = True

    async def _on_cancel_invite_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Callback for canceling invite code entry."""
        query = update.callback_query
        await query.answer()

        # Clear awaiting state
        context.user_data.pop("awaiting_invite_code", None)

        # Try to edit, fallback to sending new message
        try:
            await query.edit_message_text(
                "Cancelled. Use /start to begin again."
            )
        except Exception:
            await query.message.reply_text(
                "Cancelled. Use /start to begin again."
            )

    async def _handle_invite_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle invite code text input."""
        user = update.effective_user
        message = update.message

        # Check if user is in awaiting invite code state
        if not context.user_data.get("awaiting_invite_code"):
            # Not awaiting, let it pass to normal message handler
            return await handle_message(update, context)

        # Get the invite code
        invite_code = message.text.strip()

        # Validate and claim invite code
        user_id = user.id
        db_user = db.get_user(user_id)

        if not db_user:
            await message.reply_text("Please run /start first to set up your account.")
            context.user_data.pop("awaiting_invite_code", None)
            return

        # Check if already onboarded
        is_onboarded, _ = db.get_user_onboarding_status(user_id)
        if is_onboarded:
            await message.reply_text("You are already onboarded. Use /menu to access the bot.")
            context.user_data.pop("awaiting_invite_code", None)
            return

        # Try to claim the invite code
        success, msg = db.claim_invite_code(invite_code, user_id)
        # Always clear state after processing
        context.user_data.pop("awaiting_invite_code", None)

        if success:
            await message.reply_text(
                f"✅ Successfully onboarded! You can now use the bot.\n\n"
                f"Use `/menu` to get started.",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                f"❌ Invalid invite code: {msg}\n\n"
                f"Try again or /cancel to abort."
            )

    async def _initiate_trade_from_deep_link(update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str, db_user: dict | None):
        """Handle trade deep link: fetch market and show detailed trading menu."""
        if not db_user:
            await update.message.reply_text(
                "Please run `/start` first to set up your wallet before trading.",
                parse_mode="Markdown",
            )
            return

        # Resolve deep-link identifier using the same alias logic as callbacks.
        market_id, condition_id, market = _resolve_market_identifier(identifier)

        if not market:
            await update.message.reply_text(
                f"Market not found. Please try again later.",
                parse_mode="Markdown",
            )
            return

        # Set active market context for this user
        user_id = update.effective_user.id
        ctx_id = market_id if market_id else condition_id
        if user_id in chat_sessions:
            chat_sessions[user_id].append({
                "role": "assistant",
                "content": f"ACTIVE MARKET CONTEXT: {_mget(market, 'question', '')} | ID: {ctx_id}"
            })

        # Build detailed trading menu
        odds = _mget(market, "odds", {}) or {}
        yes_odds = odds.get("Yes", 0)
        no_odds = odds.get("No", 0)

        # Expiry
        from datetime import datetime
        end_raw = _mget(market, "end_date", "") or ""
        pretty_end = "TBD"
        if end_raw:
            try:
                iso = end_raw
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                dt = datetime.fromisoformat(iso)
                pretty_end = dt.strftime("%B %d, %Y")
            except Exception:
                pretty_end = end_raw

        callback_id = str(market_id) if market_id is not None else (condition_id or identifier)
        details = (
            f"📊 *{_mget(market, 'question', '')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔹 *Odds*\n"
            f"   ✅ Yes: {yes_odds}¢ ({yes_odds}%)\n"
            f"   ❌ No: {no_odds}¢ ({no_odds}%)\n\n"
            f"🔹 *Volume*\n"
            f"   24h: ${float(_mget(market, 'volume_24h', 0) or 0):,.0f}\n"
            f"   Liquidity: ${float(_mget(market, 'liquidity', 0) or 0):,.0f}\n\n"
            f"🔹 *Expires*\n"
            f"   {pretty_end}\n\n"
            f"🔹 *Market ID*\n"
            f"   `{callback_id}`\n"
        )

        # Build trading keyboard with amount quick-select
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ Buy Yes (${yes_odds/100:.2f}/share)", callback_data=f"trade:{callback_id}:Yes"),
                InlineKeyboardButton(f"❌ Buy No (${no_odds/100:.2f}/share)", callback_data=f"trade:{callback_id}:No"),
            ],
            [
                InlineKeyboardButton("💵 Enter Amount", callback_data=f"trade_input:{callback_id}"),
            ],
            [
                InlineKeyboardButton("🤖 AI Analysis", callback_data=f"analyze:{callback_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ Back to Markets", callback_data="markets:back"),
            ],
        ])

        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            _market_image_for_market(market),
            details,
            parse_mode="Markdown",
            reply_markup=keyboard,
            max_caption_len=1000,
        )

    def _build_home_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔍 Markets", callback_data="home:markets"),
                    InlineKeyboardButton("🧠 Copy Trade", callback_data="home:copy"),
                ],
                [
                    InlineKeyboardButton("💼 Portfolio", callback_data="home:portfolio"),
                    InlineKeyboardButton("📡 Signal Trading", callback_data="home:signals"),
                ],
                [
                    InlineKeyboardButton("💰 Wallet", callback_data="home:wallet"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="home:refresh"),
                ],
                [
                    InlineKeyboardButton("⚙️ Settings", callback_data="home:settings"),
                    InlineKeyboardButton(
                        "💬 Community",
                        url="https://t.me/+i9D5bDox8lNmNDk9",
                    ),
                ],
            ]
        )

    def _build_home_text(db_user: dict) -> str:
        """Build home screen with real portfolio data."""
        address = db_user["eth_address"]
        trading_addr = bot_tools._get_trading_wallet_address(address)

        # Fetch portfolio data
        try:
            portfolio_text, positions = bot_tools.get_polymarket_portfolio_with_positions(address)
            # Parse the portfolio text for summary values
            lines = portfolio_text.split("\n")

            # Extract key metrics from portfolio text
            positions_count = len(positions) if positions else 0
            portfolio_value = 0.0

            for line in lines:
                if "Total Portfolio Value" in line:
                    match = re.search(r'\$(\d+\.?\d*)', line)
                    if match:
                        portfolio_value = float(match.group(1))

            # Build concise home summary
            summary = (
                "🏠 *Anna Dashboard*\n"
                f"📈 Open exposure: *${portfolio_value:.2f}* across *{positions_count}* position(s)\n"
                "💰 Portfolio & wallet has full balances, PnL, deposits, and withdrawals\n\n"
                "Use the menu below to trade, monitor risk, and manage copy-trading."
            )
            return summary
        except Exception as e:
            # Fallback if portfolio fetch fails
            return (
                "🏠 *Anna Dashboard*\n"
                "📈 Open exposure: fetching...\n"
                f"⚠️ Portfolio data unavailable ({str(e)[:30]}...)\n\n"
                "Use the menu below to continue trading while balances refresh."
            )

    async def _send_home(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: dict):
        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            WELCOME_BANNER_PATH,
            _build_home_text(db_user),
            parse_mode="Markdown",
            reply_markup=_build_home_keyboard(),
            max_caption_len=1000,
        )

    async def _edit_to_home(query, db_user: dict):
        await _safe_edit_message(
            query,
            _build_home_text(db_user),
            parse_mode="Markdown",
            reply_markup=_build_home_keyboard(),
        )

    async def _send_account_overview(chat_id: int, bot, db_user: dict):
        """Unified 'My Account' view: wallet + on-chain funds + open positions."""
        address = db_user["eth_address"]
        # Compute the user's gasless trading Safe (proxy) address for deposits.
        safe_address = await asyncio.to_thread(
            bot_tools.get_safe_address_for_user,
            address,
        )
        # Reuse existing portfolio helper for summary + positions
        portfolio_text, _positions = await asyncio.to_thread(
            bot_tools.get_polymarket_portfolio_with_positions,
            address,
        )
        # Basic account actions as inline buttons
        account_buttons = [
            [
                InlineKeyboardButton("➕ Deposit", callback_data="deposit:wallet"),
                InlineKeyboardButton("🔁 EOA → Safe", callback_data="transfer:safe"),
                InlineKeyboardButton("➖ Withdraw", callback_data="withdraw:funds"),
            ],
            [
                InlineKeyboardButton("📈 View / close positions", callback_data="portfolio:view"),
            ],
        ]

        keyboard = InlineKeyboardMarkup(account_buttons)
        # Add a clearer account header and action hints.
        portfolio_text = (
            "💼 *Portfolio & Wallet*\n"
            "Track your open positions, balances, and cashout actions in one place.\n\n"
            + portfolio_text
        )
        # If we know the Safe address, append a short note so users know where to deposit.
        if safe_address:
            portfolio_text = (
                portfolio_text
                + "\n\n"
                + "⚙️ *Gasless trading wallet (Safe)*\n"
                + f"`{safe_address[:20]}...`\n"
                + "Use this address for deposits.\n\n"
                + "💎 *Your Wallet (EOA)*\n"
                + f"`{address[:20]}...`\n"
                + "Your primary wallet address."
            )
        else:
            portfolio_text = (
                portfolio_text
                + "\n\n"
                + "💎 *Your Wallet (EOA)*\n"
                + f"`{address[:20]}...`\n"
                + "Your primary wallet address."
            )
        portfolio_text = (
            portfolio_text
            + "\n\n"
            + "Tip: Use *Deposit* to fund, *EOA → Safe* to move funds, and *Withdraw* to cash out."
        )
        await _send_banner_with_caption(
            bot,
            chat_id,
            WALLET_BANNER_PATH,
            portfolio_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def _send_safe_balance(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: dict):
        """
        Show the deposit / trading address (Safe wallet when available) and its Polygon balance.
        """
        user_eoa = db_user["eth_address"]
        # Compute Safe / proxy wallet (deposit address) if configured.
        safe_address = await asyncio.to_thread(
            bot_tools.get_safe_address_for_user,
            user_eoa,
        )
        deposit_address = safe_address or user_eoa

        balance = await asyncio.to_thread(
            bot_tools.get_polygon_balance_json,
            deposit_address,
        )

        tokens = balance.get("tokens") or []
        total_usd = float(balance.get("total_usd", 0.0) or 0.0)

        lines: list[str] = []
        lines.append("💰 *Wallet Balance*")
        lines.append("")

        if safe_address and safe_address != user_eoa:
            lines.append("Primary wallet (EOA signer):")
            lines.append(f"`{user_eoa}`")
            lines.append("")
            lines.append("Trading / deposit wallet (Safe):")
            lines.append(f"`{deposit_address}`")
            lines.append("")
            lines.append(
                "Your Safe balance is what Anna uses for Polymarket trades. "
                "Fund the Safe address above."
            )
        else:
            lines.append("Trading / deposit wallet:")
            lines.append(f"`{deposit_address}`")

        if not tokens:
            lines.append("")
            lines.append("No Polygon tokens detected yet in your trading wallet.")
        else:
            lines.append("")
            lines.append("Token balances:")
            for t in tokens:
                try:
                    sym = t.get("symbol", "UNKNOWN")
                    bal = float(t.get("balance", 0) or 0)
                    usd = float(t.get("usd_value", 0) or 0)
                except (TypeError, ValueError):
                    continue
                lines.append(f"• {sym}: {bal:.4f} (${usd:.2f})")
            lines.append("")
            lines.append(f"Estimated total: *${total_usd:.2f}*")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    _leader_stats_cache: dict[str, tuple[float, dict[str, float | int | None]]] = {}
    _LEADER_STATS_TTL_SEC = 90.0

    def _compute_wallet_24h_stats(wallet: str) -> dict[str, float | int | None]:
        """
        Compute wallet-level 24h stats from public Polymarket endpoints.
        Returns:
          - trades_24h: int
          - roi_24h: float | None (realized ROI based on closed positions cost basis)
        """
        import requests

        addr = (wallet or "").strip().lower()
        if not addr.startswith("0x") or len(addr) != 42:
            return {"trades_24h": 0, "roi_24h": None}

        now_ts = int(time.time())
        since_ts = now_ts - 24 * 60 * 60

        # 1) Trades in last 24h (paged).
        trades_24h = 0
        page_size = 100
        max_pages = 5
        for page in range(max_pages):
            offset = page * page_size
            try:
                r = requests.get(
                    "https://data-api.polymarket.com/trades",
                    params={"user": addr, "limit": page_size, "offset": offset, "takerOnly": "true"},
                    timeout=8,
                )
                rows = r.json() if r.status_code == 200 else []
                if not isinstance(rows, list) or not rows:
                    break
            except Exception:
                break

            older_seen = False
            for t in rows:
                try:
                    ts = int(t.get("timestamp") or 0)
                except Exception:
                    ts = 0
                if ts >= since_ts:
                    trades_24h += 1
                else:
                    older_seen = True
            if len(rows) < page_size or older_seen:
                break

        # 2) Realized ROI over closed positions in last 24h.
        realized_pnl = 0.0
        total_bought = 0.0
        try:
            r = requests.get(
                "https://data-api.polymarket.com/closed-positions",
                params={"user": addr, "limit": 200},
                timeout=8,
            )
            closed_rows = r.json() if r.status_code == 200 else []
        except Exception:
            closed_rows = []

        if isinstance(closed_rows, list):
            for p in closed_rows:
                try:
                    ts = int(p.get("timestamp") or 0)
                except Exception:
                    ts = 0
                if ts < since_ts:
                    continue
                try:
                    realized_pnl += float(p.get("realizedPnl") or 0.0)
                except Exception:
                    pass
                try:
                    total_bought += float(p.get("totalBought") or 0.0)
                except Exception:
                    pass

        roi_24h = (realized_pnl / total_bought * 100.0) if total_bought > 0 else None
        return {"trades_24h": int(trades_24h), "roi_24h": roi_24h}

    def _get_wallet_24h_stats_cached(wallet: str) -> dict[str, float | int | None]:
        now = time.time()
        key = (wallet or "").strip().lower()
        cached = _leader_stats_cache.get(key)
        if cached:
            ts, payload = cached
            if (now - ts) <= _LEADER_STATS_TTL_SEC:
                return payload
        payload = _compute_wallet_24h_stats(key)
        _leader_stats_cache[key] = (now, payload)
        return payload

    async def _send_copy_trading_state(
        chat_id: int,
        bot,
        db_user: dict,
        leaderboard_page: int = 1,
        query=None,
    ):
        """
        Show high-level copy-trading status for the current user, mirroring /me/copy-trading from the API.
        Shows both local leaders and global (external wallet) leaders.
        """
        user_id = db_user["user_id"]

        enabled = bool(db_user.get("copy_trading_enabled") or 0)

        # Query hooks directly (handles both local and global leaders)
        rows = db.execute(
            """
            SELECT
                h.id,
                h.follower_user_id,
                h.leader_address,
                h.config as hook_config
            FROM copy_hooks h
            WHERE h.follower_user_id = ? AND h.enabled = 1;
            """,
            (user_id,),
        ).fetchall()

        leaders: list[dict] = []
        for r in rows:
            row_dict = dict(r)
            hook_config = row_dict.get("config") or "{}"
            try:
                config = json.loads(hook_config) if isinstance(hook_config, str) else hook_config
            except Exception:
                config = {}

            # Global hook - external wallet address
            leader_address = row_dict.get("leader_address") or config.get("leader_address")
            if leader_address:
                display_name = config.get("display_name")
                if not display_name or str(display_name).lower() == str(leader_address).lower():
                    display_name = _fetch_polymarket_username(leader_address) or leader_address[:10] + "..."
                # Global leader - external wallet
                leaders.append(
                    {
                        "user_id": user_id,
                        "username": display_name,
                        "eth_address": leader_address,
                        "copy_trading_enabled": enabled,
                        "global": True,
                        "hook_id": int(r["id"]),
                    }
                )

        following_count = len(leaders)

        import html as _html

        lines: list[str] = []
        lines.append("<b>👥 Copy Trading</b>")
        lines.append("")
        lines.append(f"Status: {'✅ Enabled' if enabled else '⚪️ Disabled'}")
        lines.append(f"Following: <b>{following_count}</b> leader wallet(s)")

        # Build buttons for each followed leader
        unfollow_buttons = []
        if leaders:
            lines.append("")
            lines.append("Your followed leaders:")
            for leader in leaders[:5]:  # Show first 5 with unfollow buttons
                name = leader["username"] or leader["eth_address"]
                addr = leader["eth_address"]
                flag = "✅" if leader["copy_trading_enabled"] else "⚪️"
                hook_id = leader.get("hook_id")
                if hook_id:
                    safe_name = _html.escape(str(name))
                    safe_addr = _html.escape(str(addr))
                    profile_url = f"https://polymarket.com/profile/{addr}"
                    lines.append(f"{flag} <code>{safe_addr[:12]}...</code> ({safe_name})")
                    lines.append(f"└ <a href=\"{profile_url}\">Profile</a>")
                    unfollow_buttons.append(
                        [InlineKeyboardButton(
                            f"🚫 Unfollow {addr[:10]}...",
                            callback_data=f"copyunfollow:{hook_id}"
                        )]
                    )
            if following_count > 5:
                lines.append(f"...and {following_count - 5} more.")

        # Global trader leaderboard (Polymarket Data API) with pagination.
        leaderboard_page = max(1, int(leaderboard_page or 1))
        leaderboard_page_size = 5
        leaderboard_offset = (leaderboard_page - 1) * leaderboard_page_size
        try:
            import requests

            resp = requests.get(
                "https://data-api.polymarket.com/v1/leaderboard",
                params={
                    "limit": leaderboard_page_size + 1,
                    "offset": leaderboard_offset,
                    "category": "OVERALL",
                    "timePeriod": "DAY",
                    "orderBy": "PNL",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                entries = data if isinstance(data, list) else data.get("entries", [])
            else:
                entries = []
        except Exception:
            entries = []
        has_prev_page = leaderboard_page > 1
        has_next_page = len(entries) > leaderboard_page_size
        entries = entries[:leaderboard_page_size]

        if entries:
            lines.append("")
            lines.append(f"<b>🌍 Top Global Traders (24h) · Page {leaderboard_page}</b>")
            for e in entries:
                name = e.get("userName") or e.get("proxyWallet", "")[:10] + "…"
                pnl = float(e.get("pnl", 0) or 0)
                vol = float(e.get("vol", 0) or 0)
                wallet = e.get("proxyWallet") or ""
                stats = await asyncio.to_thread(_get_wallet_24h_stats_cached, wallet)
                roi = stats.get("roi_24h")
                num_trades = int(stats.get("trades_24h") or 0)
                safe_name = _html.escape(str(name))
                safe_wallet = _html.escape(str(wallet))
                profile_url = f"https://polymarket.com/profile/{wallet}" if wallet else "https://polymarket.com"
                if TELEGRAM_BOT_USERNAME and wallet:
                    follow_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=follow_{wallet}"
                    action_links = f"<a href=\"{follow_url}\">Follow</a> · <a href=\"{profile_url}\">Profile</a>"
                else:
                    action_links = f"<a href=\"{profile_url}\">Profile</a>"
                roi_text = f"{float(roi):+.1f}%" if roi is not None else "N/A"
                lines.append(f"{safe_name}")
                lines.append(f"├ ROI {roi_text} · Trades: {num_trades}")
                lines.append(f"├ PnL ${pnl:+.2f} · Vol ${vol:.2f}")
                lines.append(f"├ <code>{safe_wallet}</code>")
                lines.append(f"└ {action_links}")
                lines.append("")

        lines.append("")
        lines.append(
                "Use the buttons below to toggle copying, browse top traders, "
                "or follow a wallet address directly. You can also use `/copy` in chat."
        )

        text = "\n".join(lines)

        # Add unfollow buttons for each leader
        if unfollow_buttons:
            unfollow_buttons.append(
                [InlineKeyboardButton("👥 View All Leaders", callback_data="copycfg:all_leaders")]
            )

        pagination_row = []
        if has_prev_page:
            pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"copycfg:leaders:{leaderboard_page-1}"))
        if has_next_page:
            pagination_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"copycfg:leaders:{leaderboard_page+1}"))

        buttons = (
            unfollow_buttons + [
                pagination_row,
                [
                    InlineKeyboardButton(
                        "✅ Enable" if not enabled else "⏸ Disable",
                        callback_data=f"copycfg:{'enable' if not enabled else 'disable'}",
                    )
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="copycfg:refresh"),
                    InlineKeyboardButton("🏠 Main Menu", callback_data="home:main"),
                ],
                [
                    InlineKeyboardButton("⭐ View & follow top traders", callback_data="copycfg:leaders:1"),
                ],
                [
                    InlineKeyboardButton("➕ Follow by wallet address", callback_data="copycfg:follow_manual"),
                ],
                [
                    InlineKeyboardButton("🚫 Unfollow all leaders", callback_data="copycfg:unfollow_all"),
                ],
            ]
        )
        # Remove empty rows from keyboard.
        buttons = [row for row in buttons if row]
        if query is not None:
            await _safe_edit_message(
                query,
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(buttons),
            )

    async def follow_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /follow <wallet_address> command - Start following a global wallet address for copy trading.
        Usage: /follow 0x...
        """
        user = update.effective_user
        db_user = db.get_user(user.id) if user else None
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return

        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: `/follow <wallet_address>`\n\n"
                "Example: `/follow 0x742d35Cc6634C0532925a3b844Bc454e4438f44e`\n\n"
                "This will start the follow flow for the given wallet address.",
                parse_mode="Markdown",
            )
            return

        wallet = args[0].strip()
        import re as _re
        if not _re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
            await update.message.reply_text(
                "Invalid wallet address format. Please provide a valid Polygon wallet address starting with 0x.",
            )
            return

        # Check if already following this wallet
        existing = db.execute(
            """
            SELECT id FROM copy_hooks
            WHERE follower_user_id = ? AND lower(leader_address) = lower(?)
            """,
            (db_user["user_id"], wallet.lower()),
        ).fetchone()
        if existing:
            await update.message.reply_text(
                f"You are already following `{wallet}`. Use /copy to manage your follow settings.",
                parse_mode="Markdown",
            )
            return

        # Start the follow flow
        context.user_data["pending_follow_wallet"] = wallet
        context.user_data["pending_follow_name"] = _refetch_polymarket_username(wallet) or wallet
        context.user_data["awaiting_follow_risk"] = True
        await _send_follow_risk_menu(update.message, wallet)

    async def _check_onboarding(update, context, db_user):
        """Check if user is onboarded. Returns (is_onboarded, reply_sent). If not onboarded, sends reply and returns (False, True)."""
        if not db_user:
            return False, False  # User doesn't exist, let caller handle

        is_onboarded, _ = db.get_user_onboarding_status(db_user["user_id"])
        if not is_onboarded:
            button = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔑 Enter Invite Code", callback_data="enter_invite_code")
            ]])
            await update.message.reply_text(
                "🔒 Your account needs to be activated with an invite code.\n\n"
                "Click below to enter your invite code:",
                parse_mode="Markdown",
                reply_markup=button,
            )
            return False, True
        return True, False

    async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /wallet command (delegates to unified account view)."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return
        # Check onboarding
        is_onboarded, replied = await _check_onboarding(update, context, db_user)
        if replied:
            return
        await _send_account_overview(update.effective_chat.id, context.bot, db_user)

    async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /balance command (delegates to unified account view)."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return
        # Check onboarding
        is_onboarded, replied = await _check_onboarding(update, context, db_user)
        if replied:
            return
        await _send_safe_balance(update, context, db_user)

    async def copyboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Show a simple notification board of recent copy-trade executions
        for this user, based on the trades table (copied_from_user_id set).
        """
        user = update.effective_user
        if not user:
            return
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return

        # Check onboarding
        is_onboarded, replied = await _check_onboarding(update, context, db_user)
        if replied:
            return

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
            LIMIT 20;
            """,
            (db_user["user_id"],),
        ).fetchall()

        if not rows:
            await update.message.reply_text(
                "You don't have any copy-trade activity yet. "
                "Follow a leader from the Copy Trading panel to get started."
            )
            return

        import datetime as _dt

        lines: list[str] = ["📝 Recent copy-trade activity:\n"]
        for r in rows:
            d = dict(r)
            ts = d.get("executed_at") or 0
            dt = _dt.datetime.fromtimestamp(int(ts)) if ts else None
            when = dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown time"
            side = (d.get("side") or "").upper()
            amount = float(d.get("amount") or 0.0)
            leader_name = d.get("leader_username") or (d.get("leader_address") or "")[:10]
            condition_id = d.get("condition_id") or ""
            status = d.get("status") or ""

            lines.append(
                f"- {when}: {side} ${amount:.2f} "
                f"(leader: {leader_name}, status: {status}, cond: {condition_id})"
            )

        text = "\n".join(lines)
        await _send_long_message(context.bot, update.effective_chat.id, text)

    async def markets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /markets command: open markets submenu first."""
        await _send_markets_submenu(update.effective_chat.id, context.bot)

    async def category_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show categories submenu or open a specific category."""
        args = context.args or []
        if not args:
            await _send_banner_with_caption(
                context.bot,
                update.effective_chat.id,
                MARKET_BANNERS["all"],
                "🧭 *Categories*\n\nChoose a category:",
                parse_mode="Markdown",
                reply_markup=_build_categories_submenu_keyboard(),
                max_caption_len=1000,
            )
            return
        category = " ".join(args).strip().lower()
        text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, category)
        keyboard = _build_pagination_keyboard(page, total_pages, category, markets)
        await _send_banner_with_caption(
            context.bot,
            update.effective_chat.id,
            _market_banner_for_slug(category),
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            max_caption_len=1000,
        )

    async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle category button tap: fetch markets for that category with trade links."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("category:"):
            return
        slug = data.split(":", 1)[1].strip()
        if not slug:
            return
        # Handle trending as a special case
        if slug == "trending":
            text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, "trending")
        else:
            text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, slug)
        if not markets:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"No markets found in {slug}. Try another category.",
            )
            return
        keyboard = _build_pagination_keyboard(page, total_pages, slug, markets)
        await _send_banner_with_caption(
            context.bot,
            query.message.chat_id,
            _market_banner_for_slug(slug),
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            max_caption_len=1000,
        )
        return

    async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the main menu / home screen."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return
        # Check if user is onboarded
        is_onboarded, _ = db.get_user_onboarding_status(user.id)
        if not is_onboarded:
            button = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔑 Enter Invite Code", callback_data="enter_invite_code")
            ]])
            await update.message.reply_text(
                "🔒 Your account needs to be activated with an invite code.\n\n"
                "Click below to enter your invite code:",
                parse_mode="Markdown",
                reply_markup=button,
            )
            return
        await update.message.reply_text("🏠 Main Menu", reply_markup=MAIN_KEYBOARD)
        await _send_home(update, context, db_user)

    async def _send_copytrade_featured(chat_id: int, bot):
        text = (
            "👥 *Copy Trading*\n"
            "Mirror selected trader wallets automatically.\n\n"
            "Open the panel to follow leaders, set risk limits, and enable or pause copying anytime."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👥 Open Copy-Trading Panel", callback_data="copycfg:refresh")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
            ]
        )
        await _send_banner_with_caption(
            bot,
            chat_id,
            SMART_WALLETS_BANNER_PATH,
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    def _fetch_username_from_gamma(address: str) -> str | None:
        """Fetch Polymarket username directly from Gamma public-profile API."""
        addr = (address or "").strip().lower()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
            return None
        username: str | None = None
        try:
            import requests

            resp = requests.get(
                "https://gamma-api.polymarket.com/public-profile",
                params={"address": addr},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    for key in ("username", "userName", "name", "displayName"):
                        val = data.get(key)
                        if isinstance(val, str) and val.strip():
                            username = val.strip()
                            break
        except Exception:
            username = None
        return username

    def _fetch_polymarket_username(address: str, *, refetch: bool = False) -> str | None:
        """Resolve Polymarket username for a wallet, with DB cache and optional refetch."""
        addr = (address or "").strip().lower()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
            return None

        # Use cached DB profile unless caller requests a forced refresh.
        if not refetch:
            cached = db.get_polymarket_profile(addr)
            if cached:
                try:
                    fetched_at = int(cached.get("fetched_at") or 0)
                except (TypeError, ValueError):
                    fetched_at = 0
                # 1 hour cache window
                if fetched_at and (int(time.time()) - fetched_at) < 3600:
                    cached_name = (cached.get("username") or "").strip()
                    if cached_name:
                        return cached_name

        username = _fetch_username_from_gamma(addr)
        # Persist fetched value (including blank) so refetch timestamp is tracked.
        db.upsert_polymarket_profile(addr, username)

        if username:
            return username

        # Final fallback to any previously cached username.
        cached = db.get_polymarket_profile(addr)
        if cached:
            cached_name = (cached.get("username") or "").strip()
            if cached_name:
                return cached_name
        return None

    def _refetch_polymarket_username(address: str) -> str | None:
        """Force-refresh Polymarket username for a wallet and persist it."""
        addr = (address or "").strip().lower()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
            return None
        try:
            return db.refetch_polymarket_username(addr, _fetch_username_from_gamma)
        except Exception:
            return _fetch_polymarket_username(addr, refetch=True)

    async def _send_follow_risk_menu(target, wallet: str):
        """Prompt for per-trade risk cap using buttons (with text fallback)."""
        poly_name = _fetch_polymarket_username(wallet) or f"{wallet[:10]}..."
        text = (
            f"You're about to follow *{poly_name}* (`{wallet}`) for copy trading.\n\n"
            "How much USD do you want to risk per copied trade for this leader?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("$1", callback_data="copyrisk:1"),
                    InlineKeyboardButton("$5", callback_data="copyrisk:5"),
                    InlineKeyboardButton("$10", callback_data="copyrisk:10"),
                ],
                [
                    InlineKeyboardButton("$20", callback_data="copyrisk:20"),
                    InlineKeyboardButton("$50", callback_data="copyrisk:50"),
                    InlineKeyboardButton("$100", callback_data="copyrisk:100"),
                ],
                [
                    InlineKeyboardButton("♾ No Cap", callback_data="copyrisk:none"),
                ],
                [
                    InlineKeyboardButton("❌ Cancel", callback_data="copyrisk:cancel"),
                ],
            ]
        )
        if hasattr(target, "edit_message_text"):
            await _safe_edit_message(target, text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await target.reply_text(
                text + "\n\nYou can also type a number (e.g. 10) or type `none`.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

    async def _send_follow_mode_menu(target):
        """Prompt for copy sizing mode using buttons (with text fallback)."""
        text = (
            "How should copy-trade sizing work for this leader?\n\n"
            "• *Fractional* → same % of your balance as the leader\n"
            "• *1:1* → same USD amount as the leader\n"
            "• *Beginner* → small fixed USD per trade (default $1)"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📐 Fractional", callback_data="copymode:fractional")],
                [InlineKeyboardButton("⚖️ 1:1", callback_data="copymode:one_to_one")],
                [InlineKeyboardButton("🌱 Beginner", callback_data="copymode:beginner")],
                [InlineKeyboardButton("❌ Cancel", callback_data="copymode:cancel")],
            ]
        )
        if hasattr(target, "edit_message_text"):
            await _safe_edit_message(target, text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await target.reply_text(
                text + "\n\nYou can also type `fractional`, `1:1`, or `beginner`.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

    async def _finalize_follow_for_user(
        db_user: dict,
        context: ContextTypes.DEFAULT_TYPE,
        pending_wallet: str,
        max_per: float,
        mode: str,
    ) -> tuple[str, str]:
        """Create/activate copy hook and clear follow wizard state."""
        if mode == "fractional":
            fractional_flag = True
            fixed_usd = 1.0
        elif mode == "one_to_one":
            fractional_flag = False
            fixed_usd = 1.0
        else:
            mode = "beginner"
            fractional_flag = False
            fixed_usd = 1.0

        follower_id = db_user["user_id"]
        cfg = {
            "size_multiplier": 1.0,
            "max_usd_per_trade": max_per,
            "fractional": fractional_flag,
            "mode": mode,
            "fixed_usd_amount": fixed_usd,
            "max_loss_pct": 0.0,
            "slippage_pct": 0.0,
            "leader_address": pending_wallet,
            "display_name": context.user_data.get("pending_follow_name") or pending_wallet,
        }

        db.add_global_copy_hook(
            follower_user_id=follower_id,
            leader_address=pending_wallet,
            config=cfg,
        )

        if COPY_TRACKER_AVAILABLE:
            try:
                tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                tracker.reload()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to reload hooks: {e}")

        context.user_data["awaiting_follow_mode"] = False
        context.user_data["awaiting_follow_risk"] = False
        context.user_data["awaiting_follow_wallet"] = False
        context.user_data["pending_follow_wallet"] = None
        context.user_data["pending_follow_max_per"] = None
        leader_name = context.user_data.get("pending_follow_name") or pending_wallet
        context.user_data["pending_follow_name"] = None

        msg = (
            f"✅ Now following {leader_name} (`{pending_wallet}`) for copy trading.\n\n"
            f"Per-trade cap: {'no cap' if max_per <= 0 else f'${max_per:.2f}'}\n"
            f"Sizing mode: {mode}\n\n"
            "Make sure copy trading is enabled. Copies will execute when your backend "
            "copy-trading worker runs for this leader address."
        )
        return msg, mode

    async def _send_markets_submenu(chat_id: int, bot):
        text = (
            "🧭 *Markets*\n\n"
            "Choose how you want to browse markets:"
        )
        await _send_banner_with_caption(
            bot,
            chat_id,
            MARKET_BANNERS["all"],
            text,
            parse_mode="Markdown",
            reply_markup=_build_markets_submenu_keyboard(),
            max_caption_len=1000,
        )

    async def handle_copycfg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Enable/disable copy trading for the current user, mirroring /me/copy-trading/enable|disable.
        """
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copycfg:"):
            return
        parts = data.split(":")
        action = parts[1].strip() if len(parts) > 1 else ""
        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        if action == "enable":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET copy_trading_enabled = 1 WHERE user_id = ?;",
                    (db_user["user_id"],),
                )
                conn.execute(
                    "UPDATE copy_hooks SET enabled = 1 WHERE follower_user_id = ?;",
                    (db_user["user_id"],),
                )
            db_user = db.get_user(db_user["user_id"])

            # Reload tracker hooks for real-time tracking
            if COPY_TRACKER_AVAILABLE:
                try:
                    tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                    tracker.reload()
                    logging.getLogger(__name__).info(f"Reloaded hooks for user {db_user['user_id']}")
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to reload hooks: {e}")

            await _safe_edit_message(
                query,
                "✅ Copy trading enabled.\n\nYour existing hooks (if any) are now active.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔄 Refresh", callback_data="copycfg:refresh")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
                    ]
                ),
            )
            return

        if action == "disable":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET copy_trading_enabled = 0 WHERE user_id = ?;",
                    (db_user["user_id"],),
                )
                conn.execute(
                    "UPDATE copy_hooks SET enabled = 0 WHERE follower_user_id = ?;",
                    (db_user["user_id"],),
                )

            # Reload tracker hooks
            if COPY_TRACKER_AVAILABLE:
                try:
                    tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                    tracker.reload()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to reload hooks: {e}")

            await _safe_edit_message(
                query,
                "⏸ Copy trading disabled.\n\nHooks remain saved but will not fire until you enable again.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔄 Refresh", callback_data="copycfg:refresh")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
                    ]
                ),
            )
            return

        if action == "refresh":
            # Re-render state summary in-place.
            db_user = db.get_user(query.from_user.id)
            if db_user and query.message:
                await _send_copy_trading_state(
                    query.message.chat_id,
                    context.bot,
                    db_user,
                    leaderboard_page=1,
                    query=query,
                )
            return

        if action == "all_leaders":
            # Show all followed leaders with unfollow buttons
            db_user = db.get_user(query.from_user.id)
            if not db_user:
                await _safe_edit_message(query, "Please run /start first.")
                return

            user_id = db_user["user_id"]
            rows = db.execute(
                "SELECT id, leader_address, config FROM copy_hooks WHERE follower_user_id = ?;",
                (user_id,),
            ).fetchall()

            if not rows:
                await _safe_edit_message(
                    query,
                    "You are not following any leaders yet.\n\nUse '➕ Follow by wallet address' to start.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")]
                    ])
                )
                return

            import html as _html
            lines = ["<b>👥 Your followed leaders</b>", ""]
            buttons = []
            for r in rows:
                try:
                    cfg = json.loads(r["config"] or "{}")
                except Exception:
                    cfg = {}
                addr = cfg.get("leader_address", "Unknown")
                name = cfg.get("display_name")
                if not name or str(name).lower() == str(addr).lower():
                    name = _fetch_polymarket_username(addr) or addr[:10] + "..."
                safe_addr = _html.escape(str(addr))
                safe_name = _html.escape(str(name))
                profile_url = f"https://polymarket.com/profile/{addr}" if addr.startswith("0x") else "https://polymarket.com"
                lines.append(f"{safe_name}")
                lines.append(f"├ <code>{safe_addr}</code>")
                lines.append(f"└ <a href=\"{profile_url}\">Profile</a>")
                lines.append("")
                buttons.append([
                    InlineKeyboardButton(
                        f"🚫 Unfollow {name}",
                        callback_data=f"copyunfollow:{r['id']}"
                    )
                ])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="copycfg:refresh")])

            await _safe_edit_message(
                query,
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if action == "leaders":
            page = 1
            if len(parts) >= 3:
                try:
                    page = max(1, int(parts[2]))
                except Exception:
                    page = 1
            db_user = db.get_user(query.from_user.id)
            if db_user and query.message:
                await _send_copy_trading_state(
                    query.message.chat_id,
                    context.bot,
                    db_user,
                    leaderboard_page=page,
                    query=query,
                )
            return

        if action == "follow_manual":
            # Ask the user to send a wallet address to follow.
            context.user_data["awaiting_follow_wallet"] = True
            await _safe_edit_message(
                query,
                "Send the Polygon wallet address you want to follow for copy trading.\n\n"
                "Example:\n0xabc123...",
            )
            return

        if action == "unfollow_all":
            # Remove all copy-trading hooks for this user.
            follower_id = db_user["user_id"]

            # Get all hooks first to remove them properly
            hooks = db.get_global_copy_hooks(follower_user_id=follower_id)
            for hook in hooks:
                if hook.get("leader_address"):
                    db.remove_global_copy_hook(
                        follower_user_id=follower_id,
                        leader_address=hook["leader_address"],
                    )

            # Ensure background tracker drops the removed hooks immediately.
            if COPY_TRACKER_AVAILABLE:
                try:
                    tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                    tracker.reload()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to reload hooks: {e}")

            await _safe_edit_message(
                query,
                "🚫 You are no longer following any leaders for copy trading.",
            )
            return

    async def handle_signals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("signals:"):
            return

        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        parts = data.split(":")
        timeframe = parts[1] if len(parts) > 1 else ""

        # Handle 5m and 15m specific actions
        if timeframe in ("5m", "15m"):
            action = parts[2] if len(parts) > 2 else ""

            if action == "enable":
                flag = 1
                col_enabled = "signal_5m_enabled" if timeframe == "5m" else "signal_15m_enabled"
                with db.transaction() as conn:
                    conn.execute(
                        f"UPDATE users SET {col_enabled} = ? WHERE user_id = ?;",
                        (flag, db_user["user_id"]),
                    )
                db_user = db.get_user(db_user["user_id"]) or db_user
                await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
                return
            elif action == "disable":
                flag = 0
                col_enabled = "signal_5m_enabled" if timeframe == "5m" else "signal_15m_enabled"
                with db.transaction() as conn:
                    conn.execute(
                        f"UPDATE users SET {col_enabled} = ? WHERE user_id = ?;",
                        (flag, db_user["user_id"]),
                    )
                db_user = db.get_user(db_user["user_id"]) or db_user
                await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
                return
            elif action == "shares":
                # Show shares selection menu
                await _render_signal_shares_menu(query.message.chat_id, context.bot, db_user, timeframe, query=query)
                return
            elif action == "amt" and len(parts) >= 4:
                # Format: signals:<timeframe>:amt:<value>
                choice = parts[3].strip()
                if choice == "custom":
                    _pending_signal_amount_input[int(db_user["user_id"])] = timeframe
                    await _safe_edit_message(
                        query,
                        f"Send the number of shares for {timeframe.upper()} signals (minimum 5).",
                        parse_mode="Markdown",
                    )
                    return
                try:
                    shares = float(choice)
                except Exception:
                    shares = 0.0
                if shares < 5:
                    await _safe_edit_message(query, "Minimum 5 shares.")
                    return
                col = "signal_5m_amount_usd" if timeframe == "5m" else "signal_15m_amount_usd"
                with db.transaction() as conn:
                    conn.execute(
                        f"UPDATE users SET {col} = ? WHERE user_id = ?;",
                        (shares, db_user["user_id"]),
                    )
                db_user = db.get_user(db_user["user_id"]) or db_user
                await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
                return

        # Back button
        if timeframe == "back":
            await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
            return

        # Legacy support for generic enable/disable
        if timeframe in ("enable", "disable"):
            flag = 1 if timeframe == "enable" else 0
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET signal_trading_enabled = ? WHERE user_id = ?;",
                    (flag, db_user["user_id"]),
                )
            db_user = db.get_user(db_user["user_id"]) or db_user
            await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
            return

        # Legacy support for amount
        if timeframe == "amt" and len(parts) >= 3:
            choice = parts[2].strip()
            if choice == "custom":
                _pending_signal_amount_input[int(db_user["user_id"])] = "legacy"
                await _safe_edit_message(
                    query,
                    "Send the number of shares per signal (minimum 5).",
                    parse_mode="Markdown",
                )
                return
            try:
                shares = float(choice)
            except Exception:
                shares = 0.0
            if shares < 5:
                await _safe_edit_message(query, "Minimum 5 shares.")
                return
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET signal_trade_amount_usd = ? WHERE user_id = ?;",
                    (shares, db_user["user_id"]),
                )
            db_user = db.get_user(db_user["user_id"]) or db_user
            await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
            return


    # ── Copy-trading notification loop (push messages for copied trades) ──

    _last_notified_copy_trade_id: int = 0
    _signal_log_pos: int = 0
    _signal_last_sent_ts: dict[str, int] = {}

    async def _copy_trading_notification_loop(app: Application) -> None:
        """
        Background task that watches the trades table for new copy-trades
        (rows where copied_from_user_id is set) and sends Telegram messages
        to followers when a copied trade is executed (both opens and closes).
        """
        nonlocal _last_notified_copy_trade_id

        # On first run, fast-forward to the latest trade id so we only
        # notify for *new* copy trades created after the bot starts.
        try:
            row = db.execute("SELECT MAX(id) AS max_id FROM trades;").fetchone()
            if row and row["max_id"]:
                _last_notified_copy_trade_id = int(row["max_id"])
        except Exception:
            _last_notified_copy_trade_id = 0

        while True:
            try:
                rows = db.execute(
                    """
                    SELECT
                        t.id,
                        t.user_id,
                        t.condition_id,
                        t.side,
                        t.amount,
                        t.size,
                        t.price,
                        t.order_side,
                        t.executed_at,
                        t.copied_from_user_id,
                        u.username AS leader_username,
                        u.eth_address AS leader_address
                    FROM trades t
                    LEFT JOIN users u ON u.user_id = t.copied_from_user_id
                    WHERE t.id > ? AND t.copied_from_user_id IS NOT NULL
                    ORDER BY t.id ASC
                    LIMIT 100;
                    """,
                    (_last_notified_copy_trade_id,),
                ).fetchall()

                if not rows:
                    await asyncio.sleep(10)
                    continue

                for r in rows:
                    d = dict(r)
                    trade_id = int(d.get("id") or 0)
                    if trade_id > _last_notified_copy_trade_id:
                        _last_notified_copy_trade_id = trade_id

                    follower_user_id = d.get("user_id")
                    if not follower_user_id:
                        continue

                    condition_id = d.get("condition_id") or ""
                    m = None
                    if condition_id:
                        try:
                            from market_cache import get_by_condition_id as _mc_get_by_condition_id

                            m = _mc_get_by_condition_id(condition_id)
                        except Exception:
                            m = None

                    question = getattr(m, "question", None) if m else None
                    side = (d.get("side") or "").upper()
                    amount = float(d.get("amount") or 0.0)
                    size = d.get("size")
                    price = d.get("price")
                    order_side = (d.get("order_side") or "").upper()
                    leader_name = d.get("leader_username") or (d.get("leader_address") or "")[:10]

                    # Classify as open vs close based on order_side (BUY = open, SELL = close).
                    if order_side == "SELL":
                        action = "Closed copy position"
                    else:
                        action = "Opened copy position"

                    parts: list[str] = [
                        f"🔔 {action} from leader {leader_name}",
                        f"Outcome: {side}",
                        f"Amount: ${amount:.2f}",
                    ]
                    if size is not None and price is not None:
                        parts.append(f"Size: {size} @ ${price:.4f}")
                    if question:
                        parts.append(f"Market: {question}")

                    text = "\n".join(parts)

                    try:
                        await app.bot.send_message(chat_id=follower_user_id, text=text)
                    except Exception as e:
                        logger.warning(f"Failed to send copy-trade notification to {follower_user_id}: {e}")

            except Exception as e:
                logger.warning(f"Error in copy-trading notification loop: {e}")
                await asyncio.sleep(10)

    async def _signal_outbox_delivery_loop(app: Application) -> None:
        """
        Background task that delivers queued signal-trading notifications from
        signal_notifications_outbox and marks them as sent.
        """
        while True:
            try:
                rows = db.execute(
                    """
                    SELECT id, user_id, kind, text, created_at
                    FROM signal_notifications_outbox
                    WHERE sent_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT 50;
                    """
                ).fetchall()
                if not rows:
                    await asyncio.sleep(3)
                    continue

                now_ts = int(time.time())
                for r in rows:
                    d = dict(r)
                    oid = int(d.get("id") or 0)
                    uid = int(d.get("user_id") or 0)
                    text = str(d.get("text") or "").strip()
                    if oid <= 0 or uid <= 0 or not text:
                        try:
                            db.execute(
                                "UPDATE signal_notifications_outbox SET sent_at = ? WHERE id = ?;",
                                (now_ts, oid),
                            )
                        except Exception:
                            pass
                        continue

                    try:
                        await app.bot.send_message(chat_id=uid, text=text)
                        db.execute(
                            "UPDATE signal_notifications_outbox SET sent_at = ? WHERE id = ?;",
                            (int(time.time()), oid),
                        )
                    except Forbidden:
                        # User blocked the bot; mark as sent to avoid retry loops.
                        try:
                            db.execute(
                                "UPDATE signal_notifications_outbox SET sent_at = ? WHERE id = ?;",
                                (int(time.time()), oid),
                            )
                        except Exception:
                            pass
                    except RetryAfter as e:
                        await asyncio.sleep(float(getattr(e, "retry_after", 1) or 1))
                    except Exception as e:
                        logger.warning("Failed to deliver outbox notification %s to %s: %s", oid, uid, e)

            except Exception as e:
                logger.warning("Error in outbox delivery loop: %s", e)
            await asyncio.sleep(2)

    def _build_signal_broadcast_text(payload: dict) -> str:
        """
        Create a neutral signal notification message for Telegram users.
        Intentionally avoids channel branding terms in the text.
        """
        side = str(payload.get("signal") or "").upper().strip() or "UNKNOWN"
        series = str(payload.get("series") or "").strip()
        asset = str(payload.get("asset") or "").strip()
        timeframe = str(payload.get("timeframe") or "").strip()
        signal_time = (
            str(payload.get("time_utc_display") or "").strip()
            or str(payload.get("signal_at") or "").strip()
        )
        lines = [
            "🔔 New trading signal generated",
            f"Direction: {side}",
        ]
        if series:
            lines.append(f"Series: {series}")
        if asset and not series:
            lines.append(f"Asset: {asset}")
        if timeframe:
            lines.append(f"Timeframe: {timeframe}")
        if signal_time:
            lines.append(f"Signal time: {signal_time}")
        lines.append("Use /markets to review opportunities.")
        return "\n".join(lines)

    def _signal_window_seconds(timeframe: str) -> int:
        """
        Dedup/throttle window for signals to avoid concurrent strategies spamming.
        - 5M -> 300 seconds
        - 15M -> 900 seconds
        Falls back to parsing '<N>M' to N*60; returns 0 if unknown.
        """
        tf = (timeframe or "").strip().upper()
        if not tf:
            return 0
        if tf.endswith("M"):
            try:
                mins = int(tf[:-1])
                return max(0, mins) * 60
            except Exception:
                return 0
        return 0

    def _signal_dedup_key(payload: dict) -> str:
        # Prefer Polymarket "series" for concurrency filtering (signals are series-level).
        series = str(payload.get("series_slug") or payload.get("series") or "").strip().upper()
        asset = str(payload.get("asset") or "").strip().upper()
        scope = series or asset or "MARKET"
        tf = str(payload.get("timeframe") or "").strip().upper()
        return f"{scope}:{tf}"

    async def _announcement_signal_broadcast_loop(app: Application) -> None:
        """
        Broadcast newly generated live signal events to all known bot users.
        Reads incremental JSONL updates from TELEGRAM_SIGNAL_OUTPUT.
        """
        nonlocal _signal_log_pos
        signal_path = (os.getenv("TELEGRAM_SIGNAL_OUTPUT") or "logs/announcement_signals.jsonl").strip()
        if not signal_path:
            signal_path = "logs/announcement_signals.jsonl"

        # Fast-forward on startup so we only broadcast signals generated after bot start.
        try:
            with open(signal_path, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                _signal_log_pos = f.tell()
        except FileNotFoundError:
            _signal_log_pos = 0
        except Exception as e:
            logger.warning("Signal broadcaster init failed for %s: %s", signal_path, e)
            _signal_log_pos = 0

        while True:
            try:
                if not os.path.exists(signal_path):
                    await asyncio.sleep(3)
                    continue

                with open(signal_path, "r", encoding="utf-8") as f:
                    # Handle log rotation/truncation.
                    try:
                        file_size = os.path.getsize(signal_path)
                    except Exception:
                        file_size = None
                    if file_size is not None and _signal_log_pos > file_size:
                        _signal_log_pos = 0
                    f.seek(_signal_log_pos)
                    new_lines = f.readlines()
                    _signal_log_pos = f.tell()

                if not new_lines:
                    await asyncio.sleep(3)
                    continue

                live_payloads: list[dict] = []
                for line in new_lines:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if not obj.get("parsed"):
                        continue
                    # Only broadcast newly observed live signals.
                    source = str(obj.get("source") or "")
                    if source != "live:new":
                        continue
                    live_payloads.append(obj)

                if not live_payloads:
                    await asyncio.sleep(1)
                    continue

                user_rows = db.execute(
                    "SELECT user_id FROM users WHERE user_id IS NOT NULL;"
                ).fetchall()
                user_ids = [int(r["user_id"]) for r in user_rows if r and r["user_id"] is not None]
                if not user_ids:
                    await asyncio.sleep(1)
                    continue

                for payload in live_payloads:
                    # Concurrency guard: if multiple strategies emit around the same time,
                    # only broadcast the first signal within its market window.
                    tf = str(payload.get("timeframe") or "").strip()
                    window = _signal_window_seconds(tf)
                    if window > 0:
                        try:
                            ts = int(payload.get("signal_ts") or 0) or int(time.time())
                        except Exception:
                            ts = int(time.time())
                        key = _signal_dedup_key(payload)
                        last_ts = int(_signal_last_sent_ts.get(key) or 0)
                        if last_ts and ts < (last_ts + window):
                            continue
                        _signal_last_sent_ts[key] = ts

                    text = _build_signal_broadcast_text(payload)
                    for uid in user_ids:
                        try:
                            await app.bot.send_message(chat_id=uid, text=text)
                        except Exception as e:
                            logger.warning("Failed signal broadcast to %s: %s", uid, e)

            except Exception as e:
                logger.warning("Error in signal broadcast loop: %s", e)
                await asyncio.sleep(3)

    async def _render_signal_trading_menu(chat_id: int, bot: Bot, db_user: dict, query=None) -> None:
        # 5m settings
        enabled_5m = bool(db_user.get("signal_5m_enabled") or 0)
        shares_5m = int(db_user.get("signal_5m_amount_usd") or 5)
        # 15m settings
        enabled_15m = bool(db_user.get("signal_15m_enabled") or 0)
        shares_15m = int(db_user.get("signal_15m_amount_usd") or 5)

        status_5m = "ON ✅" if enabled_5m else "OFF ⏸"
        status_15m = "ON ✅" if enabled_15m else "OFF ⏸"

        text = (
            "📡 *Signal Trading Settings*\n\n"
            "Configure auto-trading for different timeframes.\n\n"
            f"⏱ *5-minute signals*\n"
            f"Status: *{status_5m}*\n"
            f"Shares per signal: *{shares_5m}*\n\n"
            f"⏱ *15-minute signals*\n"
            f"Status: *{status_15m}*\n"
            f"Shares per signal: *{shares_15m}*\n\n"
            "⚡ *Note:* Shares are bought at $0.55/share max.\n"
            "Example: 10 shares = ~$5.50 USD\n\n"
            "Tap a button below to configure."
        )

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⏱ 5m ON" if enabled_5m else "⏱ 5m Enable",
                        callback_data="signals:5m:disable" if enabled_5m else "signals:5m:enable",
                    ),
                    InlineKeyboardButton(
                        "⏱ 15m ON" if enabled_15m else "⏱ 15m Enable",
                        callback_data="signals:15m:disable" if enabled_15m else "signals:15m:enable",
                    ),
                ],
                [
                    InlineKeyboardButton("🔢 5m Shares", callback_data="signals:5m:shares"),
                    InlineKeyboardButton("🔢 15m Shares", callback_data="signals:15m:shares"),
                ],
                [
                    InlineKeyboardButton("🏠 Main Menu", callback_data="home:main"),
                ],
            ]
        )
        if query is not None:
            await _safe_edit_message(query, text, parse_mode="Markdown", reply_markup=kb)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)

    async def _render_signal_shares_menu(chat_id: int, bot: Bot, db_user: dict, timeframe: str, query=None) -> None:
        """Render shares selection menu for 5m or 15m signals."""
        col = "signal_5m_amount_usd" if timeframe == "5m" else "signal_15m_amount_usd"
        current_shares = int(db_user.get(col) or 5)

        text = (
            f"🔢 *{timeframe.upper()} Signal Shares*\n\n"
            f"Current: *{current_shares} shares*\n\n"
            "Choose a preset or send custom amount.\n\n"
            "Minimum: *5 shares* (~$2.75)")

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("5 shares", callback_data=f"signals:{timeframe}:amt:5"),
                    InlineKeyboardButton("10 shares", callback_data=f"signals:{timeframe}:amt:10"),
                    InlineKeyboardButton("15 shares", callback_data=f"signals:{timeframe}:amt:15"),
                ],
                [
                    InlineKeyboardButton("20 shares", callback_data=f"signals:{timeframe}:amt:20"),
                    InlineKeyboardButton("30 shares", callback_data=f"signals:{timeframe}:amt:30"),
                    InlineKeyboardButton("50 shares", callback_data=f"signals:{timeframe}:amt:50"),
                ],
                [
                    InlineKeyboardButton("✍️ Custom", callback_data=f"signals:{timeframe}:amt:custom"),
                ],
                [
                    InlineKeyboardButton("⬅️ Back", callback_data="signals:back"),
                ],
            ]
        )
        if query is not None:
            await _safe_edit_message(query, text, parse_mode="Markdown", reply_markup=kb)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)

    async def handle_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("home:"):
            return
        choice = data.split(":", 1)[1].strip()
        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        if choice in ("main", "refresh"):
            await _edit_to_home(query, db_user)
            return
        if choice == "markets":
            await _send_markets_submenu(query.message.chat_id, context.bot)
            return
        if choice == "portfolio":
            await _send_account_overview(query.message.chat_id, context.bot, db_user)
            return
        if choice == "wallet":
            await wallet_cmd(update, context)
            return
        if choice in ("copy", "smart_wallets"):
            await _send_copy_trading_state(
                query.message.chat_id,
                context.bot,
                db_user,
                leaderboard_page=1,
                query=query,
            )
            return
        if choice == "help":
            await help_cmd(update, context)
            return

        if choice == "signals":
            await _render_signal_trading_menu(query.message.chat_id, context.bot, db_user, query=query)
            return

        if choice in ("referrals", "settings"):
            # Simple stubs for now (keeps the new menu flow coherent)
            if choice == "settings":
                text = (
                    "⚙️ *Settings*\n\n"
                    "Use these options to manage your trading setup:\n\n"
                    "• 🔑 *Export private key* (never share with others)\n"
                    "• 👥 *Manage copy‑trading leaders* (follow / unfollow top traders)\n\n"
                    "Tap a button below to continue."
                )
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔑 Export private key",
                                    callback_data="settings:pk",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "👥 Manage leaders",
                                    callback_data="copycfg:leaders",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "🏠 Main Menu",
                                    callback_data="home:main",
                                ),
                            ],
                        ]
                    ),
                )
                return
            else:
                text = (
                    "👥 Referrals\n"
                    "Referral rewards are handled outside this Telegram bot. Use the main app to manage referrals."
                )
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")]]
                    ),
                )
                return

    async def handle_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copy:"):
            return
        choice = data.split(":", 1)[1].strip()
        if choice == "view_more":
            await _safe_edit_message(
                query,
                "Use the Copy‑trading panel to manage who you follow.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("👥 Open Copy-Trading Panel", callback_data="copycfg:refresh")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
                    ]
                ),
            )
            return

        await _safe_edit_message(
            query,
            "Use /copy or the *Copy trading* menu to review settings, then enable copy trading to mirror your chosen leaders.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("👥 Open Copy-Trading Panel", callback_data="copycfg:refresh")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
                ]
            ),
        )


    async def handle_unfollow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle unfollow button for individual leaders."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copyunfollow:"):
            return
        try:
            hook_id = int(data.split(":")[1])
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Invalid hook ID.")
            return

        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        # Delete the hook - need to get leader_address first
        hook_row = db.execute(
            "SELECT leader_address FROM copy_hooks WHERE id = ? AND follower_user_id = ?;",
            (hook_id, db_user["user_id"]),
        ).fetchone()

        if hook_row:
            leader_address = hook_row["leader_address"]
            db.remove_global_copy_hook(
                follower_user_id=db_user["user_id"],
                leader_address=leader_address,
            )

        # Reload tracker hooks
        if COPY_TRACKER_AVAILABLE:
            try:
                tracker = get_manager(db_path=os.getenv("DB_PATH", "app_data.sqlite3"))
                tracker.reload()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to reload hooks: {e}")

        await _safe_edit_message(
            query,
            "✅ You have stopped following this leader.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Refresh", callback_data="copycfg:refresh")]]
            ),
        )

    async def handle_copyfollow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Follow' button from global leaderboard."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copyfollow:"):
            return
        wallet = data.split(":", 1)[1].strip()
        user = query.from_user
        db_user = db.get_user(user.id) if user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        import re
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
            await _safe_edit_message(query, "Invalid wallet address for follow.")
            return

        # Defer hook creation until we know the user's risk settings.
        context.user_data["pending_follow_wallet"] = wallet
        context.user_data["pending_follow_name"] = _refetch_polymarket_username(wallet) or wallet
        context.user_data["awaiting_follow_risk"] = True
        await _send_follow_risk_menu(query, wallet)

    async def handle_copyrisk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quick-select risk cap buttons for follow flow."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copyrisk:"):
            return
        choice = data.split(":", 1)[1].strip().lower()

        pending_wallet = context.user_data.get("pending_follow_wallet")
        if not pending_wallet:
            await _safe_edit_message(
                query,
                "Follow flow expired. Open Copy Trading and choose a leader again."
            )
            return

        if choice == "cancel":
            context.user_data["awaiting_follow_risk"] = False
            context.user_data["awaiting_follow_mode"] = False
            context.user_data["pending_follow_wallet"] = None
            context.user_data["pending_follow_max_per"] = None
            context.user_data["pending_follow_name"] = None
            await _safe_edit_message(
                query,
                "Cancelled follow setup.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("👥 Back to Copy Trading", callback_data="copycfg:refresh")]]
                ),
            )
            return

        if choice == "none":
            max_per = 0.0
        else:
            try:
                max_per = float(choice)
                if max_per <= 0:
                    raise ValueError
            except ValueError:
                await _safe_edit_message(query, "Invalid risk value. Please try again.")
                return

        context.user_data["pending_follow_max_per"] = max_per
        context.user_data["awaiting_follow_risk"] = False
        context.user_data["awaiting_follow_mode"] = True
        await _send_follow_mode_menu(query)

    async def handle_copymode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle sizing-mode buttons and finalize follow setup."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copymode:"):
            return
        choice = data.split(":", 1)[1].strip().lower()
        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        pending_wallet = context.user_data.get("pending_follow_wallet")
        max_per = float(context.user_data.get("pending_follow_max_per", 0.0) or 0.0)
        if not pending_wallet:
            await _safe_edit_message(
                query,
                "Follow flow expired. Open Copy Trading and choose a leader again."
            )
            return

        if choice == "cancel":
            context.user_data["awaiting_follow_mode"] = False
            context.user_data["pending_follow_wallet"] = None
            context.user_data["pending_follow_max_per"] = None
            context.user_data["pending_follow_name"] = None
            await _safe_edit_message(
                query,
                "Cancelled follow setup.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("👥 Back to Copy Trading", callback_data="copycfg:refresh")]]
                ),
            )
            return

        mode = choice
        if mode not in ("fractional", "one_to_one", "beginner"):
            await _safe_edit_message(query, "Invalid mode. Please choose one of the buttons.")
            return

        msg, _ = await _finalize_follow_for_user(
            db_user=db_user,
            context=context,
            pending_wallet=pending_wallet,
            max_per=max_per,
            mode=mode,
        )
        await _safe_edit_message(
            query,
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("👥 Copy Trading Panel", callback_data="copycfg:refresh")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="home:main")],
                ]
            ),
        )


    async def handle_copy_address_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle copy address callbacks - shows address with copy instruction."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("copy:"):
            return
        # Parse: copy:safe:<address> or copy:eoa:<address>
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        addr_type = parts[1]
        address = parts[2]

        if addr_type == "safe":
            label = "Safe (Trading Wallet)"
            msg = f"📋 *{label}*\n\n`{address}`\n\nTap to copy the address above."
        elif addr_type == "eoa":
            label = "EOA (Your Wallet)"
            msg = f"📋 *{label}*\n\n`{address}`\n\nTap to copy the address above."
        else:
            return

        await _safe_edit_message(query, msg, parse_mode="Markdown")

    async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle settings-related actions (e.g., export private key)."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("settings:"):
            return
        action = data.split(":", 1)[1].strip()
        user = query.from_user
        db_user = db.get_user(user.id) if user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        if action in ("pk", "copy_pk"):
            pk = (db_user.get("eth_private_key") or "").strip()
            if not pk:
                await _safe_edit_message(
                    query,
                    "No private key found for this wallet.",
                )
                return
            # Show with copy button
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Copy Private Key", callback_data="settings:copy_pk")],
                [InlineKeyboardButton("⬅️ Back", callback_data="settings:pk")],
            ])
            await _safe_edit_message(
                query,
                "⚠️ Export Private Key\n\n"
                "Your private key gives FULL access to your funds.\n"
                "Only use this in a secure, developer environment.\n\n"
                f"`{pk}`\n\nTap the button above to copy.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

    async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /cancel - Cancel current action or clear pending state.
        """
        user = update.effective_user
        if not user:
            return

        # Clear any pending invite code state
        context.user_data.pop("awaiting_invite_code", None)

        await update.message.reply_text(
            "Cancelled. Use /start to begin again or /menu to access the bot.",
            parse_mode="Markdown",
        )

    async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /join <CODE> - Join the bot using an invite code.
        """
        user = update.effective_user
        db_user = db.get_user(user.id) if user else None

        args = context.args
        if not args or len(args) == 0:
            await update.message.reply_text(
                "Please provide an invite code.\n\n"
                "Usage: `/join <CODE>`\n\n"
                "Get an invite code from an existing member.",
                parse_mode="Markdown",
            )
            return

        invite_code = args[0].strip().upper()

        # Check if user exists
        if not db_user:
            # New user - create and onboard
            await _handle_new_user_with_invite(update, context, invite_code, user)
            return

        # Check if already onboarded
        is_onboarded, _ = db.get_user_onboarding_status(db_user["user_id"])
        if is_onboarded:
            await update.message.reply_text(
                "You are already onboarded. Use `/menu` to access the bot.",
                parse_mode="Markdown",
            )
            return

        # Validate and claim invite code
        success, message = db.claim_invite_code(invite_code, db_user["user_id"])
        if not success:
            await update.message.reply_text(
                f"❌ {message}\n\nTry another invite code.",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            f"✅ Successfully onboarded! You can now use the bot.\n\n"
            f"Use `/menu` to get started.",
            parse_mode="Markdown",
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Task-based help menu rather than raw command list."""
        text = (
            "❓ **What do you want to do?**\n\n"
            "1. Place my first trade\n"
            "2. See my portfolio & wallet\n"
            "3. Deposit / Withdraw funds\n"
            "4. Learn about copy‑trading\n"
        )
        buttons = [
            [
                InlineKeyboardButton("🧠 First trade", callback_data="help:first_trade"),
            ],
            [
                InlineKeyboardButton("💼 Portfolio & wallet", callback_data="help:account"),
            ],
            [
                InlineKeyboardButton("💳 Deposit / Withdraw", callback_data="help:funds"),
            ],
            [
                InlineKeyboardButton("👥 Copy‑trading", callback_data="help:copy"),
            ],
            [
                InlineKeyboardButton("🎥 Tutorial videos", url="https://example.com/tutorials"),
                InlineKeyboardButton("💬 Community chat", url="https://t.me/+_HrlVAkvWV9iOTVl"),
            ],
        ]
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def handle_markets_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Browse markets' submenu choices (trending / volume / category)."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("markets:"):
            return
        _, choice = data.split(":", 1)
        choice = choice.strip()

        if choice == "back":
            await _send_markets_submenu(query.message.chat_id, context.bot)
            return

        if choice in ("trending", "volume") or "trending" in choice:
            slug = "volume" if choice == "volume" else "trending"
            text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, slug)
            if not markets:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="No markets available right now. Please try again later.",
                )
                return
            keyboard = _build_pagination_keyboard(page, total_pages, slug, markets)
            banner = MARKET_BANNERS["trending"] if slug == "trending" else MARKET_BANNERS["volume"]
            await _send_banner_with_caption(
                context.bot,
                query.message.chat_id,
                banner,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                max_caption_len=1000,
            )
            return

        if choice == "closing":
            text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, "closing")
            if not markets:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="No markets available right now. Please try again later.",
                )
                return
            keyboard = _build_pagination_keyboard(page, total_pages, "closing", markets)
            await _send_banner_with_caption(
                context.bot,
                query.message.chat_id,
                MARKET_BANNERS["all"],
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                max_caption_len=1000,
            )
            return

        if choice == "category":
            await _send_banner_with_caption(
                context.bot,
                query.message.chat_id,
                MARKET_BANNERS["all"],
                "🧭 *Categories*\n\nChoose a category:",
                parse_mode="Markdown",
                reply_markup=_build_categories_submenu_keyboard(),
                max_caption_len=1000,
            )
            return

        # Handle category buttons: markets:category_politics, markets_category_politics, etc.
        if choice.startswith("category_") or choice.startswith("markets_category_"):
            # Extract category from "category_politics" or "markets_category_politics"
            if choice.startswith("markets_category_"):
                category = choice.split("_", 2)[2]  # Get "politics" from "markets_category_politics"
            else:
                category = choice.split("_", 1)[1]  # Get "politics" from "category_politics"
            text, page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, 1, 5, category)
            if not markets:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"No markets found in {category}. Try another category.",
                )
                return
            keyboard = _build_pagination_keyboard(page, total_pages, category, markets)
            await _send_banner_with_caption(
                context.bot,
                query.message.chat_id,
                _market_banner_for_slug(category),
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                max_caption_len=1000,
            )
            return

    async def handle_markets_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle pagination for category markets with trade links."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("markets_page:"):
            return
        try:
            _prefix, slug, page_str = data.split(":", 2)
            page = int(page_str)
        except (ValueError, IndexError):
            return

        slug = slug.strip()
        if not slug or slug == "noop":
            return

        # Handle trending as a special case
        text, cur_page, total_pages, markets = await asyncio.to_thread(_format_markets_with_trades, page, 5, slug)
        if not markets:
            await _safe_edit_message(query, f"No markets found. Try refreshing.")
            return
        keyboard = _build_pagination_keyboard(cur_page, total_pages, slug, markets)
        await _send_banner_with_caption(
            context.bot,
            query.message.chat_id,
            _market_banner_for_slug(slug),
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            max_caption_len=1000,
        )

    async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Explain how to close Polymarket positions."""
        text = (
            "To **close or reduce a Polymarket position**, you place a trade on the **opposite side** "
            "of your current position in that market.\n\n"
            "For example, if you are long **Yes** on `#12`, you can close by trading **No** on the same market:\n"
            "`trade #12 No 20`  — sells out of Yes by buying No with $20.\n\n"
            "Use /portfolio to see your open positions, then use /markets or /category to get the `#ID`, "
            "and finally a `trade #ID Yes|No AMOUNT` command (or the Trade buttons) to close or adjust."
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

    async def handle_help_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle task-based help menu buttons."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("help:"):
            return
        _, action = data.split(":", 1)
        action = action.strip()

        user = query.from_user
        db_user = db.get_user(user.id) if user else None

        if action == "first_trade":
            # Jump straight into markets menu
            await markets_cmd(update, context)
            return

        if action == "account":
            if not db_user:
                await _safe_edit_message(query, "Please run /start first.")
                return
            await _send_account_overview(query.message.chat_id, context.bot, db_user)
            return

        if action == "funds":
            msg = (
                "💳 **Deposit / Withdraw funds**\n\n"
                "• To *deposit*, open **Portfolio & wallet → Deposit** and fund the shown Safe address.\n"
                "• To *withdraw*, use **Portfolio & wallet → Withdraw** to move USDC.e from Safe back to your main wallet.\n"
                "• Use *Claim winnings* after markets resolve."
            )
            await _safe_edit_message(query, msg, parse_mode="Markdown")
            return

        if action == "copy":
            msg = (
                "👥 **Copy Trading**\n\n"
                "Follow top trader wallets and let Anna mirror their trades to your account.\n"
                "You control when copying is enabled, who you follow, and your per-trade risk settings."
            )
            await _safe_edit_message(query, msg, parse_mode="Markdown")
            return

    async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /portfolio command (delegates to unified account view + close buttons)."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return
        # Check onboarding
        is_onboarded, replied = await _check_onboarding(update, context, db_user)
        if replied:
            return
        await update.message.reply_text("Fetching your portfolio, balances, and open positions...")
        await _show_positions_with_close_buttons(update.effective_chat.id, context, db_user)

    async def _show_positions_with_close_buttons(chat_id: int, context: ContextTypes.DEFAULT_TYPE, db_user: dict):
        """Helper: send portfolio view with Close buttons for each open position."""
        portfolio_text, positions = await asyncio.to_thread(
            bot_tools.get_polymarket_portfolio_with_positions,
            db_user["eth_address"],
        )
        keyboard = None
        if positions:
            # Store positions in user_data keyed by chat/user for later close callbacks
            context.user_data["portfolio_positions"] = positions
            buttons = []
            for i, pos in enumerate(positions):
                cid = pos.get("condition_id", "")
                if not cid:
                    continue
                m = market_cache.ensure_market_cached(cid)
                if not m:
                    continue
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"Close (full) – {m.question[:40]}…" if len(m.question) > 40 else f"Close (full) – {m.question}",
                            callback_data=f"close_pos:{i}",
                        )
                    ]
                )
            if buttons:
                keyboard = InlineKeyboardMarkup(buttons)
        await context.bot.send_message(
            chat_id=chat_id,
            text=portfolio_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def handle_portfolio_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle portfolio-related buttons from the account overview."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""

        user = query.from_user
        db_user = db.get_user(user.id) if user else None
        if not db_user:
            if query.message:
                await _safe_edit_message(query, "Please run /start first.")
            return

        address = db_user["eth_address"]

        if data == "portfolio:view":
            # Show detailed positions view with Close buttons
            if query.message:
                await _safe_edit_message(
                    query,
                    "Fetching your open positions…",
                    parse_mode="Markdown",
                )
            await _show_positions_with_close_buttons(query.message.chat_id, context, db_user)
            return

        if data == "withdraw:funds":
            # Withdraw all USDC.e from Safe trading wallet back to EOA.
            if query.message:
                await _safe_edit_message(
                    query,
                    "🔁 Withdrawing USDC.e from your Safe trading wallet back to your main address…",
                    parse_mode="Markdown",
                )
            result = await asyncio.to_thread(
                bot_tools.withdraw_safe_to_eoa,
                address,
                "all",
            )
            if query.message:
                await _safe_edit_message(
                    query,
                    strip_emoji(result),
                    parse_mode="Markdown",
                )
            return

        if data == "transfer:safe":
            # Transfer all bridged USDC.e from EOA to Safe trading wallet.
            if query.message:
                await _safe_edit_message(
                    query,
                    "🔁 Transferring USDC.e from your main address to your Safe trading wallet…",
                    parse_mode="Markdown",
                )
            result = await asyncio.to_thread(
                bot_tools.transfer_usdc_to_safe,
                address,
                "all",
            )
            if query.message:
                await _safe_edit_message(
                    query,
                    strip_emoji(result),
                    parse_mode="Markdown",
                )
            return

        if data == "deposit:wallet":
            # Show Safe (proxy) address + Polymarket Bridge addresses to match web flow.
            # https://docs.polymarket.com/trading/bridge/deposit
            safe_address = await asyncio.to_thread(
                bot_tools.get_safe_address_for_user,
                address,
            )
            deposit_address = safe_address or address

            # Fetch Polymarket Bridge deposit addresses (same as web: cross-chain → USDC.e)
            bridge = await asyncio.to_thread(
                bot_tools.get_polymarket_bridge_deposit_addresses,
                deposit_address,
            )

            chain_labels = {
                "evm": "Ethereum / Arbitrum / Base / Optimism",
                "svm": "Solana",
                "btc": "Bitcoin",
                "tvm": "Tron",
            }

            lines = [
                "💰 *Deposit to your Polymarket wallet*",
                "",
                "*Polygon (USDC.e) — fastest:*",
                f"`{deposit_address}`",
                "",
            ]

            if bridge and isinstance(bridge, dict):
                has_bridge = False
                for key in ("evm", "svm", "btc", "tvm"):
                    val = bridge.get(key)
                    if val and isinstance(val, str):
                        if not has_bridge:
                            lines.append("*Bridge — other chains → USDC.e on Polygon:*")
                            has_bridge = True
                        lines.append(f"• {chain_labels.get(key, key)}: `{val}`")
                if has_bridge:
                    lines.append("")

            lines.append("_Supported assets & minimums:_ docs.polymarket.com/trading/bridge/supported-assets")

            text = "\n".join(lines)
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔁 Move USDC.e EOA → Safe",
                            callback_data="transfer:safe",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Main Menu",
                            callback_data="home:main",
                        )
                    ],
                ]
            )
            if query.message:
                await _safe_edit_message(
                    query,
                    text,
                    parse_mode="Markdown",
                    reply_markup=buttons,
                )
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=256x256&data={deposit_address}"
            try:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=qr_url,
                )
            except Exception:
                pass
            return

    async def handle_close_pos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close the full position immediately (no amount prompt)."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("close_pos:"):
            return
        try:
            idx = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Invalid close request.")
            return
        positions = context.user_data.get("portfolio_positions", [])
        if idx < 0 or idx >= len(positions):
            await _safe_edit_message(query, "Position no longer available. Run /portfolio again.")
            return
        pos = positions[idx]
        cid = pos.get("condition_id", "")
        outcome = pos.get("outcome", "Yes")
        size = float(pos.get("size", 0))
        cur_price = float(pos.get("cur_price", 0))
        if not cid or size <= 0:
            await _safe_edit_message(query, "Cannot close this position.")
            return
        m = market_cache.ensure_market_cached(cid)
        if not m:
            await _safe_edit_message(query, "Market not found. Run /portfolio again.")
            return
        user_id = query.from_user.id if query.from_user else None
        db_user = db.get_user(user_id) if user_id else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return
        await _safe_edit_message(query, f"Selling {size:.2f} {outcome} to close position #{m.market_id}...")
        res = await asyncio.to_thread(
            bot_tools.execute_sell_position,
            m.market_id, outcome, size, db_user["eth_address"],
        )
        await _safe_edit_message(query, res)

    async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Formal /swap command."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if db_user:
            await update.message.reply_text(
                "Initiating USDC.e → bridged USDC swap..."
            )
            res = bot_tools.swap_usdc_for_trading(db_user["eth_address"])
            await update.message.reply_text(res)
        else:
            await update.message.reply_text("Please run /start first.")

    async def transfer_to_safe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Move all bridged USDC.e from the user's EOA into their Safe trading wallet.
        """
        user = update.effective_user
        db_user = db.get_user(user.id)
        if db_user:
            await update.message.reply_text(
                "🔁 Transferring all USDC.e from your main address to your Safe trading wallet…"
            )
            res = await asyncio.to_thread(
                bot_tools.transfer_usdc_to_safe,
                db_user["eth_address"],
                "all",
            )
            await update.message.reply_text(strip_emoji(res))
        else:
            await update.message.reply_text("Please run /start first.")


    async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually run the full gasless approval flow (Safe deploy + USDC/CTF allowances)."""
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return
        address = db_user.get("eth_address", "")
        await update.message.reply_text(
            "🔐 Running gasless approvals (Safe deploy + USDC/CTF allowances)…"
        )
        result = await asyncio.to_thread(bot_tools.approve_usdc_for_trading, address)
        success = not str(result).lstrip().startswith("❌")
        if success:
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;",
                        (user.id,),
                    )
            except Exception:
                pass
        await update.message.reply_text(strip_emoji(result))

    async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Place a LIMIT order via text command.

        Syntax:
          /limit CONDITION_ID SIDE PRICE SIZE
        Example:
          /limit 0xabc... Yes 0.45 20
        """
        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return

        parts = (update.message.text or "").split()
        if len(parts) != 5:
            await update.message.reply_text(
                "Usage:\n`/limit CONDITION_ID SIDE PRICE SIZE`\n"
                "Example: `/limit 0xabc... Yes 0.45 20`",
                parse_mode="Markdown",
            )
            return

        _, condition_id, side, price, size = parts

        await update.message.reply_text(
            "Placing limit order…",
        )

        from trading import execute_limit_order_for_user

        result = await asyncio.to_thread(
            execute_limit_order_for_user,
            db,
            db_user["user_id"],
            side,
            float(price),
            float(size),
            condition_id,
            "BUY",
        )
        await update.message.reply_text(result, parse_mode="Markdown")

    async def autotrader_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Manual autotrader signal execution command.

        Syntax:
          /autotrader SIGNAL SIDE TIMEFRAME [AMOUNT]
        Example:
          /autotrader BTC YES 5m
          /autotrader BTC NO 15m 10

        Supported timeframes: 1m, 5m, 15m, 30m, 1h
        """
        if not AUTOTRADER_AVAILABLE:
            await update.message.reply_text(
                "❌ Autotrader is not available in this instance."
            )
            return

        user = update.effective_user
        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text("Please run /start first.")
            return

        parts = (update.message.text or "").split()
        if len(parts) < 4:
            await update.message.reply_text(
                "Usage:\n`/autotrader SIGNAL SIDE TIMEFRAME [AMOUNT]`\n\n"
                "Examples:\n"
                "`/autotrader BTC YES 5m`\n"
                "`/autotrader BTC NO 15m 10`\n\n"
                "Supported timeframes: *1m, 5m, 15m, 30m, 1h*",
                parse_mode="Markdown",
            )
            return

        _, signal_asset, side, timeframe, *amount_parts = parts
        amount = float(amount_parts[0]) if amount_parts else None

        # Validate side
        side_upper = side.upper()
        if side_upper not in ("YES", "NO"):
            await update.message.reply_text(
                "Side must be *YES* or *NO*.",
                parse_mode="Markdown",
            )
            return

        # Validate timeframe
        timeframe_lower = timeframe.lower()
        if timeframe_lower not in TIMEFRAME_TO_SERIES:
            await update.message.reply_text(
                f"Unsupported timeframe: *{timeframe}*\n\n"
                f"Supported: *{', '.join(TIMEFRAME_TO_SERIES.keys())}*",
                parse_mode="Markdown",
            )
            return

        # Build signal dict
        signal = {
            "signal": side_upper,
            "timeframe": timeframe_lower,
            "signal_ts": int(time.time()),
            "market_end_ts": int(time.time()) + 300,  # 5 minutes from now
        }

        await update.message.reply_text(
            f"🔄 Executing autotrader signal:\n"
            f"• Signal: *{signal_asset}*\n"
            f"• Side: *{side_upper}*\n"
            f"• Timeframe: *{timeframe_upper}*\n"
            f"• Amount: *${amount if amount else 'default'}*\n\n"
            "Processing...",
            parse_mode="Markdown",
        )

        try:
            trade_amount = amount or float(db_user.get("signal_trade_amount_usd") or 3.0)

            manager = AutoTraderManager(
                db=db,
                user_id=int(db_user["user_id"]),
                trade_amount_usd=trade_amount,
            )

            traded, result = manager.process_signal(signal)

            if traded:
                await update.message.reply_text(
                    f"✅ *Trade Executed*\n\n{result}",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"⚠️ *Signal Skipped*\n\n{result}",
                    parse_mode="Markdown",
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Answers any normal text message using the LLM and the user's wallet context."""
        user = update.effective_user
        user_text = (update.message.text or "").strip()

        db_user = db.get_user(user.id)
        if not db_user:
            await update.message.reply_text(
                "Please run /start first to generate your wallet and see the command menu."
            )
            return

        # Custom shares input for signal trading.
        timeframe = _pending_signal_amount_input.get(int(user.id))
        if timeframe:
            try:
                shares = float(user_text.strip())
                if shares < 5:
                    raise ValueError
            except Exception:
                await update.message.reply_text("Please send at least 5 shares.")
                return

            # Determine which column to update based on timeframe
            if timeframe == "5m":
                col = "signal_5m_amount_usd"
            elif timeframe == "15m":
                col = "signal_15m_amount_usd"
            else:
                col = "signal_trade_amount_usd"  # legacy

            with db.transaction() as conn:
                conn.execute(
                    f"UPDATE users SET {col} = ? WHERE user_id = ?;",
                    (shares, int(user.id)),
                )
            _pending_signal_amount_input.pop(int(user.id), None)
            db_user = db.get_user(int(user.id)) or db_user
            await _render_signal_trading_menu(update.effective_chat.id, context.bot, db_user)
            return

        # Manual follow flow: first capture wallet, then ask for risk settings.
        if context.user_data.get("awaiting_follow_wallet"):
            import re as _re

            addr = user_text.strip()
            if not _re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
                await update.message.reply_text(
                    "That doesn't look like a valid wallet address.\n\n"
                    "Please send a Polygon wallet address like:\n"
                    "0xabc123...",
                )
                return

            context.user_data["awaiting_follow_wallet"] = False
            context.user_data["pending_follow_wallet"] = addr
            context.user_data["pending_follow_name"] = _refetch_polymarket_username(addr) or addr
            context.user_data["awaiting_follow_risk"] = True
            await _send_follow_risk_menu(update.message, addr)
            return

        # Second step of follow flow: read max_usd_per_trade, then ask for sizing mode.
        if context.user_data.get("awaiting_follow_risk"):
            pending_wallet = context.user_data.get("pending_follow_wallet")
            if not pending_wallet:
                context.user_data["awaiting_follow_risk"] = False
                return

            txt = user_text.strip().lower()
            if txt == "none":
                max_per = 0.0
            else:
                try:
                    max_per = float(txt)
                    if max_per <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text(
                        "Please send a positive number (e.g. 10) or 'none'.",
                    )
                    return

            # Store cap and ask for sizing mode next.
            context.user_data["pending_follow_max_per"] = max_per
            context.user_data["awaiting_follow_risk"] = False
            context.user_data["awaiting_follow_mode"] = True
            await _send_follow_mode_menu(update.message)
            return

        # Final step of follow flow: sizing mode -> create hook.
        if context.user_data.get("awaiting_follow_mode"):
            pending_wallet = context.user_data.get("pending_follow_wallet")
            max_per = context.user_data.get("pending_follow_max_per", 0.0)
            if not pending_wallet:
                context.user_data["awaiting_follow_mode"] = False
                return

            mode_txt = user_text.strip().lower()
            if mode_txt in ("fractional", "f"):
                mode = "fractional"
            elif mode_txt in ("1:1", "one_to_one", "one-to-one", "one to one"):
                mode = "one_to_one"
            elif mode_txt in ("beginner", "dummy", "small"):
                mode = "beginner"
            else:
                await update.message.reply_text(
                    "Please reply with 'fractional', '1:1', or 'beginner'.",
                )
                return

            msg, _ = await _finalize_follow_for_user(
                db_user=db_user,
                context=context,
                pending_wallet=pending_wallet,
                max_per=max_per,
                mode=mode,
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # Main Menu routing: show home card
        if user_text in ("🏠 Main Menu", "Main Menu", "Menu", "/menu"):
            await _send_home(update, context, db_user)
            return

        # If user has active market+side (waiting for amount) and types a number, execute trade directly
        ctx = active_market_context.get(user.id)
        if ctx and ctx.get("side") and (ctx.get("condition_id") or ctx.get("market_id")):
            amt_match = re.match(r"^\s*\$?\s*(\d+(?:\.\d+)?)\s*(?:\$|usd)?\s*$", user_text, re.I)
            if amt_match:
                amount = amt_match.group(1)
                try:
                    amt_float = float(amount)
                    if amt_float >= 1.0:
                        condition_id = ctx.get("condition_id")
                        if not condition_id and ctx.get("market_id"):
                            m = market_cache.get(ctx["market_id"])
                            condition_id = m.condition_id if m else None
                        if condition_id:
                            res = await asyncio.to_thread(
                                execute_trade_for_user,
                                db,
                                db_user["user_id"],
                                ctx["side"],
                                amount,
                                condition_id,
                            )
                            await update.message.reply_text(res)
                            return
                except (ValueError, TypeError):
                    pass

        # Quick shortcuts: bypass LLM for common text aliases
        t = user_text.lower().strip()
        if t in ("markets", "m", "trending", "market"):
            await markets_cmd(update, context)
            return
        if t in ("balance", "bal"):
            await _send_safe_balance(update, context, db_user)
            return
        if t in ("wallet", "account", "portfolio", "port"):
            await _send_account_overview(update.effective_chat.id, context.bot, db_user)
            return

        # Initialize chat history for the user
        if user.id not in chat_sessions:
            system_prompt = f"""Anna: Polymarket AI assistant. User: {user.first_name}. Wallet: {db_user['eth_address']}

RULES:
- Balance: use get_polygon_balance. Markets: get_polymarket_markets (generic), search_polymarket_events (topic), get_polymarket_markets_by_category (category slug).
- Start market replies with "Here are some active markets:".
- Trading: NEVER execute without explicit "yes"/"confirm". Ask confirmation first, then execute_trade with wallet address.
- Call tools silently. No "let me fetch". If you need data, call the tool first.
- Forecasts: use search_news + search_polymarket_events, state odds, compare your estimate to market, recommend Yes/No."""
            chat_sessions[user.id] = [{"role": "system", "content": system_prompt}]

            # If the user already selected a market via buttons/commands before
            # starting this chat, seed the conversation with that context so the
            # LLM can resolve phrases like "this market" or "that one".
            ctx = active_market_context.get(user.id)
            if ctx:
                m = ctx.get("market")
                side = ctx.get("side")
                parts: list[str] = []
                if m:
                    parts.append(
                        f"ACTIVE MARKET CONTEXT: #{ctx.get('market_id')} — {getattr(m, 'question', '')}"
                    )
                    yes_odds = getattr(m, "odds", {}).get("Yes", "?")
                    no_odds = getattr(m, "odds", {}).get("No", "?")
                    parts.append(f"Yes odds: {yes_odds}¢, No odds: {no_odds}¢.")
                else:
                    parts.append(
                        f"ACTIVE MARKET CONTEXT: #{ctx.get('market_id')} (details cached in backend)."
                    )
                if side:
                    parts.append(f"User last tapped side: {side}.")
                parts.append(
                    "If the user refers to 'this market' or 'that one', assume they mean this ACTIVE market unless they specify another."
                )
                chat_sessions[user.id].append(
                    {"role": "assistant", "content": " ".join(parts)}
                )

        # Append the user's new message
        chat_sessions[user.id].append({"role": "user", "content": user_text})

        # Truncate to last 12 messages (system + 6 turns) to keep LLM prompt small
        msgs = chat_sessions[user.id]
        if len(msgs) > 13:
            system = next((m for m in msgs if m.get("role") == "system"), None)
            rest = [m for m in msgs if m.get("role") != "system"][-12:]
            chat_sessions[user.id] = ([system] if system else []) + rest

        # Show a typing indicator while we wait for a single-shot response
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )

        try:
            reply_text = await llm.get_chat_response(chat_sessions[user.id])
            await _send_long_message(
                context.bot,
                update.effective_chat.id,
                reply_text,
                parse_mode="Markdown",
            )
            # Persist assistant message back into the chat session
            chat_sessions[user.id].append(
                {"role": "assistant", "content": reply_text}
            )
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
            await update.message.reply_text(
                "I couldn't process that request right now. Please try again in a few seconds "
                "or use menu buttons for faster actions."
            )

    async def handle_market_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle market detail page: show full market info with trade, analyze, and position buttons."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if not data.startswith("market:detail:"):
            return
        try:
            identifier = data.split(":", 2)[2]
        except (ValueError, IndexError):
            return

        market_id, condition_id, m = _resolve_market_identifier(identifier)
        if not m:
            await _safe_edit_message(query, "Market not found. Run /markets to refresh.")
            return

        # Store market context for this user
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None:
            active_market_context[user_id] = {
                "market_id": market_id,
                "condition_id": condition_id,
                "side": None,
                "market": m,
            }

        # Build market detail text
        yes_odds = m.odds.get("Yes", 0)
        no_odds = m.odds.get("No", 0)
        vol_24h = m.volume_24h or 0
        liq = m.liquidity or 0

        vol_str = f"${vol_24h:,.0f}" if vol_24h > 0 else "—"
        liq_str = f"${liq:,.0f}" if liq > 0 else "—"

        end_raw = m.end_date or ""
        pretty_end = "TBD"
        if end_raw:
            try:
                iso = end_raw
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                from datetime import datetime
                dt = datetime.fromisoformat(iso)
                pretty_end = dt.strftime("%b %d, %Y")
            except Exception:
                pretty_end = end_raw

        # Get user's position in this market
        user_pos = (
            _get_user_position_in_market(db, query.from_user.id, market_id)
            if query.from_user and isinstance(market_id, int)
            else None
        )
        pos_text = f"Your Position: {user_pos}" if user_pos else "Your Position: None"

        display_id = f"{market_id}" if isinstance(market_id, int) else f"`{(condition_id or '')[:10]}...`"
        detail_text = f"""🔍 *Market {display_id}*

*Question:* {m.question}

*Odds:*
✅ Yes: {yes_odds}¢
❌ No: {no_odds}¢

*Stats:*
24h Volume: {vol_str}
Liquidity: {liq_str}
Expires: {pretty_end}

_{pos_text}_

_Tap buttons below to trade or analyze._"""

        # Send with detail keyboard
        keyboard = _build_market_detail_keyboard(m)
        await _send_banner_with_caption(
            context.bot,
            query.message.chat_id,
            _market_image_for_market(m),
            detail_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            max_caption_len=1000,
        )

    async def handle_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button tap: show market details and trade options, and update LLM context."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if not data.startswith("market:"):
            return
        try:
            market_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return

        details = bot_tools.get_market_by_id(market_id)
        if "not found" in details.lower():
            await _safe_edit_message(query, details)
            return

        # Cache structured market object (if available) for richer context
        m = market_cache.get(market_id)
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None:
            active_market_context[user_id] = {
                "market_id": market_id,
                "side": None,
                "market": m,
            }
            # If there's already an LLM session, append a short context message so
            # future chat like "buy 10 on this" can be resolved.
            if user_id in chat_sessions:
                ctx_text_parts: list[str] = []
                if m:
                    ctx_text_parts.append(
                        f"ACTIVE MARKET CONTEXT: #{market_id} — {getattr(m, 'question', '')}"
                    )
                    yes_odds = getattr(m, "odds", {}).get("Yes", "?")
                    no_odds = getattr(m, "odds", {}).get("No", "?")
                    ctx_text_parts.append(
                        f"Yes odds: {yes_odds}¢, No odds: {no_odds}¢."
                    )
                else:
                    ctx_text_parts.append(
                        f"ACTIVE MARKET CONTEXT: #{market_id} (details cached in backend)."
                    )
                ctx_text_parts.append(
                    "If the user later says 'this market' or 'that one', assume they mean this ACTIVE market."
                )
                chat_sessions[user_id].append(
                    {"role": "assistant", "content": " ".join(ctx_text_parts)}
                )

        # Add Trade Yes / Trade No buttons + Back button
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Trade Yes", callback_data=f"trade:{market_id}:Yes"
                    ),
                    InlineKeyboardButton(
                        "Trade No", callback_data=f"trade:{market_id}:No"
                    ),
                ],
                [
                    InlineKeyboardButton("⬅️ Back to markets", callback_data="markets:back"),
                    InlineKeyboardButton("🔍 Analyze market", callback_data=f"analyze:{market_id}"),
                ],
            ]
        )
        # Escape special Markdown characters in the details text
        escaped_details = escape_markdown_v2(details)
        await _send_banner_with_caption(
            context.bot,
            query.message.chat_id,
            _market_image_for_market(m),
            escaped_details + "\n\n_Tap a button below to trade\\._",
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
            max_caption_len=1000,
        )

    async def handle_analyze_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Analyze a specific market using the LLM and send a forecast."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("analyze:"):
            return
        try:
            identifier = data.split(":", 1)[1]
        except (ValueError, IndexError):
            return

        market_id, condition_id, m = _resolve_market_identifier(identifier)
        if not m:
            await _safe_edit_message(query, "Market no longer in cache. Run /markets to refresh.")
            return

        question = _mget(m, "question", "") or "this market"

        # Run the multi-stage news-grounded analysis using the market question
        await _safe_edit_message(
            query,
            f"🔍 Analyzing market:\n\n{question}\n\nPlease wait…",
            parse_mode="Markdown",
        )
        try:
            analysis = await llm.run_market_analysis(question)
        except Exception as e:
            logger.error(f"Error analyzing market {identifier}: {e}")
            await _safe_edit_message(
                query,
                "I couldn't analyze this market right now. Please retry in a few seconds.",
                parse_mode="Markdown",
            )
            return

        # Stable formatter for Telegram: convert markdown-like headings/bullets into plain text.
        # We intentionally avoid parse_mode here so mixed markdown/unicode bullets always render.
        def _format_analysis_for_telegram(text: str) -> str:
            out_lines: list[str] = []
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    title = stripped[3:].strip()
                    if out_lines and out_lines[-1] != "":
                        out_lines.append("")
                    out_lines.append(title.upper())
                elif stripped.startswith("### "):
                    title = stripped[4:].strip()
                    if out_lines and out_lines[-1] != "":
                        out_lines.append("")
                    out_lines.append(title)
                elif stripped.startswith("- "):
                    bullet = stripped[2:].strip()
                    out_lines.append(f"• {bullet}")
                elif stripped.startswith("• "):
                    out_lines.append(f"• {stripped[2:].strip()}")
                else:
                    # Keep line content, but strip markdown emphasis markers for clean plaintext render.
                    cleaned = line.replace("**", "").replace("`", "")
                    out_lines.append(cleaned)
            return "\n".join(out_lines)

        telegram_text = _format_analysis_for_telegram(analysis)

        # Send analysis as a separate message to keep the trade UI intact
        await _send_long_message(
            context.bot,
            query.message.chat_id,
            telegram_text,
        )

        # After analysis, prompt user with Buy Yes / Buy No options.
        trade_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Buy Yes", callback_data=f"trade:{identifier}:Yes"
                    ),
                    InlineKeyboardButton(
                        "❌ Buy No", callback_data=f"trade:{identifier}:No"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back to markets", callback_data="markets:back"
                    ),
                ],
            ]
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Ready to trade this market? Choose a side:",
            reply_markup=trade_keyboard,
        )

    async def handle_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Trade Yes/No button: prompt for amount and update active trade context."""
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest:
            return
        data = query.data or ""
        if not data.startswith("trade:"):
            return
        try:
            if data.startswith("trade:open:"):
                # New format: trade:open:<identifier>:<Yes|No>
                _, _, identifier, side = data.split(":", 3)
            else:
                # Legacy format: trade:<identifier>:<Yes|No>
                _, identifier, side = data.split(":", 2)
        except (ValueError, IndexError):
            return

        market_id, condition_id, m = _resolve_market_identifier(identifier)

        if not m:
            await _safe_edit_message(query, "Market not found. Run /markets to refresh.")
            return

        user_id = query.from_user.id if query.from_user else None
        if user_id is not None:
            active_market_context[user_id] = {
                "market_id": market_id,
                "condition_id": condition_id,
                "side": side,
                "market": m,
            }
            if user_id in chat_sessions:
                yes_odds = getattr(m, "odds", {}).get("Yes", "?")
                no_odds = getattr(m, "odds", {}).get("No", "?")
                ctx_market_id = market_id if market_id else f"cond:{condition_id[:8]}"
                chat_sessions[user_id].append(
                    {
                        "role": "assistant",
                        "content": (
                            f"User tapped Trade {side} on ACTIVE market "
                            f"#{ctx_market_id} — {m.question}. "
                            f"(Yes: {yes_odds}¢, No: {no_odds}¢). "
                            "Awaiting amount in USD for execution."
                        ),
                    }
                )

        db_user = (
            db.get_user(update.effective_user.id) if update.effective_user else None
        )
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        amount_buttons = [
            [
                InlineKeyboardButton("$1", callback_data=f"trade_amt:{identifier}:{side}:1"),
                InlineKeyboardButton("$5", callback_data=f"trade_amt:{identifier}:{side}:5"),
            ],
            [
                InlineKeyboardButton("$10", callback_data=f"trade_amt:{identifier}:{side}:10"),
                InlineKeyboardButton("$20", callback_data=f"trade_amt:{identifier}:{side}:20"),
            ],
            [
                InlineKeyboardButton("$50", callback_data=f"trade_amt:{identifier}:{side}:50"),
                InlineKeyboardButton("$100", callback_data=f"trade_amt:{identifier}:{side}:100"),
            ],
        ]
        display_id = market_id if market_id is not None else (condition_id or identifier)[:10]
        question = _mget(m, "question", "") or "this market"
        odds_map = _mget(m, "odds", {}) or {}
        odds_val = odds_map.get(side, "?")
        msg = (
            f"**Trade {display_id}** — {side}\n\n"
            f"Question: {question}\n"
            f"Odds: {odds_val}¢\n\n"
            f"Choose amount or type: `trade {display_id} {side} 10`"
        )
        await _safe_edit_message(
            query,
            msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(amount_buttons)
        )

    async def handle_trade_amt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute trade when user taps amount button."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if not data.startswith("trade_amt:"):
            return
        try:
            parts = data.split(":")
            identifier = parts[1]  # Can be numeric ID or condition_id
            side = parts[2]
            amount = parts[3]
        except (ValueError, IndexError):
            return

        market_id, condition_id, m = _resolve_market_identifier(identifier)

        if not m:
            await _safe_edit_message(query, "Market not found. Run /markets to refresh.")
            return

        db_user = db.get_user(query.from_user.id) if query.from_user else None
        if not db_user:
            await _safe_edit_message(query, "Please run /start first.")
            return

        exec_condition_id = condition_id or (m.condition_id if m else None)
        if not exec_condition_id:
            await _safe_edit_message(query, "Could not resolve market for trade. Please refresh markets.")
            return
        res = await asyncio.to_thread(
            execute_trade_for_user,
            db,
            db_user["user_id"],
            side,
            amount,
            exec_condition_id,
        )
        await _safe_edit_message(query, res)

    async def post_init(app: Application) -> None:
        """Set bot command menu when application starts."""
        global TELEGRAM_BOT_USERNAME
        try:
            me = await app.bot.get_me()
            if me and me.username:
                TELEGRAM_BOT_USERNAME = me.username
            await app.bot.set_my_commands(BOT_COMMANDS)
        except TimedOut:
            # Network hiccup talking to Telegram; proceed without failing startup.
            logger.warning("Timed out while setting bot commands; continuing without updating command menu.")
        except Exception as e:
            # Catch-all to prevent bot startup from crashing due to Telegram/httpx issues.
            logger.warning(f"Failed to set bot commands during post_init: {e}")

        # Start background copy-trading notification loop (fire-and-forget).
        try:
            asyncio.create_task(_copy_trading_notification_loop(app))
        except Exception as e:
            logger.warning(f"Failed to start copy-trading notification loop: {e}")

        # Start background signal broadcast loop (fire-and-forget).
        try:
            asyncio.create_task(_announcement_signal_broadcast_loop(app))
        except Exception as e:
            logger.warning(f"Failed to start signal broadcast loop: {e}")

        # Start background outbox delivery loop (signal-trading notifications).
        try:
            asyncio.create_task(_signal_outbox_delivery_loop(app))
        except Exception as e:
            logger.warning(f"Failed to start signal outbox delivery loop: {e}")

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler so Telegram exceptions are handled gracefully."""
        err = context.error
        user_msg = _friendly_error_message(err)
        if user_msg is None:
            # Intentionally ignore noise such as "Message is not modified".
            return

        logger.exception("Unhandled bot error: %s", err)
        try:
            if update and getattr(update, "callback_query", None):
                query = update.callback_query
                await _safe_edit_message(query, user_msg)
                return
            if update and getattr(update, "effective_chat", None):
                await context.bot.send_message(chat_id=update.effective_chat.id, text=user_msg)
        except Exception:
            pass

    # Build application and register handlers
    app = (
        ApplicationBuilder()
        .token(bot_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(
        CommandHandler(
            ["copy", "copytrade"],
            lambda u, c: asyncio.create_task(
                _send_copy_trading_state(
                    u.effective_chat.id,
                    c.bot,
                    db.get_user(u.effective_user.id),
                )
            ),
        )
    )
    app.add_handler(CommandHandler("follow", follow_wallet_cmd))
    app.add_handler(CommandHandler("copyboard", copyboard_cmd))
    app.add_handler(CommandHandler("markets", markets_cmd))
    app.add_handler(CommandHandler("trending", markets_cmd))
    app.add_handler(CommandHandler("category", category_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("swap", swap_cmd))
    app.add_handler(CommandHandler("transfer_to_safe", transfer_to_safe_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("commands", help_cmd))
    app.add_handler(CommandHandler("autotrader", autotrader_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Inline button callbacks: markets menu, portfolio menu, category, market selection, trade
    app.add_handler(CallbackQueryHandler(handle_markets_menu_callback, pattern=r"^markets:(trending|volume|closing|category|back)$"))
    app.add_handler(CallbackQueryHandler(handle_markets_page_callback, pattern=r"^markets_page:[^:]+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_portfolio_menu_callback, pattern=r"^(portfolio:view|deposit:wallet|withdraw:funds|transfer:safe)$"))
    app.add_handler(CallbackQueryHandler(handle_help_menu_callback, pattern=r"^help:(first_trade|account|funds|copy)$"))
    app.add_handler(
        CallbackQueryHandler(
            handle_copycfg_callback,
            pattern=r"^copycfg:(enable|disable|refresh|leaders(?::\d+)?|follow_manual|unfollow_all|all_leaders)$",
        )
    )
    app.add_handler(CallbackQueryHandler(handle_unfollow_callback, pattern=r"^copyunfollow:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_home_callback, pattern=r"^home:(markets|copy|portfolio|wallet|smart_wallets|signals|refresh|limit_orders|referrals|settings|help|main)$"))
    app.add_handler(CallbackQueryHandler(handle_signals_callback, pattern=r"^signals:(5m:enable|5m:disable|5m:shares|5m:amt:.+|15m:enable|15m:disable|15m:shares|15m:amt:.+|enable|disable|amt:.+|back)$"))
    app.add_handler(CallbackQueryHandler(handle_copy_callback, pattern=r"^copy:(view_more|.+)$"))
    app.add_handler(CallbackQueryHandler(handle_copyfollow_callback, pattern=r"^copyfollow:0x[a-fA-F0-9]{40}$"))
    app.add_handler(CallbackQueryHandler(handle_copyrisk_callback, pattern=r"^copyrisk:(none|cancel|[\d.]+)$"))
    app.add_handler(CallbackQueryHandler(handle_copymode_callback, pattern=r"^copymode:(fractional|one_to_one|beginner|cancel)$"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^settings:(pk|copy_pk)$"))
    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^category:[a-z0-9\-]+$"))
    app.add_handler(CallbackQueryHandler(handle_close_pos_callback, pattern=r"^close_pos:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_market_detail_callback, pattern=r"^market:detail:[^:]+$"))
    app.add_handler(CallbackQueryHandler(handle_market_callback, pattern=r"^market:(?!detail:)\d+$"))
    app.add_handler(CallbackQueryHandler(handle_analyze_market_callback, pattern=r"^analyze:[^:]+$"))
    # Trade handlers: support both numeric IDs and string condition IDs
    app.add_handler(CallbackQueryHandler(handle_trade_callback, pattern=r"^trade:(.+):(Yes|No)$"))
    app.add_handler(CallbackQueryHandler(handle_trade_amt_callback, pattern=r"^trade_amt:(.+):(Yes|No):[\d.]+$"))

    # Copy address handler
    app.add_handler(CallbackQueryHandler(handle_copy_address_callback, pattern=r"^copy:(safe|eoa):.+"))

    # Invite code handlers
    app.add_handler(CallbackQueryHandler(_on_invite_code_callback, pattern=r"^enter_invite_code$"))
    app.add_handler(CallbackQueryHandler(_on_cancel_invite_code_callback, pattern=r"^cancel_invite_code$"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            _handle_invite_code_input,
        )
    )

    # Catch ALL non-command text messages and pipe them to the LLM
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_error_handler(_on_error)

    return app
