"""
utils.py — хелперы: курсы ЦБ, safe_send, rate_limit, валюты, DeepSeek, построение сообщений.
"""
import asyncio
import re
import urllib.request
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

_last_request_time: Dict[int, datetime] = {}


def check_rate_limit(user_id: int) -> bool:
    now = datetime.utcnow()
    last = _last_request_time.get(user_id)
    if last and (now - last).total_seconds() < RATE_LIMIT_SECONDS:
        return False
    _last_request_time[user_id] = now
    return True


def now_msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def detect_base_currency(text: str) -> str:
    text_lower = text.lower()
    for synonym, code in CURRENCY_SYNONYMS.items():
        if synonym in text_lower:
            return code
    text_upper = text.upper()
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
    found = {}
    text_lower = text.lower()
    for synonym, code in CURRENCY_SYNONYMS.items():
        if synonym in text_lower:
            found[code] = synonym
    return found


def _parse_num(s: str) -> float:
    s = s.strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def extract_ts_components(text: str) -> Dict[str, float]:
    """Пытается извлечь инвойс, фрахт, страховку из текста пользователя.
    Если ключевых слов нет — первое число >= 1000 считаем инвойсом."""
    res: Dict[str, float] = {}
    text_lower = text.lower()
    text_clean = re.sub(r"\d{8,10}", "", text_lower)

    m = re.search(
        r"(?:инвойс|сумма|стоимость|цена)[^\d]*(\d[\d\s,.]+)(?:\s*(?:ю|юань|юаней|usd|eur|rub|\$|€|¥))?",
        text_clean,
    )
    if m:
        res["invoice"] = _parse_num(m.group(1))

    m = re.search(r"(?:фрахт|доставка|перевозка)[^\d]*(\d[\d\s,.]+)", text_clean)
    if m:
        res["freight"] = _parse_num(m.group(1))

    m = re.search(r"(?:страховка|страхование)[^\d]*(\d[\d\s,.]+)", text_clean)
    if m:
        res["insurance"] = _parse_num(m.group(1))

    # Fallback invoice: если инвойс не найден явно, берём первое число >= 1000
    if "invoice" not in res:
        m = re.search(r"(\d[\d\s,.]{2,})(?:\s*(?:ю|юань|юаней|usd|eur|rub|\$|€|¥))?", text_clean)
        if m:
            val = _parse_num(m.group(1))
            if val >= 1000:
                res["invoice"] = val

    return res


def _detect_currency_near(text: str, pos: int) -> str:
    """Определяет валюту по контексту вокруг позиции числа.
    Приоритет: ВПЕРЁД (прилепленные валюты: 900дол, 2500р), потом ВОКРУГ.
    """
    text_lower = text.lower()
    # Смотрим вперёд (до 10 символов) — приоритет для прилепленных валют
    fwd = text_lower[pos:pos + 10]
    # Смотрим назад (до 10 символов)
    bwd = text_lower[max(0, pos - 10):pos]
    # Объединяем
    combined = bwd + " " + fwd

    # === ПРИОРИТЕТ 1: прилепленные валюты ВПЕРЕДИ числа ===
    # CNY прилепленный (100000ю, 100000юан)
    if re.search(r'^\s*ю(?:аней|ани|ань|ан|а|$|[^а-я])', fwd):
        return "CNY"
    # USD прилепленный (900дол, 900долл)
    if re.search(r'^\s*дол(?:лар|л|$|[^а-я])', fwd):
        return "USD"
    # EUR прилепленный (300евр, 300евро)
    if re.search(r'^\s*евр(?:о|$|[^а-я])', fwd):
        return "EUR"
    # RUB прилепленный (2500р, 2500руб)
    if re.search(r'^\s*руб(?:ль|ли|лей|лях|л|$|[^а-я])', fwd):
        return "RUB"
    if re.search(r'^\s*р(?:$|[^а-я])', fwd):
        return "RUB"

    # === ПРИОРИТЕТ 2: валюты ВОКРУГ (отдельные слова) ===
    # CNY
    if any(x in combined for x in ("юаней", "юани", "юанях", "юанями", "юань", "юаны", "китайск", "rmb", "yuan")):
        return "CNY"
    if "¥" in combined or "cny" in combined:
        return "CNY"

    # USD
    if any(x in combined for x in ("доллар", "доллары", "доллара", "долларов", "greenback", "американск")):
        return "USD"
    if any(x in combined for x in ("бакс", "баксы", "бакса", "баксов")):
        return "USD"
    if "$" in combined or "usd" in combined:
        return "USD"

    # EUR
    if any(x in combined for x in ("евро", "евров", "европейск")):
        return "EUR"
    if "€" in combined or "eur" in combined:
        return "EUR"

    # RUB
    if any(x in combined for x in ("рубль", "рубли", "рублей", "рублях", "рублями", "российск")):
        return "RUB"
    if "₽" in combined or "rub" in combined:
        return "RUB"

    return "RUB"


def _extract_component(text_clean: str, keywords: tuple) -> Optional[Dict[str, any]]:
    """Извлекает число и валюту по ключевым словам.
    Валюта ищется с позиции конца числа (учитывает прилепленные: 100000юаней, 900дол, 2500р).
    """
    pattern = "|".join(keywords)
    m = re.search(rf"(?:{pattern})[^\d]*(\d[\d\s,.]+)", text_clean)
    if m:
        raw_num = m.group(1)
        val = _parse_num(raw_num)
        # Находим длину числовой части в raw_num
        num_len = len(re.match(r"[\d\s,.]+", raw_num).group())
        # Валюта ищется с позиции КОНЦА числа
        cur = _detect_currency_near(text_clean, m.start(1) + num_len)
        return {"value": val, "currency": cur}
    return None


