"""
main.py — точка входа.
БЕЗ tnved_fetcher. Только Excel. Одна база.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot_instance import dp, bot
from config import VERSION, DB_PATH, logger
from database import init_db, restore_tnved_from_db


async def main():
    logger.info(f"Bot starting. Version: {VERSION}")
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    init_db()
    logger.info("Database initialized.")
    restore_tnved_from_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
