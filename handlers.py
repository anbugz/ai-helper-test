"""
handlers.py — все хэндлеры aiogram (@dp.message).
Импортирует bot_instance (dp, bot), config, database, parsers, tnved_engine, utils.
"""
import asyncio
import re
from datetime import datetime, timedelta
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import Message

from bot_instance import dp, bot
from config import (
    ADMIN_ID, LEARN_MODE, PENDING_CODE_UPDATE,
    RADIO_ELECTRONICS_CODES_SET, logger,
)
from database import (
    save_message, clear_history, save_correction,
    save_custom_codes, get_knowledge, get_knowledge_by_topic,
    save_knowledge, get_settings, update_settings,
    get_dialogs_for_export, create_logs_xlsx,
)
from parsers import parse_xlsx, parse_docx, parse_txt, _extract_codes_from_rows
from tnved_engine import (
    load_tnved_rows, get_tnved_from_cache,
    is_radio_electronics, extract_tnved_codes,
    _TNVED_ROWS_CACHE,
)
from utils import (
    check_rate_limit, now_msk, detect_base_currency,
    get_cbr_rates, format_cross_rates, build_messages,
    ask_deepseek, safe_send, parse_date_range,
)


# ==============================================================================
# /start
# ==============================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "<b>West Asia AI Helper</b> — помощник для менеджеров по ВЭД и логистике.\n\n"
        "Просто напиши вопрос — помогу с расчётами, сроками, маршрутами.\n\n"
        "Если ответ неправильный — напиши «несогласен» или «неверно» на моё сообщение."
    )


# ==============================================================================
# /help
# ==============================================================================
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Справка:</b>\n"
        "• Отправь текст с кодом ТН ВЭД — получишь расчёт.\n"
        "• Отправь .xlsx файл — извлеку данные.\n"
        "• Напиши «несогласен» или «неверно» на моё сообщение — запишешь замечание.\n"
        "• /clear — очистить историю диалога.\n"
        "• /help — эта справка."
    )


# ==============================================================================
# /clear
# ==============================================================================
@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    clear_history(message.from_user.id)
    await message.answer("🗑 История диалога очищена.")


