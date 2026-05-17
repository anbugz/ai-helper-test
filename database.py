"""
database.py — SQLite операции. БЕЗ full_name. Простая структура.
Таблицы: dialogs, corrections, custom_radio_codes, settings, knowledge_base, tnved_cache.
"""
import sqlite3
import re
import os
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional, Any
from io import BytesIO

from config import DB_PATH, logger, SYSTEM_PROMPT

# ------------------------------------------------------------------
# Инициализация
# ------------------------------------------------------------------

def init_db() -> None:
    """Создаёт все таблицы если их нет."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            role TEXT,
            content TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_dialogs_user ON dialogs(user_id);
        CREATE INDEX IF NOT EXISTS idx_dialogs_time ON dialogs(created_at);

        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            original TEXT,
            correction TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS custom_radio_codes (
            code TEXT PRIMARY KEY,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS knowledge_base (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT UNIQUE,
            content TEXT,
            questions TEXT,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        (user_id, username, role, content, (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def get_dialog_history(user_id: int, limit: int = 20) -> List[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM dialogs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def clear_history(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM dialogs WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Коррекции / "несогласен"
# ------------------------------------------------------------------

def save_correction(user_id: int, username: str, original: str, correction: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO corrections (user_id, username, original, correction, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, original, correction, (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Коды радиоэлектроники
# ------------------------------------------------------------------

def save_custom_codes(codes: List[str], added_by: str = "admin") -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for code in codes:
        c.execute(
            "INSERT OR REPLACE INTO custom_radio_codes (code, added_by) VALUES (?, ?)",
            (code, added_by)
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
        "INSERT OR REPLACE INTO knowledge_base (topic, content, questions, added_by) VALUES (?, ?, ?, ?)",
        (topic, content, questions, added_by)
    )
    conn.commit()
    conn.close()


def get_knowledge() -> List[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions FROM knowledge_base")
    rows = c.fetchall()
    conn.close()
    return [{"topic": r[0], "content": r[1], "questions": r[2]} for r in rows]


def get_knowledge_by_topic(topic: str) -> Optional[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT topic, content, questions FROM knowledge_base WHERE topic = ?", (topic,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"topic": row[0], "content": row[1], "questions": row[2]}
    return None

# ------------------------------------------------------------------
# ТН ВЭД
# ------------------------------------------------------------------

def save_tnved_batch(rows: List[List], parsed_rows: List[Dict]) -> None:
    """Пакетное сохранение кодов ТН ВЭД."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    for row, parsed in zip(rows, parsed_rows):
        code = str(row[0]).replace(" ", "") if row else ""
        name = str(row[1]) if len(row) > 1 else ""
        tariff = str(row[2]) if len(row) > 2 else ""
        c.execute(
            "INSERT OR REPLACE INTO tnved_cache (code, name, tariff, parsed_type, parsed_formula, loaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, tariff, parsed.get("type", ""), parsed.get("formula", ""), now)
        )
    conn.commit()
    conn.close()
    logger.info(f"TNVED кэш: сохранено {len(rows)} кодов в БД")


def get_tnved_from_db(code: str) -> Optional[Dict]:
    """Поиск кода ТН ВЭД: точное совпадение → LIKE → 6-digit prefix."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Точное совпадение
    c.execute("SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code = ?", (code,))
    row = c.fetchone()
    if row:
        conn.close()
        return _make_tnved_dict(row)
    # LIKE полный код
    c.execute("SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code LIKE ?", (f"{code}%",))
    row = c.fetchone()
    if row:
        conn.close()
        return _make_tnved_dict(row)
    # 6-digit prefix
    prefix = code[:6]
    c.execute("SELECT code, name, tariff, parsed_type, parsed_formula FROM tnved_cache WHERE code LIKE ? LIMIT 1", (f"{prefix}%",))
    row = c.fetchone()
    conn.close()
    if row:
        return _make_tnved_dict(row)
    return None


def _make_tnved_dict(row: Tuple) -> Dict:
    from parsers import parse_tnved_tariff
    return {
        "code": row[0],
        "name": row[1] if row[1] else "",
        "tariff": row[2] if row[2] else "неизвестно",
        "parsed_tariff": parse_tnved_tariff(row[2]) if row[2] else {"type": "unknown", "formula": "?"},
    }


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

def get_settings(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def update_settings(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Логи / экспорт
# ------------------------------------------------------------------

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
    query += " ORDER BY created_at DESC LIMIT 10000"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows


def create_logs_xlsx(rows: List[Tuple], sheet_name: str = "logs") -> bytes:
    """Генерирует .xlsx через zipfile + xml. Полная структура — Excel открывает без ошибок."""
    import zipfile
    import xml.etree.ElementTree as ET

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    headers = ["user_id", "username", "role", "content", "created_at"]
    all_rows = [headers] + list(rows)

    # --- sheet1.xml ---
    root = ET.Element(f"{{{ns}}}worksheet")
    sd = ET.SubElement(root, f"{{{ns}}}sheetData")
    for r_idx, row in enumerate(all_rows, 1):
        row_elem = ET.SubElement(sd, f"{{{ns}}}row", {f"{{{ns}}}r": str(r_idx)})
        for c_idx, value in enumerate(row):
            cell_ref = f"{chr(65 + c_idx)}{r_idx}"
            cell = ET.SubElement(row_elem, f"{{{ns}}}c", {f"{{{ns}}}r": cell_ref, f"{{{ns}}}t": "inlineStr"})
            is_elem = ET.SubElement(cell, f"{{{ns}}}is")
            t_elem = ET.SubElement(is_elem, f"{{{ns}}}t")
            val = str(value) if value is not None else ""
            t_elem.text = val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    sheet_xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
    sheet_xml = sheet_xml.replace("ns0:", "").replace("xmlns:ns0=", "xmlns=")
    sheet_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + sheet_xml.split("?>", 1)[-1].strip()

    # --- workbook.xml ---
    wb_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    wb_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    wb = ET.Element(f"{{{wb_ns}}}workbook")
    sheets = ET.SubElement(wb, f"{{{wb_ns}}}sheets")
    ET.SubElement(sheets, f"{{{wb_ns}}}sheet", {
        f"{{{wb_ns}}}name": sheet_name,
        f"{{{wb_ns}}}sheetId": "1",
        f"{{{wb_rel}}}id": "rId1"
    })
    wb_xml = ET.tostring(wb, encoding="unicode", xml_declaration=True)
    wb_xml = wb_xml.replace("ns0:", "").replace("xmlns:ns0=", "xmlns=")
    wb_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + wb_xml.split("?>", 1)[-1].strip()

    # --- _rels/.rels ---
    rels_pkg = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' \
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' \
               '</Relationships>'

    # --- xl/_rels/workbook.xml.rels ---
    rels_wb = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' \
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' \
              '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>' \
              '</Relationships>'

    # --- sharedStrings.xml ---
    ss = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' \
         '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"/>'

    # --- [Content_Types].xml ---
    ct = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' \
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' \
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' \
         '<Default Extension="xml" ContentType="application/xml"/>' \
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' \
         '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' \
         '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>' \
         '</Types>'

    # --- Собираем zip ---
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct.encode("utf-8"))
        zf.writestr("_rels/.rels", rels_pkg.encode("utf-8"))
        zf.writestr("xl/workbook.xml", wb_xml.encode("utf-8"))
        zf.writestr("xl/_rels/workbook.xml.rels", rels_wb.encode("utf-8"))
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
        zf.writestr("xl/sharedStrings.xml", ss.encode("utf-8"))

    return buf.getvalue()
