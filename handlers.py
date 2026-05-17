"""
handlers.py — все хэндлеры aiogram.
БЕЗ tnved_fetcher. Только Excel. Одна база.
"""
import asyncio
import re
from datetime import datetime, timedelta
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import Message

from bot_instance import dp, bot
from config import ADMIN_ID, LEARN_MODE, PENDING_CODE_UPDATE, RADIO_ELECTRONICS_CODES_SET, logger
from database import (
    save_message, clear_history, save_correction, save_custom_codes,
    get_knowledge, save_knowledge, get_dialogs_for_export, create_logs_xlsx,
)
from parsers import parse_xlsx, parse_docx, parse_txt, _extract_codes_from_rows
import tnved_engine
from tnved_engine import load_tnved_rows, is_radio_electronics, extract_tnved_codes
from utils import (
    check_rate_limit, now_msk, detect_base_currency, get_cbr_rates,
    format_cross_rates, build_messages, ask_deepseek, safe_send, parse_date_range,
)


# ------------------------------------------------------------------
# Команды
# ------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "<b>West Asia AI Helper</b> — помощник для менеджеров по ВЭД и логистике.\n\n"
        "Просто напиши вопрос — помогу с расчётами, сроками, маршрутами.\n\n"
        "Если ответ неправильный — напиши «несогласен» или «неверно» на моё сообщение."
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Справка:</b>\n"
        "• Отправь текст с кодом ТН ВЭД — получишь расчёт.\n"
        "• Отправь .xlsx файл — извлеку данные.\n"
        "• Напиши «несогласен» — запишешь замечание.\n"
        "• /clear — очистить историю.\n"
        "• /help — эта справка.\n\n"
        "<i>Обновляйте Excel с кодами раз в месяц через /updatecodes</i>"
    )

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    clear_history(message.from_user.id)
    await message.answer("🗑 История очищена.")

@dp.message(Command("brief"))
async def cmd_brief(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        "<b>BRIEF</b>\n"
        "НДС: 22% базовая, 10% льготная.\n"
        "Сбор: шкала ПП РФ №1637. Радио: 73 860 ₽.\n"
        "Валюта: инвойс. Страховка: в ТС.\n"
        "<i>Обновляйте коды ТН ВЭД раз в месяц.</i>"
    )

