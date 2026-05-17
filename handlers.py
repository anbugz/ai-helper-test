"""
handlers.py — все хэндлеры aiogram.
"""
import asyncio
import re
from datetime import timedelta
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import Message

from bot_instance import dp, bot
from config import ADMIN_ID, LEARN_MODE, PENDING_CODE_UPDATE, RADIO_ELECTRONICS_CODES_SET, logger
from database import (
    save_message, clear_history, save_correction, save_custom_codes,
    get_knowledge, get_knowledge_by_topic, save_knowledge,
    get_dialogs_for_export, create_logs_xlsx,
)
from parsers import parse_xlsx, parse_docx, parse_txt, _extract_codes_from_rows
import tnved_engine
from tnved_engine import load_tnved_rows, is_radio_electronics, extract_tnved_codes
from utils import (
    check_rate_limit, now_msk, detect_base_currency, get_cbr_rates,
    format_cross_rates, build_messages, ask_deepseek, safe_send, parse_date_range,
)


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
        "• Напиши «несогласен» или «неверно» — запишешь замечание.\n"
        "• /clear — очистить историю.\n"
        "• /help — эта справка."
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
        "Валюта: инвойс. Страховка: в ТС."
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
    await message.answer("📥 Пришли .xlsx с кодами ТН ВЭД. Ожидание: 10 мин.")


@dp.message(F.document)
async def handle_document(message: Message):
    doc = message.document
    user_id = message.from_user.id
    file_name = (doc.file_name or "").lower()

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


def _strip_deepseek_dup(answer: str) -> str:
    """Вырезает дубли от DeepSeek: шапку с кодом и блок Платежи."""
    lines = answer.split("\n")
    
    # === ШАГ 1: найти и вырезать блок "ПЛАТЕЖИ:" или "📊 Платежи:" ===
    pay_start = -1
    for i, line in enumerate(lines):
        ls = line.strip().lower()
        # Ловим любой вариант: "платежи:", "📊 платежи:", "**платежи**"
        if ls in ("платежи:", "платежи") or ls.startswith("📊 платежи:") or ls.startswith("📊 **платежи**") or "**платежи**" in ls or "платежи в валюте" in ls:
            pay_start = i
            break
    if pay_start >= 0:
        lines = lines[:pay_start]
    
    # === ШАГ 2: найти и вырезать шапку с кодом (перед 📊 ТС) ===
    tc_start = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("📊 Таможенная стоимость:") or line.strip().startswith("📊 Таможенная стоим"):
            tc_start = i
            break
    if tc_start > 0:
        header_end = tc_start
        has_header = False
        for j in range(tc_start - 1, -1, -1):
            ls = lines[j].strip()
            if any(ls.startswith(k) for k in ("📋 Код:", "📋 **Код:**", "🔧", "💰", "🧾", "⚡")):
                has_header = True
                header_end = j
            elif ls == "":
                continue
            else:
                break
        if has_header:
            lines = lines[:header_end] + lines[tc_start:]
    
    return "\n".join(lines).strip()


