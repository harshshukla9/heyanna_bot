import asyncio
import os
import re
import logging
import time
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

import market_cache
import wallets
import bot_tools
import llm
from database_manager import DatabaseManager

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
db = DatabaseManager()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# In-memory session context for conversation history (User ID -> List of Dict messages)
chat_sessions: dict[int, list[dict]] = {}

def escape_markdown_v2(text: str) -> str:
    """
    Escape special characters for Telegram MarkdownV2 parse mode.
    Characters that need escaping: _ * [ ] ( ) { } # + - = | { } > < -
    """
    if not text:
        return text
    special_chars = r"_\*[](){}~`>#+-=|{}.!"
    result = []
    for char in text:
        if char in special_chars:
            result.append("\\")
        result.append(char)
    return "".join(result)

# In-memory per-user active market context so the LLM can resolve
# "this market" / "that one" back to a concrete Polymarket market.
active_market_context: dict[int, dict] = {}

# Persistent command UI keyboard
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["/wallet", "/balance", "/portfolio"],
        ["/markets", "/trending", "/category"],
        ["/swap", "/approve", "/close", "/help"],
        ["/menu"],
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
    BotCommand("markets", "Browse trending Polymarket events"),
    BotCommand("trending", "Alias for /markets"),
    BotCommand("category", "Markets by category (politics, crypto, etc.)"),
    BotCommand("swap", "Swap USDC.e → bridged USDC"),
    BotCommand("approve", "Polymarket approval flow"),
    BotCommand("close", "How to close positions"),
    BotCommand("menu", "Show command buttons"),
    BotCommand("help", "List all commands"),
]

# Category submenu: (label, tag_slug) for Polymarket Gamma API
CATEGORY_OPTIONS = [
    ("Politics", "politics"),
    ("Crypto", "crypto"),
    ("Finance", "finance"),
    ("Sports", "sports"),
    ("Science", "science"),
    ("AI", "ai"),
    ("Pop Culture", "pop-culture"),
]


