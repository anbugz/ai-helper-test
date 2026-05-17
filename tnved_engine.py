"""
tnved_engine.py — кэш данных ТН ВЭД, поиск, проверка радиоэлектроники.
Импортирует parsers (parse_tnved_tariff), database (SQLite-кэш) и config.
"""
import re
from typing import List, Dict, Optional, Set
from parsers import parse_tnved_tariff
from database import save_tnved_batch, get_tnved_from_db, get_all_tnved_from_db
from config import RADIO_ELECTRONICS_CODES_SET, _RADIO_GROUPS, TNVED_FULL_NAMES, logger

# ------------------------------------------------------------------
# Кэш (в памяти, не в SQLite)
# ------------------------------------------------------------------
_TNVED_ROWS_CACHE: List[List[str]] = []
_TNVED_INDEX: Dict[str, int] = {}  # code -> row_index


def _build_tnved_index(rows: List[List[str]]) -> None:
    """Строит индекс для быстрого поиска кодов ТН ВЭД."""
    global _TNVED_INDEX
    _TNVED_INDEX = {}
    for i, row in enumerate(rows):
        if not row or not isinstance(row[0], str):
            continue
        code = row[0].replace(" ", "").strip()
        if len(code) >= 6 and code.isdigit():
            _TNVED_INDEX[code] = i


def load_tnved_rows(rows: List[List[str]], persist: bool = True) -> None:
    """Загружает строки ТН ВЭД в кэш, строит индекс, сохраняет в SQLite, заполняет TNVED_FULL_NAMES."""
    global _TNVED_ROWS_CACHE
    _TNVED_ROWS_CACHE = [r for r in rows if r and any(str(c).strip() for c in r)]
    _build_tnved_index(_TNVED_ROWS_CACHE)
    # Заполняем полные наименования из загруженных данных
    TNVED_FULL_NAMES.clear()
    for row in _TNVED_ROWS_CACHE:
        if not row or not isinstance(row[0], str):
            continue
        code = row[0].replace(" ", "").strip()
        name = row[1] if len(row) > 1 else ""
        if len(code) >= 6 and code.isdigit() and name:
            prefix = code[:6]
            # Сохраняем самое длинное наименование для префикса
            if prefix not in TNVED_FULL_NAMES or len(name) > len(TNVED_FULL_NAMES[prefix]):
                TNVED_FULL_NAMES[prefix] = name
    logger.info(f"TNVED кэш: {len(_TNVED_ROWS_CACHE)} строк, {len(_TNVED_INDEX)} кодов, "
                f"{len(TNVED_FULL_NAMES)} полных наименований")
    if persist:
        parsed_rows = [
            parse_tnved_tariff(row[2] if len(row) > 2 else "") for row in _TNVED_ROWS_CACHE
        ]
        save_tnved_batch(_TNVED_ROWS_CACHE, parsed_rows)


def restore_tnved_from_db() -> bool:
    """Восстанавливает кэш ТН ВЭД из SQLite при старте бота, включая полные наименования."""
    global _TNVED_ROWS_CACHE
    rows = get_all_tnved_from_db()
    if not rows:
        logger.info("TNVED кэш в БД пуст — жду загрузки .xlsx")
        return False
    _TNVED_ROWS_CACHE = rows
    _build_tnved_index(_TNVED_ROWS_CACHE)
    # Восстанавливаем полные наименования
    TNVED_FULL_NAMES.clear()
    for row in rows:
        if not row or not isinstance(row[0], str):
            continue
        code = row[0].replace(" ", "").strip()
        name = row[1] if len(row) > 1 else ""
        if len(code) >= 6 and code.isdigit() and name:
            prefix = code[:6]
            if prefix not in TNVED_FULL_NAMES or len(name) > len(TNVED_FULL_NAMES[prefix]):
                TNVED_FULL_NAMES[prefix] = name
    logger.info(f"TNVED кэш восстановлен из БД: {len(_TNVED_ROWS_CACHE)} строк, "
                f"{len(TNVED_FULL_NAMES)} полных наименований")
    return True


def get_tnved_from_cache(code: str) -> Optional[dict]:
    """Быстрый поиск кода ТН ВЭД: сначала память (O(1)), потом SQLite."""
    if not code:
        return None
    if _TNVED_INDEX:
        search_code = code.replace(" ", "").replace(".", "").strip()
        idx = _TNVED_INDEX.get(search_code)
        if idx is not None and idx < len(_TNVED_ROWS_CACHE):
            return _row_to_tnved_dict(_TNVED_ROWS_CACHE[idx])
        if len(search_code) >= 6:
            for full_code, i in _TNVED_INDEX.items():
                if full_code.startswith(search_code) and i < len(_TNVED_ROWS_CACHE):
                    return _row_to_tnved_dict(_TNVED_ROWS_CACHE[i])
    return get_tnved_from_db(code)


def _row_to_tnved_dict(row: List[str]) -> dict:
    """Преобразует строку Excel в словарь с данными ТН ВЭД."""
    tariff = row[2] if len(row) > 2 else ""
    parsed = parse_tnved_tariff(tariff)
    return {
        "code": row[0] if row else "",
        "name": row[1] if len(row) > 1 else "",
        "tariff": tariff,
        "parsed_tariff": parsed,
        "has_euro_component": parsed["type"] in ("min", "plus", "fixed_eur"),
        "needs_weight": parsed["type"] in ("min", "plus", "fixed_eur"),
    }


# ------------------------------------------------------------------
# Радиоэлектроника
# ------------------------------------------------------------------

def is_radio_electronics(code: str) -> bool:
    """Проверяет по списку + по первым 2 цифрам группы.
    Для коротких шаблонов (≤6 цифр) — startswith (группы/подгруппы).
    Для длинных шаблонов (≥8 цифр) — точное совпадение (полные коды).
    """
    if not code:
        return False
    c = code.replace(" ", "").replace(".", "").strip()
    if len(c) >= 2 and c[:2] not in _RADIO_GROUPS:
        return False
    for pattern in RADIO_ELECTRONICS_CODES_SET:
        if len(pattern) <= 6:
            if c.startswith(pattern):
                return True
        else:
            if c == pattern:
                return True
    return False


# ------------------------------------------------------------------
# Извлечение кодов из текста
# ------------------------------------------------------------------

def extract_tnved_codes(text: str) -> List[str]:
    """Извлекает коды ТН ВЭД (8-10 цифр) из текста."""
    return re.findall(r"\d{8,10}", text)


# ------------------------------------------------------------------
# Таможенный сбор по шкале
# ------------------------------------------------------------------

def calculate_customs_fee(value_rub: float) -> int:
    from config import CUSTOMS_FEE_RUB, RADIO_FEE

    for threshold, fee in sorted(CUSTOMS_FEE_RUB.items()):
        if value_rub <= threshold:
            return fee
    return RADIO_FEE
