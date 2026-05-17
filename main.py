#!/usr/bin/env python3
"""
main.py — точка входа.
Запуск: python main.py

bothost: все .py файлы должны быть в одной папке.
"""
import sys
import os
import asyncio

# bothost: гарантируем что текущая папка в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger, VERSION
from database import init_db, migrate_add_full_name
from bot_instance import dp, bot
from tnved_engine import restore_tnved_from_db

# Импорт handlers регистрирует все @dp.message декораторы
import handlers  # noqa: F401


async def main() -> None:
    logger.info(f"Bot starting. Version: {VERSION}")
    init_db()
    logger.info("Database initialized.")
    try:
        migrate_add_full_name()
        logger.info("Migration completed.")
    except Exception as e:
        logger.warning(f"Migration skipped: {e}")
    restore_tnved_from_db()
    logger.info("TNVED cache restored from DB (if exists).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