def _build_category_keyboard():
    """Inline keyboard for category submenu."""
    buttons = []
    row = []
    for label, slug in CATEGORY_OPTIONS:
        row.append(InlineKeyboardButton(label, callback_data=f"category:{slug}"))
        if len(row) >= 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _build_markets_keyboard():
    markets = market_cache.list_all()
    if not markets:
        return None
    buttons = []
    row = []
    for m in markets:
        # Use URL hyperlink if available, otherwise fallback to callback
        btn_label = f"#{m.market_id}"
        if m.url:
            button = InlineKeyboardButton(btn_label, url=m.url)
        else:
            button = InlineKeyboardButton(btn_label, callback_data=f"market:{m.market_id}")
        row.append(button)
        if len(row) >= 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /start command to initialize a user."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    
    if not db_user:
        await update.message.reply_text("Welcome! Generating your Ethereum/Polygon wallet for Polymarket. Please wait a moment...")
        eth_wallet = wallets.generate_eth_wallet()
        
        # We store empty strings for Solana since the user requested to disable it for now.
        db.create_user(
            user_id=user.id,
            username=user.username or "",
            eth_data=eth_wallet,
            sol_data=("", "")
        )
        db_user = db.get_user(user.id)
        
        welcome_msg = (
            f"Hello {user.first_name}! I am Anna, your Polymarket trading assistant.\n\n"
            f"I have generated an EVM wallet for you (Polygon):\n"
            f"`{db_user['eth_address']}`\n\n"
            f"**Two ways to use me:**\n"
            f"• **Commands** (buttons below) — Quick lookups: balance, portfolio, markets\n"
            f"• **Chat** — Type any message to ask questions, get forecasts, or trade"
        )
        await update.message.reply_text(
            welcome_msg, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            f"Welcome back {user.first_name}! Wallet: `{db_user['eth_address']}`\n\n"
            f"Use the **buttons** for lookups, or **type a message** to chat.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /wallet command."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    if db_user:
        await update.message.reply_text(f"Your EVM/Polygon Wallet:\n`{db_user['eth_address']}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("Please run /start first.")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /balance command."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    if db_user:
        await update.message.reply_text("Fetching your live Polygon balance...")
        bal = bot_tools.get_polygon_balance(db_user['eth_address'])
        await update.message.reply_text(bal, parse_mode="Markdown")
    else:
        await update.message.reply_text("Please run /start first.")

async def markets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /markets command."""
    await update.message.reply_text("Fetching trending Polymarkets...")
    markets_text = bot_tools.get_polymarket_markets()
    keyboard = _build_markets_keyboard()
    await update.message.reply_text(markets_text, reply_markup=keyboard)

async def category_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show category submenu or fetch markets if slug provided."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📂 **Choose a category:**",
            parse_mode="Markdown",
            reply_markup=_build_category_keyboard(),
        )
        return
    category = " ".join(args).strip().lower()
    await update.message.reply_text(f"Fetching markets in '{category}'...")
    markets_text = bot_tools.get_polymarket_markets_by_category(category)
    keyboard = _build_markets_keyboard()
    await update.message.reply_text(markets_text, reply_markup=keyboard)


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category button tap: fetch markets for that category."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("category:"):
        return
    slug = data.split(":", 1)[1].strip()
    if not slug:
        return
    label = next((lbl for lbl, s in CATEGORY_OPTIONS if s == slug), slug)
    await query.edit_message_text(f"Fetching markets in **{label}**...", parse_mode="Markdown")
    markets_text = bot_tools.get_polymarket_markets_by_category(slug)
    keyboard = _build_markets_keyboard()
    await query.edit_message_text(markets_text, reply_markup=keyboard)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the command keyboard (restore if hidden)."""
    await update.message.reply_text(
        "📋 **Command menu** — Tap a button for quick lookups, or type any message to chat with me.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /help command to list all commands."""
    help_text = (
        "**Commands (lookups)** — Use buttons or type:\n"
        "/wallet — Your Polygon address\n"
        "/balance — Token balances\n"
        "/portfolio — Funds + positions & PnL\n"
        "/markets — Trending Polymarket events\n"
        "/category — Markets by category (tap to choose)\n"
        "/swap — USDC.e → bridged USDC\n"
        "/approve — Polymarket approvals\n"
        "/close — How to close or reduce a position\n"
        "/menu — Show command buttons\n\n"
        "**Chat** — Type any message (e.g. \"What's my balance?\", \"Suggest markets\") to talk to me."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

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

async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /portfolio command with Close buttons per position."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    if not db_user:
        await update.message.reply_text("Please run /start first.")
        return
    await update.message.reply_text("Fetching your Polymarket portfolio...")
    portfolio_text, positions = bot_tools.get_polymarket_portfolio_with_positions(
        db_user["eth_address"]
    )
    keyboard = None
    if positions:
        context.user_data["portfolio_positions"] = positions
        buttons = []
        for i, pos in enumerate(positions):
            cid = pos.get("condition_id", "")
            if not cid:
                continue
            m = market_cache.ensure_market_cached(cid)
            if not m:
                continue
            buttons.append([
                InlineKeyboardButton(
                    f"Close #{m.market_id} (full)",
                    callback_data=f"close_pos:{i}",
                )
            ])
        if buttons:
            keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        portfolio_text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_close_pos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close the full position immediately (no amount prompt)."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("close_pos:"):
        return
    try:
        idx = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid close request.")
        return
    positions = context.user_data.get("portfolio_positions", [])
    if idx < 0 or idx >= len(positions):
        await query.edit_message_text("Position no longer available. Run /portfolio again.")
        return
    pos = positions[idx]
    cid = pos.get("condition_id", "")
    outcome = pos.get("outcome", "Yes")
    size = float(pos.get("size", 0))
    cur_price = float(pos.get("cur_price", 0))
    if not cid or size <= 0:
        await query.edit_message_text("Cannot close this position.")
        return
    m = market_cache.ensure_market_cached(cid)
    if not m:
        await query.edit_message_text("Market not found. Run /portfolio again.")
        return
    user_id = query.from_user.id if query.from_user else None
    db_user = db.get_user(user_id) if user_id else None
    if not db_user:
        await query.edit_message_text("Please run /start first.")
        return
    await query.edit_message_text(f"Selling {size:.2f} {outcome} to close position #{m.market_id}...")
    res = await asyncio.to_thread(
        bot_tools.execute_sell_position,
        m.market_id, outcome, size, db_user["eth_address"],
    )
    await query.edit_message_text(res)

async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /swap command."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    if db_user:
        await update.message.reply_text("Initiating USDC.e → bridged USDC swap...")
        res = bot_tools.swap_usdc_for_trading(db_user['eth_address'])
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("Please run /start first.")

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /approve command."""
    user = update.effective_user
    db_user = db.get_user(user.id)
    if db_user:
        await update.message.reply_text("Setting up Polymarket approvals (6 transactions)...")
        res = bot_tools.approve_usdc_for_trading(db_user['eth_address'])
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("Please run /start first.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers any normal text message using the LLM and the user's wallet context."""
    user = update.effective_user
    user_text = (update.message.text or "").strip()

    db_user = db.get_user(user.id)
    if not db_user:
        await update.message.reply_text("Please run /start first to generate your wallet and see the command menu.")
        return

    # If user has active market+side (waiting for amount) and types a number, execute trade directly
    ctx = active_market_context.get(user.id)
    if ctx and ctx.get("side") and ctx.get("market_id"):
        amt_match = re.match(r"^\s*\$?\s*(\d+(?:\.\d+)?)\s*(?:\$|usd)?\s*$", user_text, re.I)
        if amt_match:
            amount = amt_match.group(1)
            try:
                amt_float = float(amount)
                if amt_float >= 1.0:
                    m = market_cache.get(ctx["market_id"])
                    if m:
                        res = await asyncio.to_thread(
                            bot_tools.execute_trade,
                            ctx["market_id"], ctx["side"], amount, db_user["eth_address"],
                        )
                        await update.message.reply_text(res)
                        return
            except (ValueError, TypeError):
                pass

    # Quick shortcuts: bypass LLM for common actions
    t = user_text.lower().strip()
    if t in ("markets", "m", "trending", "market"):
        markets_text = await asyncio.to_thread(bot_tools.get_polymarket_markets)
        keyboard = _build_markets_keyboard()
        await update.message.reply_text(markets_text, reply_markup=keyboard)
        return
    if t in ("balance", "bal"):
        bal = await asyncio.to_thread(bot_tools.get_polygon_balance, db_user["eth_address"])
        await update.message.reply_text(bal, parse_mode="Markdown")
        return
    if t in ("portfolio", "port"):
        portfolio_text, positions = await asyncio.to_thread(
            bot_tools.get_polymarket_portfolio_with_positions, db_user["eth_address"]
        )
        keyboard = None
        if positions:
            context.user_data["portfolio_positions"] = positions
            buttons = []
            for i, pos in enumerate(positions):
                cid = pos.get("condition_id", "")
                if not cid:
                    continue
                m = market_cache.ensure_market_cached(cid)
                if not m:
                    continue
                buttons.append([
                    InlineKeyboardButton(f"Close #{m.market_id} (full)", callback_data=f"close_pos:{i}")
                ])
            if buttons:
                keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(portfolio_text, parse_mode="Markdown", reply_markup=keyboard)
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

        # If the user has already interacted with markets via buttons/commands,
        # seed the conversation with that ACTIVE market so follow-ups like
        # "buy 10 on this" can be grounded.
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
        
    async def _send_long_message(bot, chat_id: int, text: str, parse_mode: str | None = None):
        """Safely send long texts by splitting to respect Telegram's message length limit."""
        if not text:
            return
        max_len = 4000
        remaining = text
        while remaining:
            if len(remaining) <= max_len:
                chunk = remaining
                remaining = ""
            else:
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

    # Append the user's new message
    chat_sessions[user.id].append({"role": "user", "content": user_text})

    # Truncate to last 12 messages (system + 6 turns) to keep LLM prompt small
    msgs = chat_sessions[user.id]
    if len(msgs) > 13:
        system = next((m for m in msgs if m.get("role") == "system"), None)
        rest = [m for m in msgs if m.get("role") != "system"][-12:]
        chat_sessions[user.id] = ([system] if system else []) + rest

    # Show a typing indicator while we wait for a single-shot response
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    try:
        reply_text = await llm.get_chat_response(chat_sessions[user.id])
        await _send_long_message(
            context.bot,
            update.effective_chat.id,
            reply_text,
            parse_mode="Markdown",
        )
        chat_sessions[user.id].append(
            {"role": "assistant", "content": reply_text}
        )
    except Exception as e:
        logging.error(f"Error calling LLM: {e}")
        await update.message.reply_text("Sorry, I'm having trouble thinking right now.")

async def handle_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text(details)
        return

    # Cache structured market for LLM context
    m = market_cache.get(market_id)
    user_id = query.from_user.id if query.from_user else None
    if user_id is not None:
        active_market_context[user_id] = {
            "market_id": market_id,
            "side": None,
            "market": m,
        }
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

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Trade Yes", callback_data=f"trade:{market_id}:Yes"),
                InlineKeyboardButton("Trade No", callback_data=f"trade:{market_id}:No"),
            ],
        ]
    )
    # Escape special Markdown characters in the details text
    escaped_details = escape_markdown_v2(details)
    await query.edit_message_text(
        escaped_details
        + "\n\n_Tap to trade, or type: trade #"
        + str(market_id)
        + " Yes 10_",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

async def handle_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("trade:"):
        return
    try:
        parts = data.split(":")
        market_id = int(parts[1])
        side = parts[2]
    except (ValueError, IndexError):
        return

    m = market_cache.get(market_id)
    if not m:
        await query.edit_message_text("Market no longer in cache. Run /markets to refresh.")
        return

    user_id = query.from_user.id if query.from_user else None
    if user_id is not None:
        active_market_context[user_id] = {
            "market_id": market_id,
            "side": side,
            "market": m,
        }
        if user_id in chat_sessions:
            yes_odds = getattr(m, "odds", {}).get("Yes", "?")
            no_odds = getattr(m, "odds", {}).get("No", "?")
            chat_sessions[user_id].append(
                {
                    "role": "assistant",
                    "content": (
                        f"User tapped Trade {side} on ACTIVE market "
                        f"#{market_id} — {m.question}. "
                        f"(Yes: {yes_odds}¢, No: {no_odds}¢). "
                        "Awaiting amount in USD for execution."
                    ),
                }
            )

    db_user = db.get_user(update.effective_user.id) if update.effective_user else None
    if not db_user:
        await query.edit_message_text("Please run /start first.")
        return

    amount_buttons = [
        [
            InlineKeyboardButton("$1", callback_data=f"trade_amt:{market_id}:{side}:1"),
            InlineKeyboardButton("$5", callback_data=f"trade_amt:{market_id}:{side}:5"),
        ],
        [
            InlineKeyboardButton("$10", callback_data=f"trade_amt:{market_id}:{side}:10"),
            InlineKeyboardButton("$20", callback_data=f"trade_amt:{market_id}:{side}:20"),
        ],
        [
            InlineKeyboardButton("$50", callback_data=f"trade_amt:{market_id}:{side}:50"),
            InlineKeyboardButton("$100", callback_data=f"trade_amt:{market_id}:{side}:100"),
        ],
    ]
    msg = (
        f"**Trade #{market_id}** — {side}\n\n"
        f"Question: {m.question}\n"
        f"Odds: {m.odds.get(side, '?')}¢\n\n"
        f"Choose amount or type: `trade #{market_id} {side} 10`"
    )
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(amount_buttons))


async def handle_trade_amt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute trade when user taps amount button."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("trade_amt:"):
        return
    try:
        parts = data.split(":")
        market_id = int(parts[1])
        side = parts[2]
        amount = parts[3]
    except (ValueError, IndexError):
        return
    m = market_cache.get(market_id)
    if not m:
        await query.edit_message_text("Market no longer in cache. Run /markets to refresh.")
        return
    db_user = db.get_user(query.from_user.id) if query.from_user else None
    if not db_user:
        await query.edit_message_text("Please run /start first.")
        return
    res = await asyncio.to_thread(
        bot_tools.execute_trade,
        market_id, side, amount, db_user["eth_address"],
    )
    await query.edit_message_text(res)


if __name__ == '__main__':
    if not BOT_TOKEN:
        print("BOT_TOKEN is missing in the environment.")
        exit(1)

    # Initialize DB (SQLite)
    db.init_schema()

    async def post_init(app):
        await app.bot.set_my_commands(BOT_COMMANDS)

    # Build bot
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Formal commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("markets", markets_cmd))
    app.add_handler(CommandHandler("trending", markets_cmd))
    app.add_handler(CommandHandler("category", category_cmd))
    app.add_handler(CommandHandler("swap", swap_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("commands", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^category:[a-z0-9\-]+$"))
    app.add_handler(CallbackQueryHandler(handle_close_pos_callback, pattern=r"^close_pos:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_market_callback, pattern=r"^market:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_trade_callback, pattern=r"^trade:\d+:(Yes|No)$"))
    app.add_handler(CallbackQueryHandler(handle_trade_amt_callback, pattern=r"^trade_amt:\d+:(Yes|No):[\d.]+$"))

    # Catch ALL non-command text messages and pipe them to the LLM
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot is running with Commands + LLM (Polymarket Only)...")
    app.run_polling()
