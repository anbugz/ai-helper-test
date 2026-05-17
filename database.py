"""
database.py — все операции с SQLite.
Импортирует только config (DB_PATH, logger, VERSION).
"""
import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from io import BytesIO
from config import DB_PATH, logger


# ------------------------------------------------------------------
# Инициализация
# ------------------------------------------------------------------

def init_db() -> None:
    """Создаёт директорию и таблицы, если их нет."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    logger.info(f"[DB] Using database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            role TEXT,
            content TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            original TEXT,
            correction TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS custom_radio_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS knowledge_base (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            content TEXT,
            questions TEXT,
            added_by TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tnved_cache (
            code TEXT PRIMARY KEY,
            name TEXT,
            tariff TEXT,
            parsed_type TEXT,
            parsed_formula TEXT,
            loaded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tnved_name ON tnved_cache(name);
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована.")


# ------------------------------------------------------------------
# Диалоги
# ------------------------------------------------------------------

def save_message(user_id: int, username: str, role: str, content: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO dialogs (user_id, username, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, role, content, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_dialog_history(user_id: int, limit: int = 20) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM dialogs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def clear_history(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM dialogs WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_dialogs_for_export(
    date_from: Optional[str] = None, date_to: Optional[str] = None
) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = "SELECT user_id, username, role, content, created_at FROM dialogs WHERE 1=1"
    params = []
    if date_from:
        query += " AND created_at >= ?"
        params.append(f"{date_from} 00:00:00")
    if date_to:
        query += " AND created_at <= ?"
        params.append(f"{date_to} 23:59:59")
    query += " ORDER BY created_at"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------
# Исправления / замечания
# ------------------------------------------------------------------

def save_correction(
    user_id: int, username: str, original: str, correction: str
) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO corrections (user_id, username, original, correction, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, original, correction, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_all_corrections() -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, username, original, correction, created_at FROM corrections ORDER BY created_at DESC"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_unnotified_corrections() -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, original, correction, created_at FROM corrections WHERE notified = 0 ORDER BY created_at"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def mark_corrections_notified(ids: List[int]) -> None:
    if not ids:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    placeholders = ",".join("?" * len(ids))
    c.execute(
        f"UPDATE corrections SET notified = 1 WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# База знаний
# ------------------------------------------------------------------

def save_knowledge(topic: str, content: str, questions: str, added_by: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO knowledge_base (topic, content, questions, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (topic, content, questions, added_by, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_all_knowledge() -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions, added_by, created_at FROM knowledge_base")
    rows = c.fetchall()
    conn.close()
    return [
        {
            "topic": r[0],
            "content": r[1],
            "questions": r[2],
            "added_by": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


# Alias для совместимости с handlers.py
get_knowledge = get_all_knowledge


def get_knowledge_by_topic(topic: str) -> Optional[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions, added_by, created_at FROM knowledge_base WHERE topic = ?", (topic,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "topic": row[0],
        "content": row[1],
        "questions": row[2],
        "added_by": row[3],
        "created_at": row[4],
    }


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

def get_setting(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# Радиоэлектроника — кастомные коды
# ------------------------------------------------------------------

def add_custom_radio_code(code: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO custom_radio_codes (code, added_at) VALUES (?, ?)",
            (code, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


def save_custom_codes(codes: List[str]) -> int:
    """Сохраняет список кодов радиоэлектроники в таблицу custom_radio_codes.
    Возвращает количество добавленных кодов.
    """
    if not codes:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0
    for code in codes:
        try:
            c.execute(
                "INSERT OR IGNORE INTO custom_radio_codes (code, added_at) VALUES (?, ?)",
                (code, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            )
            if c.rowcount > 0:
                added += 1
        except sqlite3.Error:
            continue
    conn.commit()
    conn.close()
    logger.info(f"Сохранено {added} новых кодов радиоэлектроники")
    return added


def get_all_custom_radio_codes() -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code FROM custom_radio_codes")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


# ------------------------------------------------------------------
# ТН ВЭД — кэш
# ------------------------------------------------------------------

def save_tnved_batch(raw_rows: List, parsed_rows: List) -> None:
    """Сохраняет или обновляет коды ТН ВЭД в SQLite-кэше."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for raw, parsed in zip(raw_rows, parsed_rows):
        code = raw[0] if raw else ""
        name = raw[1] if len(raw) > 1 else ""
        tariff = raw[2] if len(raw) > 2 else ""
        code_clean = code.replace(" ", "")
        c.execute(
            """
            INSERT OR REPLACE INTO tnved_cache (code, name, tariff, parsed_type, parsed_formula, loaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code_clean,
                name,
                tariff,
                parsed.get("type", ""),
                parsed.get("formula", ""),
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    conn.commit()
    conn.close()
    logger.info(f"TNVED: сохранено {len(raw_rows)} кодов в БД")


def get_tnved_from_db(code: str) -> Optional[Dict]:
    """Получает код ТН ВЭД из SQLite-кэша."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "code": row[0],
        "name": row[1],
        "tariff": row[2],
        "parsed_tariff": {"type": row[3], "formula": row[4]},
    }


