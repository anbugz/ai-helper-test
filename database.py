"""
database.py — все операции с SQLite.
Импортирует только config (DB_PATH, logger, VERSION).
"""
import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from io import BytesIO
from config import DB_PATH, logger, VERSION

# ------------------------------------------------------------------
# Инициализация
# ------------------------------------------------------------------

def init_db() -> None:
    """Создаёт директорию и таблицы, если их нет."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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
        (user_id, username, role, content, datetime.utcnow().isoformat()),
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

def get_dialogs_for_export(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = "SELECT user_id, username, role, content, created_at FROM dialogs WHERE 1=1"
    params = []
    if date_from:
        query += " AND created_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND created_at <= ?"
        params.append(date_to)
    query += " ORDER BY created_at"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

# ------------------------------------------------------------------
# Исправления / замечания
# ------------------------------------------------------------------

def save_correction(user_id: int, username: str, original: str, correction: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO corrections (user_id, username, original, correction, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, original, correction, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Кастомные коды радиоэлектроники
# ------------------------------------------------------------------

def save_custom_codes(codes: List[str]) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for code in codes:
        c.execute(
            "INSERT OR IGNORE INTO custom_radio_codes (code, added_at) VALUES (?, ?)",
            (code, datetime.utcnow().isoformat()),
        )
    conn.commit()
    conn.close()

def get_custom_codes() -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code FROM custom_radio_codes")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

# ------------------------------------------------------------------
# База знаний (/learn)
# ------------------------------------------------------------------

def save_knowledge(topic: str, content: str, questions: str, added_by: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO knowledge_base (topic, content, questions, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (topic, content, questions, added_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_knowledge() -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions FROM knowledge_base ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"topic": r[0], "content": r[1], "questions": r[2]} for r in rows]

def get_knowledge_by_topic(topic: str) -> Optional[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions FROM knowledge_base WHERE topic = ?", (topic,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"topic": row[0], "content": row[1], "questions": row[2]}
    return None

# ------------------------------------------------------------------
# Настройки
# ------------------------------------------------------------------

def get_settings(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ------------------------------------------------------------------
# Кэш ТН ВЭД (переживает редеплой)
# ------------------------------------------------------------------

def save_tnved_batch(rows: List[List[str]], parsed_rows: List[dict]) -> int:
    """Сохраняет распарсенные данные ТН ВЭД пачкой. Возвращает количество записей."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    count = 0
    now = datetime.utcnow().isoformat()
    for row, parsed in zip(rows, parsed_rows):
        if not row or not isinstance(row[0], str):
            continue
        code = row[0].replace(" ", "").strip()
        if len(code) < 6 or not code.isdigit():
            continue
        name = row[1] if len(row) > 1 else ""
        tariff = row[2] if len(row) > 2 else ""
        c.execute(
            """INSERT INTO tnved_cache (code, name, tariff, parsed_type, parsed_formula, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name,
                 tariff=excluded.tariff,
                 parsed_type=excluded.parsed_type,
                 parsed_formula=excluded.parsed_formula,
                 loaded_at=excluded.loaded_at""",
            (code, name, tariff, parsed.get("type", ""), parsed.get("formula", ""), now),
        )
        count += 1
    conn.commit()
    conn.close()
    logger.info(f"TNVED кэш: сохранено {count} кодов в БД")
    return count


def get_tnved_from_db(code: str) -> Optional[dict]:
    """Ищет код ТН ВЭД в SQLite."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    search = code.replace(" ", "").replace(".", "").strip()
    c.execute(
        "SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code = ?",
        (search,),
    )
    row = c.fetchone()
    if not row and len(search) >= 6:
        c.execute(
            "SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code LIKE ? LIMIT 1",
            (f"{search}%",),
        )
        row = c.fetchone()
    conn.close()
    if row:
        return {
            "code": row[0], "name": row[1], "tariff": row[2],
            "parsed_tariff": {"type": row[3], "formula": row[4]},
            "has_euro_component": row[3] in ("min", "plus", "fixed_eur"),
            "needs_weight": row[3] in ("min", "plus", "fixed_eur"),
        }
    return None


def get_all_tnved_from_db() -> List[List[str]]:
    """Загружает весь кэш ТН ВЭД из SQLite. Для восстановления после редеплоя."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, name, tariff FROM tnved_cache ORDER BY code")
    rows = [[r[0], r[1], r[2]] for r in c.fetchall()]
    conn.close()
    logger.info(f"TNVED кэш: загружено {len(rows)} кодов из БД")
    return rows


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


def update_settings(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Экспорт логов в XLSX (через zipfile + xml, без openpyxl)
# ------------------------------------------------------------------

def create_logs_xlsx(rows: List[Tuple], sheet_name: str = "logs") -> bytes:
    """Генерирует .xlsx из списка кортежей через zipfile + xml."""
    import zipfile
    import xml.etree.ElementTree as ET

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    root = ET.Element(f"{{{ns}}}worksheet")
    sheet_data = ET.SubElement(root, f"{{{ns}}}sheetData")

    for r_idx, row in enumerate(rows, 1):
        row_elem = ET.SubElement(sheet_data, f"{{{ns}}}row", {f"{{{ns}}}r": str(r_idx)})
        for c_idx, value in enumerate(row):
            cell_ref = f"{chr(65 + c_idx)}{r_idx}"
            cell = ET.SubElement(row_elem, f"{{{ns}}}c", {f"{{{ns}}}r": cell_ref})
            v = ET.SubElement(cell, f"{{{ns}}}v")
            v.text = str(value) if value is not None else ""

    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=True)
    xml_bytes = xml_bytes.replace("ns0:", "").replace("xmlns:ns0=", "xmlns=")
    xml_bytes = xml_bytes.encode("utf-8")

    xlsx_buffer = BytesIO()
    with zipfile.ZipFile(xlsx_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", xml_bytes)
        zf.writestr("[Content_Types].xml", b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')

    return xlsx_buffer.getvalue()
