"""
bot_instance.py — aiogram Dispatcher и Bot.
Импортирует только config (токен). Все остальные модули импортируют dp/bot отсюда.
aiogram 3.7+: default=DefaultBotProperties() вместо parse_mode=...
"""
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from config import BOT_TOKEN, logger

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан! Бот не запустится.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

async def send_admin_notification(text: str) -> None:
    """Отправляет уведомление АБ, если ADMIN_ID задан."""
    from config import ADMIN_ID
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Не удалось уведомить АБ: {e}")
