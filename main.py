import asyncio
import logging
import os
import threading
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from api_app import create_api_app
from bot_app import create_telegram_application
from database_manager import DatabaseManager


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def start_telegram_bot(db: DatabaseManager, bot_token: str) -> None:
    application = create_telegram_application(db=db, bot_token=bot_token)
    # This blocks in this thread until the bot is stopped.
    # Disable signal handlers because we're not in the main thread.
    application.run_polling(stop_signals=None)


def start_signal_listener(db: DatabaseManager) -> None:
    """
    Start the Telegram signal listener as a self-contained pipeline.

    This creates a pipeline:
    1. Listener watches @Metazen_pulse_5M_15M_Btc channel for signals
    2. Parses signals and writes to logs/announcement_signals.jsonl
    3. Autotrader processes signals and executes trades via API

    Runs in a separate thread to avoid blocking.
    """
    import traceback
    from scripts.telegram_announcement_listener import _load_config_from_env, run_listener
    from autotrader_manager import AutoTraderManager

    try:
        cfg = _load_config_from_env()
    except ValueError as e:
        logger.warning(f"Signal listener not configured: {e}")
        return

    # Ensure logs directory exists
    cfg.output_path.parent.mkdir(parents=True, exist_ok=True)

    # Get user ID for trading
    user_id = int(os.getenv("USER_ID", "1"))
    trade_amount = float(os.getenv("AUTO_TRADE_AMOUNT_USD", "3.0"))

    logger.info(f"Starting signal listener for channel {cfg.chat_ref}")
    logger.info(f"Trading: user_id={user_id}, amount=${trade_amount}")

    # Create autotrader instance for trade execution
    autotrader = AutoTraderManager(
        db=db,
        user_id=user_id,
        trade_amount_usd=trade_amount,
        dry_run=False,
        send_notification=True,
    )

    async def on_trade(payload: dict) -> str:
        """Callback to execute trade when signal detected."""
        try:
            result = autotrader.process_signal(payload)
            return result
        except Exception as e:
            logger.error(f"Trade execution failed: {e}")
            return f"error: {str(e)}"

    async def run():
        """Run the listener with trade execution callback."""
        await run_listener(cfg, on_trade=on_trade)

    # Run the async listener in a dedicated event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run())
    except Exception as e:
        logger.error(f"Signal listener error: {e}")
        logger.error(traceback.format_exc())


def main() -> None:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing in the environment.")

    # Shared DB manager for both bot and API
    db = DatabaseManager()
    db.init_schema()

    # Build FastAPI app
    api_app = create_api_app(db=db)

    # Start Telegram bot in a background thread
    bot_thread = threading.Thread(
        target=start_telegram_bot, args=(db, bot_token), daemon=True
    )
    bot_thread.start()

    # Start signal listener in a background thread (if enabled).
    # Important: api_app can also start its own Telegram listener daemon.
    # Avoid running both at once with the same Telethon session.
    enable_listener = os.getenv("ENABLE_SIGNAL_LISTENER", "1") == "1"
    api_listener_enabled = (
        os.getenv("ENABLE_TELEGRAM_SIGNAL_LISTENER", "").strip().lower() in ("1", "true", "yes")
        or bool(
            os.getenv("TELEGRAM_SIGNAL_CHAT")
            and os.getenv("TELEGRAM_API_ID")
            and os.getenv("TELEGRAM_API_HASH")
        )
    )
    if enable_listener and not api_listener_enabled:
        listener_thread = threading.Thread(
            target=start_signal_listener, args=(db,), daemon=True
        )
        listener_thread.start()
        logger.info("Signal listener started in background")
    elif enable_listener and api_listener_enabled:
        logger.info("Skipping main-thread signal listener because API daemon listener is enabled")

    # Run FastAPI (Uvicorn) in the main thread
    uvicorn.run(
        api_app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