def search_tnved_in_db(query: str) -> List[Dict]:
    """Поиск по наименованию в SQLite-кэше."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE name LIKE ? LIMIT 10",
        (f"%{query}%",),
    )
    rows = c.fetchall()
    conn.close()
    return [
        {
            "code": r[0],
            "name": r[1],
            "tariff": r[2],
            "parsed_tariff": {"type": r[3], "formula": r[4]},
        }
        for r in rows
    ]


def get_all_tnved_from_db() -> List[List[str]]:
    """Получает ВСЕ коды из SQLite для восстановления кэша при старте."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, name, tariff FROM tnved_cache ORDER BY code")
    rows = c.fetchall()
    conn.close()
    logger.info(f"TNVED кэш: загружено {len(rows)} кодов из БД")
    return [[r[0], r[1], r[2]] for r in rows]


def clear_tnved_cache_db() -> int:
    """Очищает таблицу tnved_cache. Возвращает количество удалённых записей."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tnved_cache")
    count = c.fetchone()[0]
    c.execute("DELETE FROM tnved_cache")
    conn.commit()
    conn.close()
    logger.info(f"TNVED кэш: удалено {count} кодов из БД")
    return count


def get_tnved_stats() -> dict:
    """Статистика по кэшу ТН ВЭД в БД."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*), parsed_type FROM tnved_cache GROUP BY parsed_type")
    stats = dict(c.fetchall())
    c.execute("SELECT COUNT(DISTINCT code) FROM tnved_cache")
    total = c.fetchone()[0]
    conn.close()
    return {"total": total, "by_type": stats}


# ------------------------------------------------------------------
# Экспорт логов в XLSX (через zipfile + xml, без openpyxl)
# ------------------------------------------------------------------

def _col_letter(idx: int) -> str:
    """Преобразует индекс колонки (0-based) в буквенное обозначение (A, B, ..., Z, AA, ...)."""
    result = ""
    while idx >= 0:
        result = chr(65 + (idx % 26)) + result
        idx = idx // 26 - 1
    return result


def _escape_xml(text: str) -> str:
    """Экранирует XML-спецсимволы и переводы строк."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    text = text.replace("\r\n", "&#10;").replace("\n", "&#10;").replace("\r", "&#10;")
    return text


def create_logs_xlsx(rows: List[Tuple], sheet_name: str = "logs") -> bytes:
    """Генерирует минимально валидный .xlsx через zipfile + ручной XML.
    Совместимость: Excel, LibreOffice, Google Sheets.
    """
    import zipfile

    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"

    # --- workbook.xml ---
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        '  <sheets>\n'
        f'    <sheet name="{_escape_xml(sheet_name)}" sheetId="1" r:id="rId1"/>\n'
        '  </sheets>\n'
        '  <calcPr calcId="124519" fullCalcOnLoad="1"/>\n'
        '</workbook>'
    ).encode("utf-8")

    # --- worksheet.xml ---
    max_col = 4  # A-E (0-4)
    max_row = len(rows) + 1
    ws_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n',
        f'  <dimension ref="A1:{_col_letter(max_col)}{max_row}"/>\n',
        '  <sheetData>\n',
    ]
    # Заголовок
    ws_parts.append('    <row r="1">\n')
    headers = ["user_id", "username", "role", "content", "created_at"]
    for c_idx, h in enumerate(headers):
        cell_ref = f"{_col_letter(c_idx)}1"
        ws_parts.append(f'      <c r="{cell_ref}" t="inlineStr"><is><t>{_escape_xml(h)}</t></is></c>\n')
    ws_parts.append('    </row>\n')
    # Данные — все ячейки как inlineStr (текст)
    for r_idx, row in enumerate(rows, 2):
        ws_parts.append(f'    <row r="{r_idx}">\n')
        for c_idx, value in enumerate(row):
            cell_ref = f"{_col_letter(c_idx)}{r_idx}"
            safe_val = str(value) if value is not None else ""
            ws_parts.append(f'      <c r="{cell_ref}" t="inlineStr"><is><t>{_escape_xml(safe_val)}</t></is></c>\n')
        ws_parts.append('    </row>\n')
    ws_parts.append('  </sheetData>\n')
    ws_parts.append('  <sheetViews><sheetView tabSelected="1" workbookViewId="0"/></sheetViews>\n')
    ws_parts.append('</worksheet>')
    ws_xml = "".join(ws_parts).encode("utf-8")

    # --- relationships ---
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>\n'
        '</Relationships>'
    ).encode("utf-8")

    # --- [Content_Types].xml ---
    ct_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{ct_ns}">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
        '  <Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
        '</Types>'
    ).encode("utf-8")

    # --- .rels ---
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>\n'
        '</Relationships>'
    ).encode("utf-8")

    # --- сборка zip ---
    xlsx_buffer = BytesIO()
    with zipfile.ZipFile(xlsx_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/worksheets/sheet1.xml", ws_xml)

    return xlsx_buffer.getvalue()
