"""
handlers.py — все хэндлеры aiogram.
"""
import asyncio
from typing import Dict
import re
from datetime import timedelta
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import Message

from bot_instance import dp, bot
from config import (
    ADMIN_ID,
    LEARN_MODE,
    PENDING_CODE_UPDATE,
    RADIO_ELECTRONICS_CODES_SET,
    RADIO_FEE,
    CUSTOMS_FEE_RUB,
    TNVED_FULL_NAMES,
    logger,
)
from database import (
    save_message,
    save_correction,
    save_custom_codes,
    get_knowledge,
    get_knowledge_by_topic,
    save_knowledge,
    get_dialogs_for_export,
    create_logs_xlsx,
    search_tnved_in_db,
)
from parsers import parse_xlsx, parse_docx, parse_txt, _extract_codes_from_rows
import tnved_engine
from tnved_engine import (
    load_tnved_rows,
    is_radio_electronics,
    extract_tnved_codes,
    get_tnved_from_cache,
    calculate_customs_fee,
)
from calc_engine import _format_calculation_fallback, _strip_deepseek_dup, _strip_ai_assistant_junk
from utils import (
    check_rate_limit,
    now_msk,
    detect_base_currency,
    get_cbr_rates,
    format_cross_rates,
    build_messages,
    ask_deepseek,
    safe_send,
    parse_date_range,
    extract_ts_components_with_currency,
    convert_fee_to_currency,
)


# ------------------------------------------------------------------
# Команды
# ------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "<b>West Asia AI Helper</b> — помощник для менеджеров по ВЭД и логистике.\n\n"
        "Просто напиши вопрос — помогу с расчётами, сроками, маршрутами.\n\n"
        "Если ответ неправильный — ответь на моё сообщение словом «несогласен»."
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Справка:</b>\n"
        "• Отправь текст с кодом ТН ВЭД — получишь расчёт.\n"
        "• Отправь .xlsx файл — извлеку данные.\n"
        "• Ответь «несогласен» на сообщение бота — запишешь замечание.\n"
        "• /clear — визуально очистить чат (логи НЕ удаляются).\n"
        "• /help — эта справка."
    )

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    """Визуально очищает чат — разделитель, НЕ удаляет логи из БД."""
    await message.answer(
        "\n".join([
            " ",
            "═══════════ 🧹 ИСТОРИЯ ЧАТА ОЧИЩЕНА 🧹 ═══════════",
            " ",
        ])
    )

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
    LEARN_MODE[message.from_user.id] = {
        "topic": topic,
        "content": "",
        "questions": [],
        "waiting_for": "content",
    }
    await message.answer(
        f"📚 Режим обучения: {topic}\nПришли текст или файл. /done — выйти."
    )

@dp.message(Command("done"))
async def cmd_done(message: Message):
    uid = message.from_user.id
    if uid not in LEARN_MODE:
        await message.answer("Ты не в режиме обучения.")
        return
    mode = LEARN_MODE.pop(uid)
    if mode["content"]:
        save_knowledge(
            mode["topic"], mode["content"], "", message.from_user.username or str(uid)
        )
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


# ------------------------------------------------------------------
# Документы
# ------------------------------------------------------------------