def extract_ts_components_with_currency(text: str) -> Dict[str, Dict[str, any]]:
    """Извлекает компоненты ТС с валютами.
    Возвращает: {"invoice": {"value": 100000.0, "currency": "CNY"}, ...}"""
    res: Dict[str, Dict[str, any]] = {}
    text_lower = text.lower()
    text_clean = re.sub(r"\d{8,10}", "", text_lower)

    # Инвойс — по ключевым словам
    inv = _extract_component(text_clean, ("инвойс", "сумма", "стоимость", "цена"))
    if inv:
        res["invoice"] = inv

    # Фрахт
    fr = _extract_component(text_clean, ("фрахт", "доставка", "перевозка"))
    if fr:
        res["freight"] = fr

    # Страховка
    ins = _extract_component(text_clean, ("страховка", "страхование"))
    if ins:
        res["insurance"] = ins

    # Вес
    weight_m = re.search(r"(\d[\d\s,.]*)(?:\s*(?:кг|kg|килограмм|килограммов|кило|tons?|тн?\.?))", text_clean)
    if weight_m:
        res["weight_kg"] = _parse_num(weight_m.group(1))

    # Fallback invoice — первое число ≥ 1000
    # Если число идёт сразу после кода ТН ВЭД (позиция 0-2) — это инвойс
    # Не пропускаем если фрахт/страховка идёт через другое слово (например, "100000юаней фрахт")
    if "invoice" not in res:
        for m in re.finditer(r"(\d[\d\s,.]{2,})", text_clean):
            val = _parse_num(m.group(1))
            if val < 1000:
                continue
            num_len = len(re.match(r"[\d\s,.]+", m.group(1)).group())
            pos_after = m.start(1) + num_len

            # Пропускаем если перед числом ключевое слово фрахта/страховки
            before = text_clean[max(0, m.start() - 20):m.start()]
            if any(kw in before for kw in ("фрахт", "доставк", "перевозк", "страховк", "страхан")):
                continue

            # Проверяем что после числа (первые 2 токена)
            after = text_clean[pos_after:pos_after + 20]
            after_tokens = after.split()[:2]
            after_str = " ".join(after_tokens)

            # Число в начале строки (после кода ТН ВЭД) = инвойс
            is_at_start = m.start() <= 2

            # Если валютный маркер сразу после числа, а потом фрахт — это инвойс
            has_currency_marker = any(x in after_str for x in ("ю", "дол", "евр", "руб", "р.", "$", "€", "¥"))

            # Пропускаем только если фрахт/страховка идёт СРАЗУ после числа (без валютного маркера)
            if any(kw in after_str for kw in ("фрахт", "доставк", "перевозк", "страховк", "страхан")):
                if not is_at_start and not has_currency_marker:
                    continue

            cur = _detect_currency_near(text_clean, pos_after)
            res["invoice"] = {"value": val, "currency": cur}
            break

    return res


CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


async def get_cbr_rates() -> Dict[str, str]:
    rates = {"CNY": "н/д", "USD": "н/д", "EUR": "н/д", "DATE": ""}
    try:
        def _fetch():
            with urllib.request.urlopen(CBR_URL, timeout=15) as resp:
                return resp.read().decode("windows-1251")
        xml_text = await asyncio.to_thread(_fetch)
        root = ET.fromstring(xml_text)
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


def convert_fee_to_currency(fee_rub: float, currency: str, rates: Dict[str, str]) -> tuple:
    """Конвертирует сбор из рублей в валюту инвойса.
    Возвращает: (fee_in_currency, display_string)
    Пример: convert_fee_to_currency(1231, "CNY", rates) -> (13.45, "1 231 ₽ → 13.45 CNY")
    """
    if fee_rub <= 0:
        return 0.0, "0 ₽"
    if currency == "RUB" or not rates:
        return fee_rub, f"{fee_rub:,.0f} ₽"
    if currency in rates:
        try:
            rate_val = float(rates[currency])
            if rate_val > 0:
                fee_cur = round(fee_rub / rate_val, 2)
                return fee_cur, f"{fee_rub:,.0f} ₽ → {fee_cur:,.2f} {currency}"
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    return fee_rub, f"{fee_rub:,.0f} ₽"


def format_cross_rates(rates: Dict[str, str]) -> str:
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


async def ask_deepseek(messages: List[Dict]) -> str:
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
            temperature=0.2,
            max_tokens=1500,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return f"⚠️ Ошибка при обращении к AI: {e}"


def build_messages(user_id: int, user_text: str, extra_context: str = "", include_history: bool = True) -> List[Dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if extra_context:
        msgs.append({"role": "system", "content": extra_context})
    if include_history:
        history = get_dialog_history(user_id, limit=MAX_HISTORY)
        for h in history:
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs


def parse_date_range(text: str) -> tuple:
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
    dates = re.findall(r"(\d{2})[.](\d{2})[.](\d{4})", text)
    if len(dates) >= 2:
        d1 = f"{dates[0][2]}-{dates[0][1]}-{dates[0][0]}"
        d2 = f"{dates[1][2]}-{dates[1][1]}-{dates[1][0]}"
        return d1, d2
    return None, None


async def safe_send(message: Message, text: str, chunk: int = 4000) -> None:
    try:
        if len(text) <= chunk:
            await message.answer(text)
            return
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
        for part in parts:
            await message.answer(part)
            await asyncio.sleep(0.3)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "parse" in err or "tag" in err or "entity" in err:
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
