"""
parsers.py — парсинг Excel/DOCX/TXT + парсинг тарифов ТН ВЭД.
Чистые функции, не импортирует другие модули проекта.
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import List, Dict, Optional, Union


def _find_all(root, tag_name: str):
    """Ищет теги без учёта namespace."""
    return [elem for elem in root.iter() if elem.tag.endswith(tag_name) or elem.tag == tag_name]


def _col_to_idx(col_str: str) -> int:
    """A=0, B=1, Z=25, AA=26."""
    idx = 0
    for ch in col_str.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def parse_xlsx(file_bytes: Union[bytes, BytesIO]) -> List[List[str]]:
    """Парсит .xlsx — ВСЕ листы, учитывает позиции ячеек (r), поддерживает inlineStr."""
    all_rows: List[List[str]] = []
    try:
        with zipfile.ZipFile(file_bytes) as zf:
            shared = {}
            if "xl/sharedStrings.xml" in zf.namelist():
                with zf.open("xl/sharedStrings.xml") as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    for idx, si in enumerate(_find_all(root, "si")):
                        texts = _find_all(si, "t")
                        shared[idx] = "".join(t.text or "" for t in texts)

            sheet_names = sorted([
                name for name in zf.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            ])

            for sheet_name in sheet_names:
                sheet_rows: List[List[str]] = []
                with zf.open(sheet_name) as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    for row_elem in _find_all(root, "row"):
                        row_r = row_elem.get("r", "")
                        try:
                            row_idx = int(row_r) - 1 if row_r else len(sheet_rows)
                        except ValueError:
                            row_idx = len(sheet_rows)

                        cells_dict: Dict[int, str] = {}
                        max_col = -1

                        for c_elem in _find_all(row_elem, "c"):
                            cell_r = c_elem.get("r", "")
                            col_str = "".join(ch for ch in cell_r if ch.isalpha())
                            col_idx = _col_to_idx(col_str) if col_str else 0

                            t = c_elem.get("t", "")
                            v = None
                            for child in _find_all(c_elem, "v"):
                                v = child
                                break
                            val = v.text if v is not None else ""

                            if t == "inlineStr":
                                is_elem = None
                                for child in _find_all(c_elem, "is"):
                                    is_elem = child
                                    break
                                if is_elem is not None:
                                    t_elems = _find_all(is_elem, "t")
                                    val = "".join(t.text or "" for t in t_elems)
                            elif t == "s" and val.isdigit():
                                val = shared.get(int(val), "")

                            cells_dict[col_idx] = val
                            if col_idx > max_col:
                                max_col = col_idx

                        if max_col >= 0:
                            row_data = [cells_dict.get(i, "") for i in range(max_col + 1)]
                        else:
                            row_data = []

                        while len(sheet_rows) <= row_idx:
                            sheet_rows.append([])
                        sheet_rows[row_idx] = row_data

                all_rows.extend(sheet_rows)
    except Exception as e:
        import traceback
        traceback.print_exc()
    return all_rows


def parse_docx(file_bytes: Union[bytes, BytesIO]) -> str:
    """Парсит .docx через document.xml."""
    text_parts = []
    try:
        with zipfile.ZipFile(file_bytes) as zf:
            with zf.open("word/document.xml") as f:
                tree = ET.parse(f)
                root = tree.getroot()
                for elem in root.iter():
                    if elem.tag.endswith("t") or elem.tag == "t":
                        if elem.text:
                            text_parts.append(elem.text)
    except Exception:
        pass
    return "\n".join(text_parts)


def parse_txt(file_bytes: Union[bytes, BytesIO]) -> str:
    """Парсит .txt в UTF-8 или cp1251."""
    raw = file_bytes.read() if hasattr(file_bytes, "read") else file_bytes
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_codes_from_rows(rows: List[List[str]]) -> List[str]:
    """Извлекает 6–10-значные коды ТН ВЭД из строк Excel."""
    codes = []
    for row in rows:
        for cell in row:
            if isinstance(cell, str):
                c = cell.replace(" ", "").replace(".", "").strip()
                if c.isdigit() and 6 <= len(c) <= 10:
                    codes.append(c)
    return codes


# ------------------------------------------------------------------
# Парсинг тарифов ТН ВЭД
# ------------------------------------------------------------------

def parse_tnved_tariff(tariff_str: str) -> dict:
    """Парсит тариф ТН ВЭД в структурированный формат."""
    result = {
        "raw": tariff_str, "type": "unknown", "percent": None,
        "eur_value": None, "usd_value": None, "unit": None, "formula": None,
    }
    if not tariff_str:
        return result
    s = tariff_str.strip()

    def _f(s: str) -> float:
        return float(s.replace(",", "."))

    # "X%, но не менее Y EUR за 1 кг"
    m = re.match(r"([\d.,]+)%\s*,\s*но не менее\s+([\d.,]+)\s+EUR\s+за\s+1\s+(\S+)", s)
    if m:
        result.update({"type": "min", "percent": _f(m.group(1)), "eur_value": _f(m.group(2)),
                       "unit": m.group(3), "formula": f"{_f(m.group(1))}%, минимум {_f(m.group(2))} EUR/{m.group(3)}"})
        return result

    # "X% плюс Y EUR за 1 кг"
    m = re.match(r"([\d.,]+)%\s*плюс\s+([\d.,]+)\s+EUR\s+за\s+1\s+(\S+)", s)
    if m:
        result.update({"type": "plus", "percent": _f(m.group(1)), "eur_value": _f(m.group(2)),
                       "unit": m.group(3), "formula": f"{_f(m.group(1))}% + {_f(m.group(2))} EUR/{m.group(3)}"})
        return result

    # "X EUR за 1 кг"
    m = re.match(r"([\d.,]+)\s+EUR\s+за\s+1\s+(\S+)", s)
    if m:
        result.update({"type": "fixed_eur", "eur_value": _f(m.group(1)),
                       "unit": m.group(2), "formula": f"{_f(m.group(1))} EUR/{m.group(2)}"})
        return result

    # "X USD за 1 т"
    m = re.match(r"([\d.,]+)\s+USD\s+за\s+1\s+(\S+)", s)
    if m:
        result.update({"type": "fixed_usd", "usd_value": _f(m.group(1)),
                       "unit": m.group(2), "formula": f"{_f(m.group(1))} USD/{m.group(2)}"})
        return result

    # "X%"
    m = re.match(r"([\d.,]+)%", s)
    if m:
        result.update({"type": "percent", "percent": _f(m.group(1)),
                       "formula": f"{_f(m.group(1))}%"})
        return result

    return result


def search_tnved_in_rows(rows: List[List[str]], code: str) -> Optional[dict]:
    """Ищет код ТН ВЭД в распарсенных строках Excel."""
    if not code:
        return None
    search_code = code.replace(" ", "").replace(".", "").strip()
    for row in rows:
        if not row or not isinstance(row[0], str):
            continue
        row_code = row[0].replace(" ", "").replace(".", "").strip()
        if row_code == search_code or (len(search_code) >= 6 and row_code.startswith(search_code)):
            tariff = row[2] if len(row) > 2 else ""
            return {"code": row[0], "name": row[1] if len(row) > 1 else "",
                    "tariff": tariff, "parsed_tariff": parse_tnved_tariff(tariff)}
    return None