# ==============================================================================
# /brief — только АБ
# ==============================================================================
@dp.message(Command("brief"))
async def cmd_brief(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ <b>Нет доступа.</b>")
        return
    # ... полный текст brief из rev10
    await message.answer(
        "<b>BRIEF — AI Helper West Asia</b>\n\n"
        "<b>НДС:</b> базовая 22% с 01.01.2026. Льготная 10% — продовольствие, детские, медицина, печать.\n"
        "<b>Таможенный сбор:</b> шкала ПП РФ №1637 (в ред. №1638).\n"
        "<b>Радиоэлектроника:</b> 73 860 ₽ (106 кодов, Приложение №1).\n"
        "<b>Валюта:</b> валюта инвойса. Кросс через рубль ЦБ РФ.\n"
        "<b>Страхование:</b> в ТС, не отдельной строкой.\n"
        "<b>Рубли:</b> только справочно."
    )


# ==============================================================================
# /topics — только АБ
# ==============================================================================
@dp.message(Command("topics"))
async def cmd_topics(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ <b>Нет доступа.</b>")
        return
    topics = get_knowledge()
    if not topics:
        await message.answer("📭 База знаний пуста.")
        return
    lines = [f"{i+1}. {t['topic']}" for i, t in enumerate(topics)]
    await message.answer("<b>Темы в базе знаний:</b>\n" + "\n".join(lines))


# ==============================================================================
# /learn — только АБ
# ==============================================================================
@dp.message(Command("learn"))
async def cmd_learn(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ <b>Нет доступа.</b>")
        return
    topic = message.text.replace("/learn", "").strip()
    if not topic:
        await message.answer("Использование: /learn <тема>")
        return
    user_id = message.from_user.id
    LEARN_MODE[user_id] = {"topic": topic, "content": "", "questions": [], "waiting_for": "content"}
    await message.answer(
        f"📚 <b>Режим обучения: {topic}</b>\n\n"
        f"Пришли текст (.txt, .docx, .xlsx) или напиши текстом.\n"
        f"/done — выйти."
    )


# ==============================================================================
# /done — только АБ (в контексте /learn)
# ==============================================================================
@dp.message(Command("done"))
async def cmd_done(message: Message):
    user_id = message.from_user.id
    if user_id not in LEARN_MODE:
        await message.answer("Ты не в режиме обучения.")
        return
    if user_id != ADMIN_ID:
        await message.answer("⛔ <b>Нет доступа.</b>")
        return
    mode = LEARN_MODE.pop(user_id)
    topic = mode["topic"]
    content = mode["content"]
    questions = "\n".join(mode["questions"]) if mode["questions"] else ""
    if content:
        save_knowledge(topic, content, questions, message.from_user.username or str(user_id))
        await message.answer(f"✅ Тема «{topic}» сохранена.")
    else:
        await message.answer("❌ Нет контента — тема не сохранена.")


# ==============================================================================
# /updatecodes — только АБ
# ==============================================================================
@dp.message(Command("updatecodes"))
async def cmd_updatecodes(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ <b>Нет доступа.</b>")
        return
    PENDING_CODE_UPDATE[message.from_user.id] = now_msk()
    await message.answer(
        "📥 <b>Режим обновления кодов</b>\n\n"
        "Пришли .xlsx файл с перечнем кодов ТН ВЭД.\n"
        "Я извлеку все 6–10-значные коды и обновлю базу.\n\n"
        "<i>Ожидание файла: 10 минут.</i>"
    )


# ==============================================================================
# ОБРАБОТКА ДОКУМЕНТОВ
# ==============================================================================
@dp.message(F.document)
async def handle_document(message: Message):
    doc = message.document
    user_id = message.from_user.id
    file_name = (doc.file_name or "").lower()

    # Режим обучения — приоритет
    if user_id in LEARN_MODE and LEARN_MODE[user_id].get("waiting_for") == "content":
        if not any(file_name.endswith(ext) for ext in [".txt", ".docx", ".xlsx"]):
            await message.answer("В режиме обучения принимаю только .txt, .docx, .xlsx")
            return
        try:
            file = await bot.get_file(doc.file_id)
            bytes_io = await bot.download_file(file.file_path)
            if file_name.endswith(".txt"):
                text = parse_txt(bytes_io)
            elif file_name.endswith(".docx"):
                text = parse_docx(bytes_io)
            elif file_name.endswith(".xlsx"):
                rows = parse_xlsx(bytes_io)
                text = "\n".join(" | ".join(str(c) for c in row) for row in rows)
            else:
                text = ""
            if not text.strip():
                await message.answer("Не удалось прочитать файл или он пустой.")
                return
            LEARN_MODE[user_id]["content"] = text
            LEARN_MODE[user_id]["waiting_for"] = "questions"
            await message.answer("✅ Материал получен. Тема сохранена в базу знаний.")
        except Exception as e:
            logger.error(f"Ошибка обработки файла для обучения: {e}")
            await message.answer(f"Ошибка: {e}")
        return

    # Обычный режим — только xlsx
    if not file_name.endswith(".xlsx"):
        await message.answer("Пока принимаю только .xlsx файлы.")
        return

    now = now_msk()
    is_code_update = False

    if user_id in PENDING_CODE_UPDATE:
        if user_id != ADMIN_ID:
            await message.answer("⛔ <b>Нет доступа.</b> Обновление кодов доступно только АБ.")
            del PENDING_CODE_UPDATE[user_id]
            return
        if (now - PENDING_CODE_UPDATE[user_id]) < timedelta(minutes=10):
            is_code_update = True
        del PENDING_CODE_UPDATE[user_id]

    try:
        file = await bot.get_file(doc.file_id)
        bytes_io = await bot.download_file(file.file_path)
        data = parse_xlsx(bytes_io)
        if not data:
            await message.answer("Не удалось прочитать файл.")
            return

        # Автозагрузка справочника ТН ВЭД
        has_tnved_codes = any(
            isinstance(r[0], str) and re.match(r"\d{10}", r[0].replace(" ", ""))
            for r in data if r
        )
        if has_tnved_codes:
            load_tnved_rows(data)
            await message.answer(
                f"📋 <b>Справочник ТН ВЭД загружен</b>\n"
                f"Кодов в кэше: {len(_TNVED_ROWS_CACHE)}\n"
                f"Теперь бот берёт реальные ставки из файла."
            )

        if is_code_update:
            codes = _extract_codes_from_rows(data)
            if not codes:
                await message.answer("❌ В файле не найдены коды ТН ВЭД.")
                return
            save_custom_codes(codes)
            await message.answer(
                f"✅ <b>База кодов обновлена</b>\n\n"
                f"Загружено: <b>{len(codes)}</b> кодов\n"
                f"Примеры: {', '.join(codes[:5])}"
            )
            return

        # Показ данных
        lines = ["<b>Данные из Excel:</b>"]
        data_rows = [r for r in data if r and any(str(c).strip() for c in r)]
        for i, row in enumerate(data_rows[:15], 1):
            line = " | ".join(str(c)[:40] for c in row[:4])
            lines.append(f"{i}. {line}")
        if len(data_rows) > 15:
            lines.append(f"... и ещё {len(data_rows) - 15} строк.")
        await safe_send(message, "\n".join(lines))

    except Exception as e:
        logger.error(f"Ошибка обработки документа: {e}")
        await message.answer(f"Ошибка обработки файла: {e}")


# ==============================================================================
# ОСНОВНОЙ ХЭНДЛЕР ТЕКСТА
# ==============================================================================
@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    user_text = message.text or ""
    if not user_text:
        return

    # Игнорируем команды
    if user_text.startswith("/"):
        return

    # 1. Экспорт логов (только АБ)
    if user_id == ADMIN_ID:
        log_keywords = ["логи", "выгрузи", "экспорт", "вопросы", "диалоги", "история"]
        if any(k in user_text.lower() for k in log_keywords):
            date_from, date_to = parse_date_range(user_text)
            logs = get_dialogs_for_export(date_from, date_to)
            if not logs:
                await message.answer("📭 Логов не найдено.")
                return
            xlsx_bytes = create_logs_xlsx(logs, "logs")
            filename = f"logs_{date_from or 'all'}_{date_to or 'all'}.xlsx"
            await message.answer_document(
                document=types.BufferedInputFile(xlsx_bytes, filename=filename),
                caption=f"📊 Логи диалогов\nПериод: {date_from or 'все'} — {date_to or 'все'}\nЗаписей: {len(logs)}"
            )
            return

    # 2. "Несогласен" / "неверно" — замечание на ответ бота
    dispute_keywords = [
        "несогласен", "не согласен", "неправильно", "ошибка",
        "не так", "неверно", "неверный", "спорю", "не верно",
        "wrong", "incorrect", "disagree"
    ]
    if any(k in user_text.lower() for k in dispute_keywords):
        original = ""
        if message.reply_to_message and message.reply_to_message.text:
            original = message.reply_to_message.text
        save_correction(
            user_id=user_id,
            username=message.from_user.username or "",
            original=original,
            correction=user_text,
        )
        # Уведомление АБ
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Менеджер @{message.from_user.username or user_id} не согласен:</b>\n\n"
                    f"<b>Ответ бота:</b> {original[:300]}\n"
                    f"<b>Комментарий:</b> {user_text[:400]}",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить АБ: {e}")
        await message.answer("⚠️ Замечание записано. АБ проверит.")
        return

    # 3. Режим обучения
    if user_id in LEARN_MODE and LEARN_MODE[user_id].get("waiting_for") == "questions":
        await message.answer("✅ Ответ записан.")
        return

    # 4. Rate limit
    if not check_rate_limit(user_id):
        return

    # 5. Основной AI-диалог
    logger.info(f"User {user_id} ({message.from_user.username}): {user_text[:80]}...")
    save_message(user_id, message.from_user.username or "", "user", user_text)

    codes = extract_tnved_codes(user_text)
    radio_detected = any(is_radio_electronics(c) for c in codes)

    # --- ЖЁСТКАЯ ПРОВЕРКА КОДОВ ТН ВЭД ---
    if codes and not _TNVED_ROWS_CACHE:
        await safe_send(
            message,
            f"⚠️ <b>Код ТН ВЭД обнаружен, но справочник не загружен</b>\n\n"
            f"Обнаруженные коды: {', '.join(codes[:3])}\n\n"
            f"Для расчёта загрузите файл <code>TWS_TNVED_*.xlsx</code>."
        )
        return

    tnved_context = ""
    missing_codes = []
    found_codes = []
    if codes and _TNVED_ROWS_CACHE:
        for code in codes[:3]:
            info = get_tnved_from_cache(code)
            if info:
                found_codes.append(info)
                pt = info["parsed_tariff"]
                tnved_context += (
                    f"\nКОД ТН ВЭД {info['code']}: {info['name'][:80]}. "
                    f"ТАМОЖЕННАЯ СТАВКА: {info['tariff']}. "
                )
                if pt.get("formula"):
                    tnved_context += f"Расшифровка: {pt['formula']}. "
                if info.get("has_euro_component"):
                    tnved_context += (
                        "ВАЖНО: пошлина комбинированная — считай 2 значения и бери МАКСИМУМ. "
                        "Для евро-компоненты нужен ВЕС."
                    )
            else:
                missing_codes.append(code)

    if codes and not found_codes:
        await safe_send(
            message,
            f"⚠️ <b>Код(ы) не найдены в справочнике</b>\n\n"
            f"{', '.join(missing_codes)}\n\n"
            f"Проверьте правильность или уточните у декларанта."
        )
        return

    if missing_codes:
        tnved_context += (
            f"\nВНИМАНИЕ: коды не найдены: {', '.join(missing_codes)}. "
            f"Расчёт только для найденных."
        )

    # Определяем валюту и страхование
    base_currency = detect_base_currency(user_text)
    has_insurance = any(w in user_text.lower() for w in ("страховка", "страхование", "insured", "insurance"))

    # Курсы ЦБ
    rates = None
    try:
        rates = await get_cbr_rates()
        cross = format_cross_rates(rates)
        extra = (
            f"[КУРСЫ ЦБ РФ на {rates.get('DATE', 'н/д')}] "
            f"CNY={rates.get('CNY', 'н/д')}₽, USD={rates.get('USD', 'н/д')}₽, EUR={rates.get('EUR', 'н/д')}₽. "
            f"Кросс: {cross}. "
            f"Базовая валюта: {base_currency}. "
            f"НДС: 22% базовая, 10% льготная. "
        )
        if has_insurance:
            extra += "Страхование — ВКЛЮЧИ в ТС (п. 1 ст. 40 ТК ЕАЭС). "
        if tnved_context:
            extra += f"[ТН ВЭД ИЗ СПРАВОЧНИКА]{tnved_context}\n"
        extra += "НЕ придумывай ставки — используй ТОЛЬКО данные из справочника."
    except Exception as e:
        logger.error(f"Курсы ЦБ недоступны: {e}")
        extra = f"[КУРСЫ ЦБ РФ недоступны]. НДС: 22%/10%. Проверяй ставку по коду ТН ВЭД."
        if tnved_context:
            extra += f"[ТН ВЭД]{tnved_context}"

    msgs = build_messages(user_id, user_text, extra_context=extra)
    answer = await ask_deepseek(msgs)

    # Пост-обработка валюты
    if base_currency != "RUB" and base_currency not in answer.upper() and rates:
        answer += (
            f"\n\n<i>Курс ЦБ РФ на {rates.get('DATE', '')}: "
            f"CNY={rates.get('CNY', '')}₽, USD={rates.get('USD', '')}₽, EUR={rates.get('EUR', '')}₽. "
            f"Расчёт в {base_currency}, рубли — справочно.</i>"
        )

    # Пост-обработка НДС
    if not any(k in answer.lower() for k in ("ндс", "налог на добавленную", "nds")):
        if any(w in user_text.lower() for w in ("расчёт", "стоимость", "таможенная", "пошлина", "сбор", "ндс")):
            answer += "\n\n<i>НДС: базовая 22% с 01.01.2026, льготная 10%.</i>"

    # Двойная защита по радиоэлектронике
    if radio_detected and "⚡" not in answer and "73860" not in answer:
        answer = (
            "⚡ <b>РАДИОЭЛЕКТРОНИКА: фиксированный сбор 73 860 ₽</b> (Приложение №1 к ПП РФ №1637)\n\n"
            + answer
        )

    save_message(user_id, message.from_user.username or "", "assistant", answer)
    await safe_send(message, answer)
