import logging
import os
import threading

import uvicorn
from dotenv import load_dotenv

from api_app import create_api_app
from bot_app import create_telegram_application
from database_manager import DatabaseManager


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def start_telegram_bot(db: DatabaseManager, bot_token: str) -> None:
    application = create_telegram_application(db=db, bot_token=bot_token)
    # This blocks in this thread until the bot is stopped.
    # Disable signal handlers because we're not in the main thread.
    application.run_polling(stop_signals=None)


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

    # Run FastAPI (Uvicorn) in the main thread
    uvicorn.run(
        api_app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()

