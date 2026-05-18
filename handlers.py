"""
handlers.py — все хэндлеры aiogram.
"""
import asyncio
from collections import Counter
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
    # Удаляем коды ТН ВЭД из текста (нормализуем пробелы), потом ищем суммы
    # "5208 43 000 0" → нормализуем → "5208430000" → удаляем → остаётся ""
    text_normalized = re.sub(r"(\d)\s+(?=\d)", r"\1", user_text)
    text_no_codes = re.sub(r"\d{8,10}", "", text_normalized)
    # Проверяем наличие числа ≥ 1000 (инвойс/фрахт/страховка)
    has_amount = bool(re.search(r"(?<!\d)\d{4,}(?!\d)", text_no_codes))
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
        # --- ДЕТЕКЦИЯ ВЭД-ИНТЕНТА ---
        # Если запрос НЕ содержит ВЭД-специфичных слов — сразу в AI-ассистент,
        # не тратить время на поиск по ТН ВЭД.
        VED_INTENT_KEYWORDS = (
            # Прямые ВЭД-термины
            "тн вэд", "таможн", "пошлин", "деклар", "оформлен",
            # Действия с кодом
            "подбери", "подбир", "найди код", "какой код", "код товара",
            "код для", "код на ", "шифр", "номенклатур",
            # Материалы (из MATERIAL_MAP + общие)
            "хлопок", "хлопчатобумажн", "шерсть", "шерстяной", "шёлк", "шелк", "шелковый",
            "лён", "лен ", "льняной", "синтетика", "полиэстер", "полиэфир", "акрил",
            "кожа", "кожаный", "сталь", "нержавейка", "алюминий", "медь", "латунь", "цинк",
            "телефон", "смартфон", "ноутбук", "компьютер", "планшет", "монитор", "телевизор",
            # Категории товаров
            "ткань", "ткани", "трикотаж", "текстиль", "материал", "сырьё", "сырье",
            "одежда", "обувь", "куртка", "пальто", "пуховик", "рубашка", "блузка", "футболка",
            "брюки", "джинсы", "юбка", "кроссовки", "ботинки", "туфли", "сапоги",
            "электроника", "радио", "лампа", "светодиод",
            "мебель", "стул", "стол", "диван", "кровать", "шкаф",
            "продукт", "кофе", "чай", "шоколад", "конфеты", "сок", "вино",
            "косметика", "парфюм", "игрушка", "велосипед", "самокат",
            # Логистика (основа слова для всех падежей)
            "груз", "перевозк", "фрахт", "доставк", "контейнер", "партия",
            "карго", "логистик", "маршрут", "инвойс", "упаковк",
            # Бизнес-процессы ВЭД
            "контракт", "поставщик", "партнёр", "партнер", "клиент",
            "импорт", "экспорт", "закупк", "заказ", "сделка",
            "документ", "сертификат", "декларац", "разрешени",
            "счёт", "счет", "платёж", "платеж", "оплат",
            # Расчётные
            "расчёт", "посчитай", "сколько будет", "сколько плат", "узнать плат",
            "ндс", "сбор", "страховк", "платеж",
            # Общие ВЭД-контексты
            "китай", "китайск", "турци", "оаэ", "индия", "вьетнам", "европ",
        )
        # Если НЕТ ВЭД-интента — пропускаем весь блок поиска
        has_ved_intent = any(kw in text_lower for kw in VED_INTENT_KEYWORDS)
        if not has_ved_intent:
            # Переходим сразу к AI-ассистенту (ниже по коду)
            pass  # будет обработан в сценарии 3
        else:
            # --- МАППИНГ ключевых слов → коды разделов ТН ВЭД ---
            # Позволяет найти товар по названию материала
            MATERIAL_MAP = {
            # === ХЛОПОК (5208) ===
            "хлопок": "5208", "хлопка": "5208", "хлопку": "5208", "хлопком": "5208",
            "хлопковый": "5208", "хлопковая": "5208", "хлопковое": "5208", "хлопковые": "5208",
            "хлопковой": "5208", "хлопкового": "5208", "хлопковому": "5208",
            "хлопковых": "5208", "хлопковым": "5208", "хлопковыми": "5208",
            "хлопчатобумажный": "5208", "хлопчатобумажная": "5208",
            "хлопчатобумажное": "5208", "хлопчатобумажные": "5208",
            "хлопчатка": "5208", "хлопчатобумаж": "5208",
            "cotton": "5208",
            # === ШЕРСТЬ (5105) ===
            "шерсть": "5105", "шерсти": "5105", "шерстью": "5105",
            "шерстяной": "5105", "шерстяная": "5105", "шерстяное": "5105",
            "шерстяные": "5105", "шерстяного": "5105", "шерстяной": "5105",
            "шерстяным": "5105", "шерстяных": "5105",
            "wool": "5105",
            # === ШЁЛК (5007) ===
            "шёлк": "5007", "шелк": "5007", "шёлка": "5007", "шелка": "5007",
            "шёлку": "5007", "шёлком": "5007",
            "шелковый": "5007", "шелковая": "5007", "шелковое": "5007",
            "шелковые": "5007", "шелковой": "5007", "шелковых": "5007",
            "шелковым": "5007", "шелковыми": "5007",
            "silk": "5007",
            # === ЛЁН (5309) ===
            "лён": "5309", "лен": "5309", "льна": "5309",
            "льняной": "5309", "льняная": "5309", "льняное": "5309",
            "льняные": "5309", "льняного": "5309", "льняной": "5309",
            "flax": "5309",
            # === СИНТЕТИКА (5407 / 5501) ===
            "синтетика": "5407", "синтетики": "5407", "синтетикой": "5407",
            "синтетический": "5407", "синтетическая": "5407", "синтетическое": "5407",
            "синтетические": "5407", "синтетического": "5407",
            "полиэстер": "5407", "полиэстра": "5407", "полиэстера": "5407",
            "полиэфир": "5407",
            "polyester": "5407",
            "акрил": "5501", "акриловый": "5501", "акриловая": "5501",
            # === КОЖА (4202) ===
            "кожа": "4202", "кожи": "4202", "кожей": "4202", "кожу": "4202",
            "кожаный": "4202", "кожаная": "4202", "кожаное": "4202",
            "кожаные": "4202", "кожаной": "4202", "кожаных": "4202",
            "кожаным": "4202",
            "leather": "4202",
            # === МЕТАЛЛЫ ===
            "сталь": "7326", "стали": "7326", "стальная": "7326", "стальной": "7326",
            "нержавейка": "7326", "нержавеющая": "7326", "нержавеющей": "7326",
            "алюминий": "7602", "алюминия": "7602", "алюминиевый": "7602",
            "медь": "7409", "медная": "7409", "медный": "7409", "меди": "7409",
            "латунь": "7409", "латунная": "7409", "латуни": "7409",
            "цинк": "7901", "цинковый": "7901",
            # === ЭЛЕКТРОНИКА ===
            "телефон": "8517", "телефона": "8517", "телефонов": "8517",
            "смартфон": "8517", "смартфона": "8517",
            "iphone": "8517", "айфон": "8517",
            "ноутбук": "8471", "ноутбука": "8471",
            "компьютер": "8471", "компьютера": "8471", "компьютерный": "8471",
            "планшет": "8471", "планшета": "8471",
            "монитор": "8528", "монитора": "8528",
            "телевизор": "8528", "телевизора": "8528",
            # === ОДЕЖДА ===
            "куртка": "6201", "куртки": "6201", "куртку": "6201",
            "пальто": "6201",
            "пуховик": "6201", "пуховика": "6201",
            "рубашка": "6205", "рубашки": "6205", "рубашку": "6205",
            "блузка": "6206", "блузки": "6206", "блузку": "6206",
            "футболка": "6109", "футболки": "6109", "футболку": "6109",
            "брюки": "6203", "брюк": "6203",
            "джинсы": "6204", "джинсов": "6204",
            "юбка": "6204", "юбки": "6204", "юбку": "6204",
            # === ОБУВЬ ===
            "обувь": "6403", "обуви": "6403", "обувью": "6403",
            "кроссовки": "6404", "кроссовок": "6404",
            "ботинки": "6403", "ботинок": "6403",
            "туфли": "6403", "туфель": "6403",
            "сапоги": "6403", "сапог": "6403",
            # === ПРОДУКТЫ ===
            "кофе": "0901", "чай": "0902", "чая": "0902",
            "шоколад": "1806", "шоколада": "1806",
            "конфеты": "1704", "конфет": "1704",
            "сок": "2009", "сока": "2009",
            "вино": "2204", "вина": "2204",
            # === МЕБЕЛЬ ===
            "стул": "9403", "стула": "9403", "стулья": "9403",
            "стол": "9403", "стола": "9403",
            "диван": "9401", "дивана": "9401",
            "кровать": "9403", "кровати": "9403",
            "шкаф": "9403", "шкафа": "9403",
            # === ПРОЧЕЕ ===
            "игрушка": "9503", "игрушки": "9503",
            "велосипед": "8712", "велосипеда": "8712",
            "самокат": "8712", "самоката": "8712",
            "косметика": "3304", "косметики": "3304",
            "парфюм": "3303",
            "зубная": "3306",
            "лампа": "8539", "лампы": "8539",
            "светодиод": "8539", "светодиода": "8539",
            "led": "8539",
            # === ОБЩИЕ ===
            "ткань": "5208", "ткани": "5208", "тканей": "5208",
            "тканью": "5208", "тканям": "5208", "тканями": "5208",
            "ткань": "5208",
            "трикотаж": "6004", "трикотажа": "6004",
        }
        
        # Извлекаем ключевые слова (4+ букв)
        keywords = re.findall(r'[а-яёa-z]{4,}', text_lower)
        keywords = [w for w in keywords if w not in (
            # Служебные и вежливые
            "подбери", "какой", "код", "товар", "груз", "штука", "кг", "вес",
            "цена", "стоимость", "сумма", "рубль", "доллар", "евро", "юань",
            "нужен", "расчёт", "помоги", "пожалуйста", "привет", "скажи",
            "будь", "добрый", "можно", "сколько", "стоить", "будет",
            "прошу", "дай", "выдай", "покажи", "нужно", "надо", "делать",
            # Предлоги и союзы
            "из", "для", "под", "при", "про", "без", "над", "через", "перед",
            "после", "между", "около", "возле", "пока", "если", "когда",
            # Местоимения и указатели
            "такой", "этот", "также", "очень", "только", "чтобы", "который",
            "которая", "которые", "которых", "здесь", "там", "тут", "где",
            # Единицы измерения
            "штук", "палет", "короб", "мест", "сантиметр", "сантиметров",
            "плотност", "ширина", "длина", "высота", "размер",
            "процент", "масса", "грамм", "метр", "сантиметр", "миллиметр",
            # "Бытовые" слова — не материалы и не товары
            "заявка", "заявки", "заявку", "заявок",
            "пришла", "пришло", "пришёл", "пришли", "приходить",
            "почта", "почту", "почтой", "почте", "письмо", "письма", "email",
            "новая", "новый", "новое", "новые",
            "сделать", "сделал", "делаю", "делать", "делаешь",
            "работа", "работу", "работы", "работе", "задача", "задачу",
            "встреча", "встречу", "звонок", "звоню", "звонить",
            "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
            "сегодня", "вчера", "завтра", "утром", "днём", "вечером",
            "утро", "вечер", "день", "неделя", "месяц", "год",
            "пока", "потом", "сейчас", "сразу", "позже", "раньше",
            "хочу", "хотел", "нужен", "надо", "надобно", "необходимо",
            "мне", "тебе", "нам", "вам", "ему", "ей", "им",
            "я", "ты", "он", "она", "оно", "мы", "вы", "они",
            "быть", "есть", "нет", "да", "нету",
        )]
        seen = set()
        keywords = [w for w in keywords if not (w in seen or seen.add(w))]
        
        # --- ЛЕММАТИЗАЦИЯ: приводим ключевые слова к базовой форме ---
        # Чтобы "хлопковой" → "хлопок", "ткани" → "ткань"
        def _lemmatize_russian(word: str) -> str:
            """Простая лемматизация: отрезаем типичные русские окончания."""
            suffixes = (
                # Прилагательные жен.род
                "овой", "овая", "овое", "овые", "овый", "ового", "овому",
                "евой", "евая", "евое", "евые", "евый",
                "ной", "ная", "ное", "ные", "ный", "ного", "ному",
                "еной", "еная", "еное", "еные",
                # Существительные
                "ов", "ев", "ей", "ям", "ях", "ами", "ой", "ий",
                "ие", "ии", "иям", "иях", "иями",
                # Глаголы/причастия
                "ешь", "ете", "ут", "ют", "ить", "ать", "ять",
            )
            w = word.lower()
            for suffix in sorted(suffixes, key=len, reverse=True):
                if w.endswith(suffix) and len(w) > len(suffix) + 2:
                    return w[:-len(suffix)]
            return w
        
        # --- Шаг 1: Поиск по маппингу материалов (с лемматизацией) ---
        matched_sections = set()
        lemmatized_hits = []  # какие базовые слова нашлись
        for kw in keywords:
            # Прямое совпадение
            if kw in MATERIAL_MAP:
                matched_sections.add(MATERIAL_MAP[kw])
                lemmatized_hits.append(kw)
            else:
                # Пробуем лемматизировать
                lemma = _lemmatize_russian(kw)
                if lemma in MATERIAL_MAP:
                    matched_sections.add(MATERIAL_MAP[lemma])
                    lemmatized_hits.append(f"{kw}→{lemma}")
                # Пробуем проверить начало слова (хлопковой → хлоп)
                elif len(kw) >= 5:
                    for base_key, section in MATERIAL_MAP.items():
                        if len(base_key) >= 4 and kw.startswith(base_key[:4]):
                            matched_sections.add(section)
                            lemmatized_hits.append(f"{kw}~{base_key}")
                            break
        
        all_results = []
        if matched_sections:
            # Ищем коды в matched разделах
            import sqlite3
            from config import DB_PATH
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            for section in matched_sections:
                c.execute(
                    "SELECT code, name, tariff FROM tnved_cache WHERE code LIKE ? AND name IS NOT NULL AND name != '' LIMIT 20",
                    (f"{section}%",)
                )
                for row in c.fetchall():
                    # Фильтруем мусор: код должен соответствовать разделу
                    code_prefix = row[0][:4] if len(row[0]) >= 4 else row[0]
                    section_prefix = section[:4]
                    if code_prefix == section_prefix:
                        all_results.append({
                            "code": row[0], "name": row[1], "tariff": row[2],
                            "section": section,
                        })
            conn.close()
        
        # --- ВСЕГДА Используем DeepSeek с уточняющими вопросами ---
        # (вместо прямого вывода списка кодов из БД)
        context_parts = [
            f"[КОНТЕКСТ: запрос на подбор кода ТН ВЭД]",
            f"Запрос пользователя: {user_text}",
        ]
        
        if lemmatized_hits:
            context_parts.append(f"Распознанные материалы: {', '.join(lemmatized_hits)}")
        
        if all_results:
            context_parts.append(f"\nНайденные варианты кодов из БД:")
            # Убираем дубликаты по коду
            seen_codes = set()
            unique_results = []
            for r in all_results[:8]:
                if r["code"] not in seen_codes:
                    seen_codes.add(r["code"])
                    unique_results.append(r)
            for r in unique_results[:5]:
                name = (r["name"] or "—").replace("🠺", "→").strip()
                context_parts.append(f"  {r['code']} | {name[:120]} | {r['tariff']}")
        else:
            context_parts.append("\nВ БД нет точных совпадений по материалу.")
        
        context_parts.append(
            "\n\n=== ИНСТРУКЦИЯ ДЛЯ ОТВЕТА ===\n"
            "Ты — эксперт West Asia по подбору кодов ТН ВЭД.\n"
            "1. Начни с краткого анализа: какая ГРУППА ТН ВЭД (первые 4 цифры) наиболее вероятна.\n"
            "2. Задай 2-4 УТОЧНЯЮЩИХ ВОПРОСА — без ответов на них невозможно точно определить код:\n"
            "   - Для тканей: плотность (г/м²), переплетение (саржа, полотняное), состав (%), отделка, ширина\n"
            "   - Для электроники: назначение, технические характеристики\n"
            "   - Для одежды: пол, возраст, материал, способ изготовления\n"
            "   - Для металлов: сплав, форма, обработка\n"
            "   - Общие: страна происхождения, назначение, технические параметры\n"
            "3. Если есть похожие группы — укажи альтернативы с кратким пояснением.\n"
            "4. НЕ пиши курс ЦБ. НЕ давай финальный расчёт.\n"
            "5. В конце: '📌 Точный код подтвердите у декларанта или через предварительное решение ФТС.'\n"
            "6. Формат: кратко, структурировано, с эмодзи."
        )
        
        extra = "\n".join(context_parts)
        msgs = build_messages(user_id, user_text, extra_context=extra)
        answer = await ask_deepseek(msgs)
        answer = _strip_ai_assistant_junk(answer)
        
        await safe_send(message, answer)
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

        # Удаляем коды ТН ВЭД перед парсингом сумм, чтобы код не стал инвойсом
        text_clean_for_ts = user_text
        for c in codes:
            text_clean_for_ts = text_clean_for_ts.replace(c, "")
            # Удаляем и раздельный вариант (5208 43 000 0)
            spaced = " ".join(c[i:i+2] for i in range(0, len(c), 2))
            text_clean_for_ts = text_clean_for_ts.replace(spaced, "")
        
        comps = extract_ts_components_with_currency(text_clean_for_ts)
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
        
        # Лёгкая очистка — только markdown-таблицы и курс ЦБ если придуман
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
