"""
utils.py — хелперы: курсы ЦБ, safe_send, rate_limit, валюты, DeepSeek, построение сообщений.
Использует requests (уже на bothost) вместо httpx. openai — отложенный импорт.
"""
import asyncio
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

from config import (
    DEEPSEEK_API_KEY, CURRENCY_SYNONYMS, MAX_HISTORY,
    RATE_LIMIT_SECONDS, SYSTEM_PROMPT, logger,
)
from database import get_dialog_history

# ------------------------------------------------------------------
# Rate limit
# ------------------------------------------------------------------
_last_request_time: Dict[int, datetime] = {}


def check_rate_limit(user_id: int) -> bool:
    now = datetime.utcnow()
    last = _last_request_time.get(user_id)
    if last and (now - last).total_seconds() < RATE_LIMIT_SECONDS:
        return False
    _last_request_time[user_id] = now
    return True


# ------------------------------------------------------------------
# Московское время
# ------------------------------------------------------------------

def now_msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


# ------------------------------------------------------------------
# Определение базовой валюты
# ------------------------------------------------------------------

def detect_base_currency(text: str) -> str:
    text_lower = text.lower()
    for synonym, code in CURRENCY_SYNONYMS.items():
        if synonym in text_lower:
            return code
    if re.search(r"\b(CNY|USD|EUR|RUB)\b", text_upper := text.upper()):
        for c in ("CNY", "USD", "EUR", "RUB"):
            if c in text_upper:
                return c
    if "юан" in text_lower or "китайск" in text_lower or "rmb" in text_lower:
        return "CNY"
    if "доллар" in text_lower or "бакс" in text_lower or "$" in text:
        return "USD"
    if "евро" in text_lower or "€" in text:
        return "EUR"
    return "RUB"


def extract_currencies(text: str) -> Dict[str, str]:
    """Извлекает все упомянутые валюты из текста."""
    found = {}
    text_lower = text.lower()
    for synonym, code in CURRENCY_SYNONYMS.items():
        if synonym in text_lower:
            found[code] = synonym
    return found


# ------------------------------------------------------------------
# Курсы ЦБ РФ
# ------------------------------------------------------------------

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


async def get_cbr_rates() -> Dict[str, str]:
    """Получает курсы ЦБ РФ: CNY, USD, EUR. requests через to_thread (bothost нет httpx)."""
    rates = {"CNY": "н/д", "USD": "н/д", "EUR": "н/д", "DATE": ""}
    try:
        # bothost: requests есть, httpx нет → запускаем синхронный requests в треде
        resp = await asyncio.to_thread(requests.get, CBR_URL, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        date_attr = root.get("Date", "")
        rates["DATE"] = date_attr
        for valute in root.findall("Valute"):
            char_code = valute.findtext("CharCode", "")
            value = valute.findtext("Value", "").replace(",", ".")
            nominal = int(valute.findtext("Nominal", "1"))
            if char_code in ("CNY", "USD", "EUR"):
                try:
                    rates[char_code] = str(round(float(value) / nominal, 4))
                except ValueError:
                    pass
    except Exception as e:
        logger.error(f"Ошибка получения курсов ЦБ: {e}")
    return rates


def format_cross_rates(rates: Dict[str, str]) -> str:
    """Форматирует кросс-курсы для отображения."""
    parts = []
    try:
        cny = float(rates.get("CNY", 0))
        usd = float(rates.get("USD", 0))
        eur = float(rates.get("EUR", 0))
        if cny and usd:
            parts.append(f"CNY/USD={round(cny/usd, 4)}")
        if cny and eur:
            parts.append(f"CNY/EUR={round(cny/eur, 4)}")
        if usd and eur:
            parts.append(f"USD/EUR={round(usd/eur, 4)}")
    except (ValueError, ZeroDivisionError):
        pass
    return ", ".join(parts) if parts else "н/д"


# ------------------------------------------------------------------
# DeepSeek API
# ------------------------------------------------------------------

async def ask_deepseek(messages: List[Dict]) -> str:
    """Отправляет запрос к DeepSeek API. Отложенный импорт openai на случай если не установлен."""
    try:
        import openai
    except ImportError:
        return "⚠️ Модуль openai не установлен. Установите: pip install openai"
    client = openai.AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return f"⚠️ Ошибка при обращении к AI: {e}"


def build_messages(user_id: int, user_text: str, extra_context: str = "") -> List[Dict]:
    """Строит список сообщений для DeepSeek: системный промпт + история + текущий запрос."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if extra_context:
        msgs.append({"role": "system", "content": extra_context})
    history = get_dialog_history(user_id, limit=MAX_HISTORY)
    for h in history:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs


# ------------------------------------------------------------------
# Парсинг дат из текста (для экспорта логов)
# ------------------------------------------------------------------

def parse_date_range(text: str) -> tuple:
    """Извлекает даты из текста для фильтрации логов."""
    now = datetime.utcnow()
    text_lower = text.lower()

    if "сегодня" in text_lower:
        today = now.strftime("%Y-%m-%d")
        return today, today
    if "вчера" in text_lower:
        yest = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return yest, yest
    if "неделю" in text_lower or "за неделю" in text_lower:
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        return start, now.strftime("%Y-%m-%d")

    # Формат: с 01.05.2026 по 05.05.2026
    dates = re.findall(r"(\d{2})[.](\d{2})[.](\d{4})", text)
    if len(dates) >= 2:
        d1 = f"{dates[0][2]}-{dates[0][1]}-{dates[0][0]}"
        d2 = f"{dates[1][2]}-{dates[1][1]}-{dates[1][0]}"
        return d1, d2

    return None, None


# ------------------------------------------------------------------
# safe_send
# ------------------------------------------------------------------

async def safe_send(message: Message, text: str, chunk: int = 4000) -> None:
    """Отправляет текст частями. При ошибке HTML — fallback в plain text."""
    try:
        if len(text) <= chunk:
            await message.answer(text)
            return
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
        for part in parts:
            await message.answer(part)
            await asyncio.sleep(0.3)
    except TelegramBadRequest as e:
        if "parse" in str(e).lower() or "tag" in str(e).lower() or "entity" in str(e).lower():
            plain = text.replace("<b>", "").replace("</b>", "")
            plain = plain.replace("<i>", "").replace("</i>", "")
            plain = plain.replace("<code>", "").replace("</code>", "")
            plain = plain.replace("<pre>", "").replace("</pre>", "")
            plain = plain.replace("<a href=", "[").replace("</a>", "]")
            if len(plain) <= chunk:
                await message.answer(plain, parse_mode=None)
                return
            for part in [plain[i:i + chunk] for i in range(0, len(plain), chunk)]:
                await message.answer(part, parse_mode=None)
                await asyncio.sleep(0.3)
        else:
            raise