@dp.message(Command("topics"))
async def cmd_topics(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    topics = get_knowledge()
    if not topics:
        await message.answer("📭 Пусто.")
        return
    lines = [f"{i+1}. {t['topic']}" for i, t in enumerate(topics)]
    await message.answer("<b>Темы:</b>\n" + "\n".join(lines))

@dp.message(Command("learn"))
async def cmd_learn(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    topic = message.text.replace("/learn", "").strip()
    if not topic:
        await message.answer("Использование: /learn <тема>")
        return
    LEARN_MODE[message.from_user.id] = {"topic": topic, "content": "", "questions": [], "waiting_for": "content"}
    await message.answer(f"📚 Режим обучения: {topic}\nПришли текст или файл. /done — выйти.")

@dp.message(Command("done"))
async def cmd_done(message: Message):
    uid = message.from_user.id
    if uid not in LEARN_MODE:
        await message.answer("Ты не в режиме обучения.")
        return
    mode = LEARN_MODE.pop(uid)
    if mode["content"]:
        save_knowledge(mode["topic"], mode["content"], "", message.from_user.username or str(uid))
        await message.answer(f"✅ «{mode['topic']}» сохранено.")
    else:
        await message.answer("❌ Нет контента.")

@dp.message(Command("updatecodes"))
async def cmd_updatecodes(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    PENDING_CODE_UPDATE[message.from_user.id] = now_msk()
    await message.answer(
        "📥 <b>Режим обновления кодов</b>\n\n"
        "Пришли .xlsx файл с перечнем ТН ВЭД.\n"
        "Я извлеку коды и обновлю базу.\n"
        "<i>Рекомендуется обновлять раз в месяц.</i>\n\n"
        "Ожидание: 10 мин."
    )


# ------------------------------------------------------------------
# Документы
# ------------------------------------------------------------------

@dp.message(F.document)
async def handle_document(message: Message):
    doc = message.document
    user_id = message.from_user.id
    file_name = (doc.file_name or "").lower()

    # Режим обучения
    if user_id in LEARN_MODE and LEARN_MODE[user_id].get("waiting_for") == "content":
        if not any(file_name.endswith(ext) for ext in [".txt", ".docx", ".xlsx"]):
            await message.answer("Только .txt, .docx, .xlsx")
            return
        try:
            file = await bot.get_file(doc.file_id)
            bytes_io = await bot.download_file(file.file_path)
            if file_name.endswith(".txt"):
                text = parse_txt(bytes_io)
            elif file_name.endswith(".docx"):
                text = parse_docx(bytes_io)
            else:
                rows = parse_xlsx(bytes_io)
                text = "\n".join(" | ".join(str(c) for c in row) for row in rows)
            if not text.strip():
                await message.answer("Файл пустой.")
                return
            LEARN_MODE[user_id]["content"] = text
            LEARN_MODE[user_id]["waiting_for"] = "questions"
            await message.answer("✅ Сохранено.")
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await message.answer(f"Ошибка: {e}")
        return

    if not file_name.endswith(".xlsx"):
        await message.answer("Только .xlsx")
        return

    now = now_msk()
    is_code_update = False
    if user_id in PENDING_CODE_UPDATE:
        if (now - PENDING_CODE_UPDATE[user_id]) < timedelta(minutes=10):
            is_code_update = True
        del PENDING_CODE_UPDATE[user_id]

    try:
        file = await bot.get_file(doc.file_id)
        bytes_io = await bot.download_file(file.file_path)
        data = parse_xlsx(bytes_io)
        if not data:
            await message.answer("Не прочитал.")
            return

        has_tnved = any(isinstance(r[0], str) and re.match(r"\d{10}", r[0].replace(" ", "")) for r in data if r)
        if has_tnved:
            load_tnved_rows(data)
            await message.answer(f"📋 Загружено: {len(tnved_engine._TNVED_ROWS_CACHE)} кодов")

        if is_code_update:
            codes = _extract_codes_from_rows(data)
            if not codes:
                await message.answer("❌ Коды не найдены.")
                return
            save_custom_codes(codes)
            await message.answer(f"✅ {len(codes)} кодов. Примеры: {', '.join(codes[:5])}")
            return

        lines = ["<b>Excel:</b>"]
        for i, row in enumerate([r for r in data if r and any(str(c).strip() for c in r)][:15], 1):
            lines.append(f"{i}. {' | '.join(str(c)[:40] for c in row[:4])}")
        await safe_send(message, "\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"Ошибка: {e}")


# ------------------------------------------------------------------
# Таблица платежей
# ------------------------------------------------------------------

def _format_payments_box(answer: str, currency: str, rates: dict = None) -> str:
    """Только Пошлина/НДС/Сбор/ИТОГО в рамке. Без ТС."""
    import re
    lines = answer.split("\n")
    data: dict = {}
    cur_pat = {"USD": r"USD|\\$", "EUR": r"EUR|€", "CNY": r"CNY|CNH|¥", "RUB": r"RUB|₽"}
    cur_re = cur_pat.get(currency, re.escape(currency))
    KEYWORDS = {
        "пошлина": ("пошлин", "таможенная пошлина"),
        "ндс": ("ндс", "налог на добавленную"),
        "сбор": ("сбор", "таможенный сбор"),
        "итого": ("итого", "всего платеж"),
    }
    for line in lines:
        ls = line.strip().lower()
        if len(ls) < 3 or not re.search(r"\d", ls):
            continue
        for key, keywords in KEYWORDS.items():
            if key in data:
                continue
            if any(kw in ls for kw in keywords):
                m = re.search(r"([\d\s,.]+)\s*(?:" + cur_re + r")", line, re.IGNORECASE)
                if m:
                    val = m.group(1).strip().replace(" ", "")
                    if val:
                        data[key] = val
                break
    if "пошлина" not in data and "ндс" not in data:
        return ""
    lw, vw = 10, 14
    top = f"┌{'─'*(lw+2)}┬{'─'*(vw+2)}┐"
    hdr = f"│ {'Платеж':<{lw}} │ {currency:>{vw}} │"
    sep = f"├{'─'*(lw+2)}┼{'─'*(vw+2)}┤"
    bot = f"└{'─'*(lw+2)}┴{'─'*(vw+2)}┘"
    rows = []
    for key in ("пошлина", "ндс", "сбор"):
        if key in data:
            lbl = {"пошлина": "Пошлина", "ндс": "НДС", "сбор": "Сбор"}[key]
            rows.append(f"│ {lbl:<{lw}} │ {data[key]:>{vw}} │")
    body = "\n".join(rows)
    tot = f"\n{sep}\n│ {'ИТОГО':<{lw}} │ {data['итого']:>{vw}} │" if "итого" in data else ""
    return f"\n\n📊 <b>Платежи</b>\n<code>{top}\n{hdr}\n{sep}\n{body}{tot}\n{bot}</code>"


# ------------------------------------------------------------------
# Текстовые сообщения
# ------------------------------------------------------------------

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    user_text = message.text or ""
    if not user_text or user_text.startswith("/"):
        return

    # Логи (только АБ)
    if user_id == ADMIN_ID and any(k in user_text.lower() for k in ["логи", "выгрузи", "экспорт"]):
        df, dt = parse_date_range(user_text)
        logs = get_dialogs_for_export(df, dt)
        if not logs:
            await message.answer("📭 Пусто.")
            return
        xb = create_logs_xlsx(logs, "logs")
        fn = f"logs_{df or 'all'}_{dt or 'all'}.xlsx"
        await message.answer_document(document=types.BufferedInputFile(xb, filename=fn), caption=f"📊 {len(logs)} записей")
        return

    # Несогласен
    if any(k in user_text.lower() for k in ["несогласен", "не согласен", "неправильно", "неверно"]):
        orig = message.reply_to_message.text if message.reply_to_message else ""
        save_correction(user_id, message.from_user.username or "", orig[:500], user_text[:500])
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ @{message.from_user.username or user_id}: {user_text[:200]}")
            except Exception as e:
                logger.error(f"Не удалось уведомить: {e}")
        await message.answer("⚠️ Замечание записано.")
        return

    if user_id in LEARN_MODE:
        await message.answer("✅ Записано.")
        return

    if not check_rate_limit(user_id):
        return

    logger.info(f"User {user_id}: {user_text[:80]}...")
    save_message(user_id, message.from_user.username or "", "user", user_text)

    codes = extract_tnved_codes(user_text)
    radio_detected = any(is_radio_electronics(c) for c in codes)
    from database import get_tnved_from_db as _gtd

    calc_words = ("инвойс", "сумма", "стоимость", "расчёт", "платеж", "пошлина",
                  "ндс", "сбор", "таможенная", "тс", "фрахт", "страховк",
                  "посчитай", "сколько будет", "узнать плат", "сколько плат")
    is_calc = any(w in user_text.lower() for w in calc_words)

    found_codes = []
    missing = []
    if codes:
        for c in codes[:3]:
            info = _gtd(c)
            if info:
                found_codes.append(info)
            else:
                missing.append(c)

    # === БЫСТРЫЙ ОТВЕТ: только код ===
    if codes and found_codes and not is_calc:
        info = found_codes[0]
        pt = info["parsed_tariff"]
        duty_type = "адвалорная" if pt.get("type") == "percent" else (f"комбинированная ({pt.get('formula', '')})" if pt.get("type") in ("min", "plus") else info['tariff'])
        vat = "10%" if any(w in info['name'].lower() for w in ("пищев", "детск", "медиц", "книг", "печат")) else "22%"
        radio = "\n⚡ Сбор 73 860 ₽" if any(is_radio_electronics(c) for c in codes) else ""
        await message.answer(
            f"📋 <code>{info['code']}</code>\n"
            f"🔧 {info['name']}\n"
            f"💰 Пошлина: {info['tariff']} — {duty_type}\n"
            f"🧾 НДС: {vat}"
            f"{radio}"
        )
        return

    if codes and not found_codes:
        await safe_send(message, f"❌ Код не найден: <code>{', '.join(missing)}</code>")
        return

    base_cur = detect_base_currency(user_text)
    has_ins = any(w in user_text.lower() for w in ("страховка", "страхование"))

    rates = None
    try:
        rates = await get_cbr_rates()
        cr = format_cross_rates(rates)
        extra = (
            f"[КУРСЫ ЦБ РФ {rates.get('DATE','')}] CNY={rates.get('CNY','')}₽ USD={rates.get('USD','')}₽ EUR={rates.get('EUR','')}₽. "
            f"Кросс: {cr}. Валюта: {base_cur}. НДС: 22%/10%. "
        )
        extra += (
            "ТС (п.1 ст.40 ТК ЕАЭС): Инвойс + Фрахт + Страховка + Упаковка + Прочее. "
            "Не указано → 0. Всё в валюте инвойса. "
            "Конвертация: чужая валюта → ₽ ЦБ → валюта инвойса. "
        )
        if has_ins:
            extra += "Страховка — в ТС. "
        extra += "НЕ придумывай ставки."
    except Exception as e:
        logger.error(f"Курсы: {e}")
        extra = "[КУРСЫ ЦБ недоступны]. НДС: 22%/10%."

    msgs = build_messages(user_id, user_text, extra_context=extra)
    answer = await ask_deepseek(msgs)

    # Шапка
    header = ""
    if found_codes:
        info = found_codes[0]
        pt = info["parsed_tariff"]
        header = f"📋 <b>Код:</b> <code>{info['code']}</code>\n"
        header += f"🔧 {info['name']}\n"
        header += f"💰 <b>Пошлина:</b> {info['tariff']}"
        if pt.get("type") in ("min", "plus", "fixed_eur"):
            header += f" — комбинированная ({pt['formula']})"
        elif pt.get("type") == "percent":
            header += " — адвалорная"
        header += "\n"
        vat = "10% (льготная)" if any(w in info['name'].lower() for w in ("пищев", "детск", "медиц", "книг", "печат")) else "22% (базовая)"
        header += f"🧾 <b>НДС:</b> {vat}\n"
        if any(is_radio_electronics(c) for c in codes):
            header += "⚡ <b>Радиоэлектроника:</b> сбор 73 860 ₽\n"
        if missing:
            header += f"⚠️ Не найдены: {', '.join(missing)}\n"

    # Таблица платежей
    if is_calc and base_cur != "RUB":
        box = _format_payments_box(answer, base_cur, rates)
        if box:
            answer += box

    # Курс ЦБ
    if rates and base_cur != "RUB":
        rate = rates.get(base_cur, "")
        if rate and rate != "н/д":
            answer += f"\n\nℹ️ <i>Курс ЦБ РФ на {rates.get('DATE','сегодня')}: 1 {base_cur} = {rate} ₽</i>"

    # НДС fallback
    if not any(k in answer.lower() for k in ("ндс", "налог на добавленную")):
        if any(w in user_text.lower() for w in ("расчёт", "пошлина", "сбор", "ндс")):
            answer += "\n\n<i>НДС: базовая 22% с 01.01.2026, льготная 10%.</i>"

    if header:
        answer = header + "\n" + answer

    if radio_detected and "⚡" not in answer and "73860" not in answer:
        answer = "⚡ <b>РАДИОЭЛЕКТРОНИКА: сбор 73 860 ₽</b> (Приложение №1)\n\n" + answer

    # Финальная сноска — всегда
    if "декларант" not in answer.lower():
        answer += "\n\n📌 <i>Точную информацию уточняйте у декларанта.</i>"

    save_message(user_id, message.from_user.username or "", "assistant", answer)
    await safe_send(message, answer)
