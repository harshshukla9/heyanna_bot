import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

import database
import wallets
import bot_tools
import llm

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# In-memory session context for conversation history (User ID -> List of Dict messages)
chat_sessions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /start command to initialize a user."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    
    if not db_user:
        await update.message.reply_text("Welcome! Generating your Ethereum/Polygon wallet for Polymarket. Please wait a moment...")
        eth_wallet = wallets.generate_eth_wallet()
        
        # We store empty strings for Solana since the user requested to disable it for now.
        database.create_user(
            user_id=user.id,
            username=user.username or "",
            eth_data=eth_wallet,
            sol_data=("", "")
        )
        db_user = database.get_user(user.id)
        
        welcome_msg = (
            f"Hello {user.first_name}! I am Anna, your Polymarket trading assistant.\n\n"
            f"I have automatically generated an EVM wallet for you (Polygon network):\n"
            f"`{db_user['eth_address']}`\n\n"
            f"Use /portfolio to see your funds, /markets to browse, or just chat with me normally!"
        )
        await update.message.reply_text(welcome_msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Welcome back {user.first_name}! Your Polygon wallet is `{db_user['eth_address']}`.\n\nUse /commands or just chat with me!", parse_mode="Markdown")

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /wallet command."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    if db_user:
        await update.message.reply_text(f"Your EVM/Polygon Wallet:\n`{db_user['eth_address']}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("Please run /start first.")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /balance command."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    if db_user:
        await update.message.reply_text("Fetching your live Polygon balance...")
        bal = bot_tools.get_polygon_balance(db_user['eth_address'])
        await update.message.reply_text(f"Balance: {bal}")
    else:
        await update.message.reply_text("Please run /start first.")

async def markets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /markets command."""
    await update.message.reply_text("Fetching trending Polymarkets...")
    markets = bot_tools.get_polymarket_markets()
    await update.message.reply_text(markets)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /help command to list all commands."""
    help_text = (
        "Here are the available commands:\n"
        "/start - Initialize your account\n"
        "/wallet - Show your Polygon wallet address\n"
        "/balance - Check MATIC/POL & USDC balances\n"
        "/portfolio - View your funds + open positions & PnL\n"
        "/markets - Browse trending Polymarket events\n"
        "/trending - Alias for /markets\n"
        "/swap - Swap USDC.e to bridged USDC for trading\n"
        "/approve - Run 6x approval flow for Polymarket\n"
        "/help - Show this help message\n\n"
        "You can also just chat with me normally!"
    )
    await update.message.reply_text(help_text)

async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /portfolio command."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    if db_user:
        await update.message.reply_text("Fetching your Polymarket portfolio...")
        portfolio = bot_tools.get_polymarket_portfolio(db_user['eth_address'])
        await update.message.reply_text(portfolio, parse_mode="Markdown")
    else:
        await update.message.reply_text("Please run /start first.")

async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /swap command."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    if db_user:
        await update.message.reply_text("Initiating USDC.e → bridged USDC swap...")
        res = bot_tools.swap_usdc_for_trading(db_user['eth_address'])
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("Please run /start first.")

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Formal /approve command."""
    user = update.effective_user
    db_user = database.get_user(user.id)
    if db_user:
        await update.message.reply_text("Setting up Polymarket approvals (6 transactions)...")
        res = bot_tools.approve_usdc_for_trading(db_user['eth_address'])
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("Please run /start first.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers any normal text message using the LLM and the user's wallet context."""
    import time
    user = update.effective_user
    user_text = update.message.text
    
    db_user = database.get_user(user.id)
    if not db_user:
        await update.message.reply_text("Please run /start first to generate your wallet.")
        return

    # Initialize chat history for the user
    if user.id not in chat_sessions:
        system_prompt = f"""
        You are Anna, an AI prediction market and trading assistant strictly focused on Polymarket. 
        Your user is {user.first_name}. 
        Here is the user's wallet information:
        - EVM (Polygon) Address: {db_user['eth_address']}
        
        You can help the user check their wallet balances on Polygon, browse trending prediction markets on Polymarket, make mock trades, or just chat. 
        
        CRITICAL RULES FOR BALANCES & MARKETS:
        1. If the user asks for their balance, use the Polygon balance tool on their EVM address.
        2. If the user asks to "suggest me some markets" generically, use get_polymarket_markets to fetch trending active markets.
        3. If the user asks for a SPECIFIC topic (e.g. "AI", "politics", "GPT-4"), use search_polymarket_events with that keyword to find matching markets.
        4. ALWAYS start your market response with the phrase "Here are some active markets:" and list them nicely.

        CRITICAL TRADING RULES:
        1. NEVER execute a trade immediately when the user requests it. 
        2. First, ask the user for explicit confirmation (e.g. "Do you want to confirm this trade of X on Y?").
        3. ONLY use the execute_trade tool AFTER the user explicitly replies "yes" or "confirm" to your prompt.
        4. When you call execute_trade, pass the required EVM wallet address for Polymarket.

        CRITICAL TOOL EXECUTION RULES:
        - NEVER say "let me call the tool" or "let me fetch" or "let me pull". Just DO IT silently by calling the tool.
        - NEVER narrate or describe what tool you are about to use. Simply use it.
        - NEVER pretend to have data you don't have. If you need market odds or news, CALL the tool first.
        - When you need information, call the appropriate tool function immediately without announcing it.
        
        SUPERFORECASTER IDENTITY:
        When a user asks you to predict the likelihood of an event or assess a market, act as a 'Superforecaster' and a top Polymarket trader. 
        Use the following systematic process:
        1. Context & Odds: ALWAYS use the search_news tool to gather real-time context on the topic. ALWAYS use the search_polymarket_events tool to find the specific market and check the exact current probability/odds shown on Polymarket.
        2. Break Down the Question: Decompose it into manageable parts.
        3. Explain Context & Current Odds: Explicitly summarize the recent news context you found that informs your forecast. Clearly state the exact current Polymarket probability you retrieved.
        4. Consider Base Rates & Historical Precedents.
        5. Think Probabilistically: Express your prediction in terms of percentages/probabilities. Assign your own independent likelihood estimation.
        6. Market Analysis: Compare your estimated probability against the actual current market odds (e.g., 30c for Yes). Explicitly state whether the market is overvalued or undervalued, and confidently recommend a side (Buy Yes / Buy No).
        """
        chat_sessions[user.id] = [{"role": "system", "content": system_prompt}]
        
    # Append the user's new message
    chat_sessions[user.id].append({"role": "user", "content": user_text})

    # Show a typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # Send a placeholder message to edit (so user feels immediate feedback)
    message = await update.message.reply_text("🤔 Thinking...")

    # Get response from LLM via streaming
    try:
        current_text = ""
        last_edit_time = time.time()
        
        async for chunk in llm.get_chat_response_stream(chat_sessions[user.id]):
            if chunk["type"] == "content":
                current_text += chunk["data"]
                
                # Edit message roughly every 1.5 seconds to avoid Telegram rate limits
                now = time.time()
                if now - last_edit_time > 1.5 and current_text.strip():
                    try:
                        # Re-send the chat action since it usually expires after 5 secs
                        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
                        await message.edit_text(current_text + " ▌")
                    except Exception:
                        pass # Ignore intermittent edit errors like MessageNotModified
                    last_edit_time = now

        # Final edit
        if current_text.strip():
            # Only use Markdown on the finalized text to prevent formatting breakages mid-stream
            try:
                await message.edit_text(current_text, parse_mode="Markdown")
            except Exception:
                # Fallback to plain text if markdown formatting breaks
                await message.edit_text(current_text)
            chat_sessions[user.id].append({"role": "assistant", "content": current_text})
        else:
            await message.edit_text("Sorry, I had nothing to say.")
            
    except Exception as e:
        logging.error(f"Error streaming LLM: {e}")
        await message.edit_text("Sorry, I'm having trouble thinking right now.")

if __name__ == '__main__':
    if not BOT_TOKEN:
        print("BOT_TOKEN is missing in the environment.")
        exit(1)

    # Initialize DB
    database.init_db()

    # Build bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Formal commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("markets", markets_cmd))
    app.add_handler(CommandHandler("trending", markets_cmd))
    app.add_handler(CommandHandler("swap", swap_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Catch ALL non-command text messages and pipe them to the LLM
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot is running with Commands + LLM (Polymarket Only)...")
    app.run_polling()