@dp.message(F.document)
async def handle_document(message: Message):
    doc = message.document
    user_id = message.from_user.id
    file_name = (doc.file_name or "").lower()

    if (
        user_id in LEARN_MODE
        and LEARN_MODE[user_id].get("waiting_for") == "content"
    ):
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

        has_tnved = any(
            isinstance(r[0], str)
            and re.match(r"\d{10}", r[0].replace(" ", ""))
            for r in data
            if r
        )

        if has_tnved and (is_code_update or user_id == ADMIN_ID):
            load_tnved_rows(data)
            await message.answer(
                f"📋 Загружено: {len(tnved_engine._TNVED_ROWS_CACHE)} кодов"
            )

        if is_code_update:
            codes = _extract_codes_from_rows(data)
            if not codes:
                await message.answer("❌ Коды не найдены.")
                return
            save_custom_codes(codes)
            await message.answer(
                f"✅ {len(codes)} кодов. Примеры: {', '.join(codes[:5])}"
            )
            return

        lines = ["<b>Excel:</b>"]
        for i, row in enumerate(
            [r for r in data if r and any(str(c).strip() for c in r)][:15], 1
        ):
            lines.append(f"{i}. {' | '.join(str(c)[:40] for c in row[:4])}")
        await safe_send(message, "\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"Ошибка: {e}")


# ------------------------------------------------------------------
# Текст
# ------------------------------------------------------------------

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    user_text = message.text or ""
    if not user_text or user_text.startswith("/"):
        return

    text_lower = user_text.lower()

    # === ОБРАБОТКА ТРАНСЛИТА (vatnye volokna → ватные волокна) ===
    has_cyrillic = bool(re.search(r'[а-яё]', text_lower))
    has_latin = bool(re.search(r'[a-z]', text_lower))
    if not has_cyrillic and has_latin:
        translit_map = str.maketrans(
            "abvgdeziyklmnoprstufhe'chshyaeyu",
            "абвгдезийклмнопрстуфхэчшьяею",
        )
        text_lower = text_lower.translate(translit_map)

    log_keywords = ["логи", "выгрузи логи", "экспорт логов", "логи работы"]
    if user_id == ADMIN_ID and any(
        text_lower.startswith(k) or f" {k} " in f" {text_lower} " for k in log_keywords
    ):
        df, dt = parse_date_range(user_text)
        logs = get_dialogs_for_export(df, dt)
        if not logs:
            await message.answer("📭 Пусто.")
            return
        xb = create_logs_xlsx(logs, "logs")
        fn = f"logs_{df or 'all'}_{dt or 'all'}.xlsx"
        await message.answer_document(
            document=types.BufferedInputFile(xb, filename=fn),
            caption=f"📊 {len(logs)} записей",
        )
        return

    if any(
        k in text_lower for k in ["несогласен", "не согласен", "неправильно", "неверно"]
    ):
        if not message.reply_to_message:
            await message.answer(
                "Для записи замечания ответьте на сообщение бота словом «несогласен»."
            )
            return
        orig = message.reply_to_message.text or ""
        save_correction(
            user_id,
            message.from_user.username or "",
            orig[:500],
            user_text[:500],
        )
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ @{message.from_user.username or user_id}: {user_text[:200]}",
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить: {e}")
        await message.answer("⚠️ Замечание записано.")
        return

    if user_id in LEARN_MODE:
        LEARN_MODE[user_id]["content"] += "\n" + user_text
        await message.answer("✅ Записано.")
        return

    if not check_rate_limit(user_id):
        return

    logger.info(f"User {user_id}: {user_text[:80]}...")
    save_message(user_id, message.from_user.username or "", "user", user_text)

    codes = extract_tnved_codes(user_text)
    radio_detected = any(is_radio_electronics(c) for c in codes)

    found_codes = []
    missing = []
    if codes:
        for c in codes[:3]:
            info = get_tnved_from_cache(c)
            if info:
                # Очищаем наименование от некорректных символов
                info["name"] = info.get("name", "").replace("🠺", "→").replace("🠔", "←").strip()
                found_codes.append(info)
            else:
                missing.append(c)

    calc_words = (
        "инвойс",
        "сумма",
        "стоимость",
        "расчёт",
        "платеж",
        "пошлина",
        "ндс",
        "сбор",
        "таможенная",
        "тс",
        "фрахт",
        "страховк",
        "посчитай",
        "сколько будет",
        "узнать плат",
        "сколько плат",
    )
    text_no_codes = re.sub(r"\d{8,10}", "", user_text)
    has_amount = bool(re.search(r"\d{3,}", text_no_codes))
    is_calc = any(w in text_lower for w in calc_words) or (
        bool(found_codes) and has_amount
    )

    if codes and found_codes and not is_calc:
        info = found_codes[0]
        pt = info["parsed_tariff"]
        if pt.get("type") in ("min", "plus", "fixed_eur"):
            duty_type = f"комбинированная ({pt['formula']})"
        elif pt.get("type") == "percent":
            duty_type = "адвалорная"
        else:
            duty_type = info["tariff"]
        vat = (
            "10%"
            if any(w in info["name"].lower() for w in ("пищев", "детск", "медиц", "книг", "печат"))
            else "22%"
        )
        radio = (
            "\n⚡ Сбор 73 860 ₽"
            if any(is_radio_electronics(c) for c in codes)
            else ""
        )
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
        await safe_send(
            message, f"❌ Код не найден: <code>{', '.join(missing)}</code>"
        )
        return

    # === ПОИСК ПО ОПИСАНИЮ (если нет явных кодов в запросе) ===
    if not found_codes and not codes:
        # Извлекаем потенциальные ключевые слова (существительные, 4+ букв)
        keywords = re.findall(r'[а-яё]{4,}', text_lower)
        keywords = [w for w in keywords if w not in (
            "подбери", "какой", "код", "товар", "груз", "штука", "кг", "вес",
            "цена", "стоимость", "сумма", "рубль", "доллар", "евро", "юань",
            "нужен", "расчёт", "помоги", "пожалуйста", "привет", "скажи",
            "будь", "добрый", "можно", "сколько", "стоить", "будет",
        )]
        if keywords:
            # Ищем в кэше по первому ключевому слову
            search_results = search_tnved_in_db(keywords[0])
            if search_results:
                # Показываем до 3 результатов
                lines = [f"📋 Найдено по слову \"<b>{keywords[0]}</b>\":"]
                for r in search_results[:3]:
                    code = r["code"]
                    name = r["name"].replace("🠺", "→").strip() if r["name"] else "—"
                    tariff = r.get("tariff", "—")
                    lines.append(f"\n🔹 <code>{code}</code>")
                    lines.append(f"   {name}")
                    lines.append(f"   💰 {tariff}")
                lines.append("\n📌 Для расчёта отправь: <code>КОД СУММА_ИНВОЙСА</code>")
                await safe_send(message, "\n".join(lines))
                return
            else:
                # Ничего не найдено — честно говорим
                if any(w in text_lower for w in ("подбери", "какой код", "найди код")):
                    await safe_send(
                        message,
                        f"❌ По слову \"<b>{keywords[0]}</b>\" ничего не найдено в справочнике tws.by.\n\n"
                        f"📌 Загрузите актуальный справочник через /updatecodes\n"
                        f"📌 Или уточните описание товара"
                    )
                    return

    base_cur = detect_base_currency(user_text)
    has_ins = any(w in text_lower for w in ("страховка", "страхование"))

    # ============================================================
    # ОПРЕДЕЛЯЕМ ТИП ЗАПРОСА:
    # 1. ВЭД-расчёт (is_calc=True)    → шапка + конвертация + итог
    # 2. Быстрый ответ (код, не calc) → шапка + дисклеймер (уже обработано выше)
    # 3. AI-ассистент (нет кода)      → общий вопрос → DeepSeek без ВЭД-контекста
    # ============================================================
    
    # Инициализация переменных (используются в обоих сценариях)
    ti = found_codes[0] if found_codes else None
    vat_rate = 0.22
    customs_fee_rub = 0.0
    ts_fallback = 0.0
    ts_components: Dict[str, Dict[str, any]] = {}
    comps: Dict[str, Dict[str, any]] = {}

    rates = None
    try:
        rates = await get_cbr_rates()
    except Exception as e:
        logger.error(f"Курсы: {e}")

    if is_calc and found_codes:
        # === СЦЕНАРИЙ 1: ВЭД-РАСЧЁТ ===
        cr = format_cross_rates(rates) if rates else ""
        extra = (
            f"[КУРСЫ ЦБ РФ {rates.get('DATE','') if rates else ''}] "
            f"CNY={rates.get('CNY','') if rates else ''}₽ "
            f"USD={rates.get('USD','') if rates else ''}₽ "
            f"EUR={rates.get('EUR','') if rates else ''}₽. "
            f"Кросс: {cr}. Валюта: {base_cur}. НДС: 22%/10%. "
        )
        extra += (
            "ТС (п.1 ст.40 ТК ЕАЭС): Инвойс + Фрахт + Страховка + Упаковка + Прочее. "
            "Не указано → 0. Всё в валюте инвойса. "
            "Конвертация: чужая валюта → ₽ ЦБ → валюта инвойса. "
            "СБОР (таможенный и радио): считай в ₽, затем конвертируй в валюту инвойса (CNY/USD/EUR). "
            "НЕ пиши курс ЦБ РФ в ответе — он будет добавлен автоматически. "
        )
        if has_ins:
            extra += "Страховка — в ТС. "
        extra += "НЕ придумывай ставки и курсы."

        comps = extract_ts_components_with_currency(user_text)
        # Базовая валюта = валюта инвойса, если определена
        if "invoice" in comps and comps["invoice"]["currency"] != "RUB":
            base_cur = comps["invoice"]["currency"]
        else:
            base_cur = detect_base_currency(user_text)

        # Вычисляем ТС в валюте инвойса с конвертацией
        if "invoice" in comps:
            inv = comps["invoice"]
            ts_fallback += inv["value"]
            ts_components["invoice"] = {
                "value": inv["value"], "currency": inv["currency"],
                "converted": inv["value"], "rate": None,
            }

        for key in ("freight", "insurance"):
            if key in comps:
                comp = comps[key]
                val = comp["value"]
                cur = comp["currency"]
                converted = val
                rate_info = None
                if cur != base_cur and rates and cur in rates and base_cur in rates:
                    try:
                        rub_val = val * float(rates[cur])
                        converted = round(rub_val / float(rates[base_cur]), 2)
                        rate_info = f"{val} {cur} → {rub_val:,.2f} ₽ → {converted:,.2f} {base_cur}"
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass
                ts_fallback += converted
                ts_components[key] = {
                    "value": val, "currency": cur,
                    "converted": converted, "rate": rate_info,
                }

        vat_rate = (
            0.10
            if any(
                w in ti["name"].lower()
                for w in ("пищев", "детск", "медиц", "книг", "печат")
            )
            else 0.22
        )

        ts_rub_for_fee = 0.0
        if ts_fallback and rates:
            if base_cur == "RUB":
                ts_rub_for_fee = ts_fallback
            elif base_cur in rates:
                try:
                    ts_rub_for_fee = ts_fallback * float(rates[base_cur])
                except (ValueError, TypeError):
                    pass

        if radio_detected:
            customs_fee_rub = RADIO_FEE
        else:
            customs_fee_rub = calculate_customs_fee(ts_rub_for_fee)

        # === ВЭД-РАСЧЁТ: НЕ вызываем DeepSeek, сразу fallback ===
        # DeepSeek даёт verbose ответы с "Отлично", пошаговыми разборами,
        # формулами — это не нужно. Fallback даёт чистый расчёт.
        answer = ""

    else:
        # === СЦЕНАРИЙ 3: AI-АССИСТЕНТ (общий вопрос) ===
        # Контекст без ВЭД-специфики и без курса
        extra = "Отвечай как эксперт West Asia по ВЭД и логистике. "
        extra += "НЕ пиши курс ЦБ РФ в ответе — он будет добавлен автоматически."
        msgs = build_messages(user_id, user_text, extra_context=extra)
        answer = await ask_deepseek(msgs)
        
        # Убираем лишнее (markdown, примеры, придуманные курсы)
        answer = _strip_ai_assistant_junk(answer)

    # --- РАСЧЁТНЫЙ ЗАПРОС: чистый fallback ----------------------
    if is_calc and found_codes and ts_fallback and base_cur:
        code_val = found_codes[0]["code"]
        name_val = TNVED_FULL_NAMES.get(
            found_codes[0]["code"][:6], found_codes[0]["name"]
        )
        answer = _format_calculation_fallback(
            code=code_val,
            name=name_val,
            currency=base_cur,
            rates=rates or {},
            tariff_info=ti,
            is_radio=radio_detected,
            customs_fee_rub=customs_fee_rub,
            vat_rate=vat_rate,
            ts_fallback=ts_fallback,
            ts_components=ts_components,
            weight_kg=comps.get("weight_kg"),
        )
    else:
        # --- ШАПКА (только для не-расчётных или без суммы) ------
        header = ""
        if found_codes:
            info = found_codes[0]
            pt = info["parsed_tariff"]
            header = f"📋 <b>Код:</b> <code>{info['code']}</code>\n"
            name_clean = re.sub(r"\s*\(за исключением[^)]+", "", info["name"]).strip()
            full_name = TNVED_FULL_NAMES.get(info["code"][:6], name_clean)
            header += f"🔧 {full_name}\n"
            header += f"💰 <b>Пошлина:</b> {info['tariff']}"
            if pt.get("type") in ("min", "plus", "fixed_eur"):
                header += f" — комбинированная ({pt['formula']})"
            elif pt.get("type") == "percent":
                header += " — адвалорная"
            header += "\n"
            vat_str = "10% (льготная)" if vat_rate == 0.10 else "22% (базовая)"
            header += f"🧾 <b>НДС:</b> {vat_str}\n"
            if radio_detected:
                _, fee_display = convert_fee_to_currency(RADIO_FEE, base_cur or "RUB", rates or {})
                header += f"⚡ <b>Радиоэлектроника:</b> сбор {fee_display}\n"
            if missing:
                header += f"⚠️ Не найдены: {', '.join(missing)}\n"

        # Fallback если DeepSeek не вывёл платежи
        has_deepseek_calc = any(
            k in answer.lower()
            for k in (
                "итого платежей", "итоговый расчёт", "итоговый расчет", "📊 итоговый",
                "платежи в валюте", "платежи:", "итого:", "итого к оплате",
                "таможенная стоимость:", "таможенных платежей",
            )
        )
        if is_calc and base_cur and not has_deepseek_calc:
            code_val = found_codes[0]["code"] if found_codes else None
            name_val = (
                TNVED_FULL_NAMES.get(found_codes[0]["code"][:6], found_codes[0]["name"])
                if found_codes else None
            )
            fallback = _format_calculation_fallback(
                code=code_val,
                name=name_val,
                currency=base_cur,
                rates=rates or {},
                tariff_info=ti,
                is_radio=radio_detected,
                customs_fee_rub=customs_fee_rub,
                vat_rate=vat_rate,
                ts_fallback=ts_fallback,
                ts_components=ts_components,
                weight_kg=comps.get("weight_kg"),
            )
            if fallback:
                answer += "\n\n" + fallback

        if header:
            answer = header + "\n" + answer

    # --- Декларант (только для НЕ-расчётных ответов) ------------
    if found_codes and not is_calc and "декларант" not in answer.lower():
        answer += "\n\n📌 <i>Точную информацию уточняйте у декларанта.</i>"

    # --- Курс ЦБ РФ ----------------------------------------------
    # Добавляем курс ТОЛЬКО если его ещё нет в ответе
    if "💱" not in answer and "курс цб" not in answer.lower():
        try:
            rates = await get_cbr_rates()
            cny = rates.get("CNY", "н/д")
            usd = rates.get("USD", "н/д")
            eur = rates.get("EUR", "н/д")
            date = rates.get("DATE", "сегодня")
            answer += (
                f"\n\n💱 <i>Курс ЦБ РФ на {date}: "
                f"1 USD = {usd} ₽, 1 CNY = {cny} ₽, 1 EUR = {eur} ₽</i>"
            )
        except Exception:
            pass

    save_message(user_id, message.from_user.username or "", "assistant", answer)
    await safe_send(message, answer)