def _format_payments_box(answer: str, currency: str, rates: dict = None, tariff_info: dict = None, is_radio: bool = False) -> str:
    """Красивый итоговый расчёт с ТС, эмодзи и рублевым эквивалентом."""
    import re
    lines = answer.split("\n")
    data: dict = {}
    ts_val = None
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
        # ТС
        if any(k in ls for k in ("тс:", "таможенная стоимость:")) and not ls.startswith("ндс"):
            m = re.search(r"([\d\s,.]+)\s*(?:" + cur_re + r")", line, re.IGNORECASE)
            if m:
                ts_val = m.group(1).strip().replace(" ", "")
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
    # Ставки для подписей
    duty_label = "Пошлина"
    vat_label = "НДС"
    fee_label = "Сбор"
    if tariff_info:
        pt = tariff_info.get("parsed_tariff", {})
        if pt.get("type") == "percent":
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
        elif pt.get("type") in ("min", "plus", "fixed_eur"):
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
        vat_label = "НДС 22%"
        if is_radio:
            fee_label = "Сбор (радио) 73 860 ₽"
    # Рублевый эквивалент
    def to_rub(val_str):
        if not rates or currency not in rates:
            return None
        try:
            rate = float(rates[currency])
            v = float(val_str.replace(" ", "").replace(",", "."))
            return round(v * rate, 2)
        except (ValueError, TypeError):
            return None
    # Собираем блок
    parts = ["\n📊 <b>Итоговый расчёт</b>\n"]
    # ТС
    if ts_val:
        parts.append(f"💰 Таможенная стоимость: <code>{ts_val} {currency}</code>")
        rub = to_rub(ts_val)
        if rub:
            rub_str = f"{rub:,.2f}".replace(",", " ")
            parts.append(f"   ~ <code>{rub_str} ₽</code>")
        parts.append("")
    # Платежи
    for key, emoji in (("пошлина", "📋"), ("ндс", "🧾"), ("сбор", "⚡")):
        if key in data:
            lbl = {"пошлина": duty_label, "ндс": vat_label, "сбор": fee_label}[key]
            parts.append(f"{emoji} {lbl}: <code>{data[key]} {currency}</code>")
            rub = to_rub(data[key])
            if rub and rub > 0:
                rub_str = f"{rub:,.2f}".replace(",", " ")
                parts.append(f"   ~ <code>{rub_str} ₽</code>")
    # Итого
    if "итого" in data:
        parts.append("─────────────────────")
        parts.append(f"💵 <b>ИТОГО:</b> <code>{data['итого']} {currency}</code>")
        rub = to_rub(data['итого'])
        if rub:
            rub_str = f"{rub:,.2f}".replace(",", " ")
            parts.append(f"   ~ <code>{rub_str} ₽</code>")
    return "\n".join(parts)


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

    # Сначала ищем коды
    found_codes = []
    missing = []
    if codes:
        for c in codes[:3]:
            info = _gtd(c)
            if info:
                found_codes.append(info)
            else:
                missing.append(c)

    calc_words = ("инвойс", "сумма", "стоимость", "расчёт", "платеж", "пошлина",
                  "ндс", "сбор", "таможенная", "тс", "фрахт", "страховк",
                  "посчитай", "сколько будет", "узнать плат", "сколько плат")
    # Если есть код и число больше 1000 — считаем расчётным запросом
    has_amount = bool(re.search(r"\d{3,}", user_text))
    is_calc = any(w in user_text.lower() for w in calc_words) or (bool(found_codes) and has_amount)

    # === БЫСТРЫЙ ОТВЕТ: только код, без расчёта ===
    if codes and found_codes and not is_calc:
        info = found_codes[0]
        pt = info["parsed_tariff"]
        # Тип пошлины
        if pt.get("type") in ("min", "plus", "fixed_eur"):
            duty_type = f"комбинированная ({pt['formula']})"
        elif pt.get("type") == "percent":
            duty_type = "адвалорная"
        else:
            duty_type = info['tariff']
        # НДС
        vat = "10%" if any(w in info['name'].lower() for w in ("пищев", "детск", "медиц", "книг", "печат")) else "22%"
        # Радио
        radio = "\n⚡ Сбор 73 860 ₽" if any(is_radio_electronics(c) for c in codes) else ""
        await message.answer(
            f"📋 <code>{info['code']}</code>\n"
            f"🔧 {info['name']}\n"
            f"💰 Пошлина: {info['tariff']} — {duty_type}\n"
            f"🧾 НДС: {vat}"
            f"{radio}"
            f"\n\n📌 <i>Точную информацию уточняйте у декларанта.</i>"
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

    # === ВЫРЕЗАЕМ дубли от DeepSeek (шапка + блок Платежи) ===
    answer = _strip_deepseek_dup(answer)

    # === ШАПКА (одна, сверху) ===
    header = ""
    if found_codes:
        info = found_codes[0]
        pt = info["parsed_tariff"]
        header = f"📋 <b>Код:</b> <code>{info['code']}</code>\n"
        name_clean = re.sub(r'\s*\(за исключением[^)]+\)', '', info['name']).strip()
        header += f"🔧 {name_clean}\n"
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

    # === ТАБЛИЦА ПЛАТЕЖЕЙ (одна, без ТС) ===
    if is_calc and base_cur != "RUB":
        ti = found_codes[0] if found_codes else None
        box = _format_payments_box(answer, base_cur, rates, tariff_info=ti, is_radio=radio_detected)
        if box:
            answer += box

    # Курс ЦБ уже в таблице — отдельная сноска не нужна
    if rates and base_cur != "RUB":
        rate = rates.get(base_cur, "")
        if rate and rate != "н/д" and rate not in answer and "курсу ЦБ" not in answer:
            answer += f"\n\nℹ️ <i>Курс ЦБ РФ на {rates.get('DATE','сегодня')}: 1 {base_cur} = {rate} ₽</i>"

    # НДС fallback
    if not any(k in answer.lower() for k in ("ндс", "налог на добавленную")):
        if any(w in user_text.lower() for w in ("расчёт", "пошлина", "сбор", "ндс")):
            answer += "\n\n<i>НДС: 22% базовая, 10% льготная.</i>"

    # Склеиваем
    if header:
        answer = header + "\n" + answer

    if radio_detected and "⚡" not in answer and "73860" not in answer:
        answer = "⚡ <b>РАДИОЭЛЕКТРОНИКА: сбор 73 860 ₽</b> (Приложение №1)\n\n" + answer

    if found_codes and "декларант" not in answer.lower():
        answer += "\n\n📌 <i>Точную информацию уточняйте у декларанта.</i>"

    # Курс ЦБ РФ — всегда в конце
    try:
        rates = await get_cbr_rates()
        cny = rates.get('CNY', 'н/д')
        usd = rates.get('USD', 'н/д')
        date = rates.get('DATE', 'сегодня')
        answer += f"\n\n💱 <i>Курс ЦБ РФ на {date}: 1 USD = {usd} ₽, 1 CNY = {cny} ₽</i>"
    except Exception:
        pass

    save_message(user_id, message.from_user.username or "", "assistant", answer)
    await safe_send(message, answer)
