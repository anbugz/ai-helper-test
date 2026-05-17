"""
tnved_fetcher.py — получение полных наименований ТН ВЭД с внешних сайтов.

Логика:
1. Пробуем classinform.ru → alta.ru → tws.by (по очереди)
2. Если нашли — записываем в SQLite (tnved_cache.full_name)
3. Если все сайты недоступны — берём name из Excel
4. Если сайт ≠ Excel — предупреждение (различается более чем на 30%)
"""
import asyncio
import urllib.request
import urllib.error
import sqlite3
import re
import html
from typing import Optional, Tuple
from config import logger

DB_PATH = None  # Устанавливается из database.py


def _set_db_path(path: str):
    global DB_PATH
    DB_PATH = path


def _fetch_classinform(code: str) -> Optional[str]:
    """Парсит classinform.ru — полные наименования."""
    try:
        url = f"https://classinform.ru/Info/TNVED/{code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        # Ищем наименование после кода
        pattern = re.compile(rf'{code}\s*[–—-]?\s*([^<\n]+)', re.IGNORECASE)
        m = pattern.search(text)
        if m:
            name = html.unescape(m.group(1).strip())
            if len(name) > 20:  # Фильтруем мусор
                return name
        # Fallback: ищем в title или h1
        title_m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.DOTALL | re.IGNORECASE)
        if title_m:
            title = html.unescape(re.sub(r'<[^>]+>', '', title_m.group(1)).strip())
            if code in title and len(title) > len(code) + 5:
                return title.replace(code, '').strip(' –—-:')
    except Exception as e:
        logger.debug(f"classinform.ru {code}: {e}")
    return None


def _fetch_alta(code: str) -> Optional[str]:
    """Парсит alta.ru."""
    try:
        url = f"https://www.alta.ru/tnved/code/{code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        # Ищем h1 или .name
        h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.DOTALL | re.IGNORECASE)
        if h1_m:
            name = html.unescape(re.sub(r'<[^>]+>', '', h1_m.group(1)).strip())
            name = re.sub(rf'\b{code}\b', '', name).strip(' –—-:')
            if len(name) > 20:
                return name
    except Exception as e:
        logger.debug(f"alta.ru {code}: {e}")
    return None


def _fetch_tws(code: str) -> Optional[str]:
    """Парсит tws.by — собирает полный путь из иерархии."""
    try:
        url = f"https://www.tws.by/tws/tnved/code/{code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        # Ищем breadcrumbs / иерархию
        parts = []
        # Заголовок страницы
        title_m = re.search(r'<title>(.*?)</title>', text, re.IGNORECASE)
        if title_m:
            title = html.unescape(title_m.group(1))
            # TWS title: "Код ТН ВЭД 8528599009: прочие (с 01.01.2017) | TWS.BY"
            name_part = title.split('|')[0].replace(f'Код ТН ВЭД {code}:', '').strip()
            if name_part:
                parts.append(name_part)
        # Ищем цепочку родителей в хлебных крошках
        crumbs = re.findall(r'<a[^>]*href="/tws/tnved/[^"]*"[^>]*>(.*?)</a>', text)
        if crumbs:
            for crumb in crumbs:
                clean = html.unescape(re.sub(r'<[^>]+>', '', crumb).strip())
                if clean and clean not in parts:
                    parts.insert(0, clean)
        if parts:
            return ' → '.join(parts)
    except Exception as e:
        logger.debug(f"tws.by {code}: {e}")
    return None


async def fetch_full_name(code: str, excel_name: str = "") -> Tuple[str, bool, Optional[str]]:
    """Получает полное наименование с сайтов.
    
    Returns:
        (name, from_cache, warning)
        name — полное наименование
        from_cache — True если взято из SQLite
        warning — текст предупреждения если сайт ≠ Excel
    """
    global DB_PATH
    
    # 1. Проверяем SQLite (full_name)
    if DB_PATH:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT full_name FROM tnved_cache WHERE code = ? AND full_name IS NOT NULL', (code,))
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                logger.debug(f"{code}: full_name из кэша SQLite")
                return row[0], True, None
        except Exception:
            pass
    
    # 2. Запрашиваем сайты (по очереди)
    sources = [
        ("classinform.ru", _fetch_classinform),
        ("alta.ru", _fetch_alta),
        ("tws.by", _fetch_tws),
    ]
    full_name = None
    source_used = None
    
    loop = asyncio.get_event_loop()
    for source_name, fetcher in sources:
        try:
            full_name = await asyncio.wait_for(
                loop.run_in_executor(None, fetcher, code),
                timeout=12
            )
            if full_name and len(full_name) > 10:
                source_used = source_name
                logger.info(f"{code}: получено с {source_name}: {full_name[:60]}...")
                break
        except asyncio.TimeoutError:
            logger.debug(f"{source_name} timeout для {code}")
        except Exception as e:
            logger.debug(f"{source_name} ошибка для {code}: {e}")
    
    # 3. Fallback на Excel
    if not full_name:
        if excel_name:
            logger.debug(f"{code}: fallback на Excel")
            return excel_name, False, None
        return code, False, None
    
    # 4. Сравнение с Excel
    warning = None
    if excel_name and len(excel_name) > 3:
        # Простое сравнение: если Excel-name не входит в full_name
        excel_short = re.sub(r'[^а-яa-z0-9]', '', excel_name.lower())
        full_short = re.sub(r'[^а-яa-z0-9]', '', full_name.lower())
        if excel_short not in full_short and len(excel_short) > 5:
            overlap = sum(1 for a, b in zip(excel_short, full_short) if a == b)
            similarity = overlap / max(len(excel_short), len(full_short)) if max(len(excel_short), len(full_short)) > 0 else 0
            if similarity < 0.5:  # Менее 50% совпадения
                warning = (
                    f"⚠️ Расхождение: сайт ({source_used}) ≠ Excel:\n"
                    f"  Сайт: {full_name[:80]}\n"
                    f"  Excel: {excel_name[:80]}"
                )
                logger.warning(warning)
    
    # 5. Сохраняем в SQLite
    if DB_PATH and full_name:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                'UPDATE tnved_cache SET full_name = ?, full_name_source = ? WHERE code = ?',
                (full_name, source_used, code)
            )
            conn.commit()
            conn.close()
            logger.debug(f"{code}: full_name сохранён в SQLite")
        except Exception as e:
            logger.debug(f"Не удалось сохранить full_name: {e}")
    
    return full_name, False, warning


# Инициализация
import os
if os.path.exists('/home/wa/bot-data/bot.db'):
    _set_db_path('/home/wa/bot-data/bot.db')
elif os.path.exists(os.path.expanduser('~/ai-helper-test/bot.db')):
    _set_db_path(os.path.expanduser('~/ai-helper-test/bot.db'))
