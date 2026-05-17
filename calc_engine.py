"""
calc_engine.py — логика расчёта ТП и форматирования ответа.
Вся математика здесь, не в handlers.py.
"""
import re
from typing import Dict, Optional


def _strip_deepseek_dup(answer: str) -> str:
    """Вырезает дубли от DeepSeek: шапку с кодом и блок Платежи."""
    lines = answer.split("\n")
    # ШАГ 1: найти и вырезать блок "ПЛАТЕЖИ:" или "📊 Платежи:"
    pay_start = -1
    for i, line in enumerate(lines):
        ls = line.strip().lower()
        if ls in ("платежи:", "платежи") or ls.startswith("📊 платежи:") or ls.startswith("📊 **платежи**") or "**платежи**" in ls or "платежи в валюте" in ls:
            pay_start = i
            break
    if pay_start >= 0:
        lines = lines[:pay_start]
    # ШАГ 2: найти и вырезать шапку с кодом (перед 📊 ТС)
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
    lines = answer.split("\n")
    data: dict = {}
    ts_val = None
    cur_pat = {"USD": r"USD|\\$", "EUR": r"EUR|€", "CNY": r"CNY|CNH|¥", "RUB": r"RUB|₽"}
    cur_re = cur_pat.get(currency, re.escape(currency))
    
    # Ищем ТС в ответе
    for line in lines:
        ls = line.strip().lower()
        if any(k in ls for k in ("тс:", "таможенная стоимость:")):
            m = re.search(r"([\d\s,.]+)\s*(?:" + cur_re + r")", line, re.IGNORECASE)
            if m:
                ts_val = m.group(1).strip().replace(" ", "")
    
    # Всегда считаем сами если есть ТС и ставки
    if ts_val and tariff_info:
        try:
            ts_num = float(ts_val.replace(" ", "").replace(",", "."))
            pt = tariff_info.get("parsed_tariff", {})
            rate = float(pt.get("rate", 0)) if pt.get("rate") else 0
            duty = round(ts_num * rate / 100, 2) if rate else 0
            data["пошлина"] = f"{duty:,.2f}".replace(",", " ")
            vat = round((ts_num + duty) * 0.22, 2)
            data["ндс"] = f"{vat:,.2f}".replace(",", " ")
            data["сбор"] = "—" if is_radio else "0"
            total = duty + vat
            data["итого"] = f"{total:,.2f}".replace(",", " ")
        except (ValueError, TypeError):
            pass
    
    if "пошлина" not in data and "ндс" not in data:
        return ""
    
    # Ставки для подписей
    duty_label = "Пошлина"
    vat_label = "НДС 22%"
    fee_label = "Сбор (радио) 73 860 ₽" if is_radio else "Сбор"
    if tariff_info:
        pt = tariff_info.get("parsed_tariff", {})
        if pt.get("type") == "percent":
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
        elif pt.get("type") in ("min", "plus", "fixed_eur"):
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
    
    def to_rub(val_str):
        if not rates or currency not in rates:
            return None
        try:
            rate = float(rates[currency])
            v = float(val_str.replace(" ", "").replace(",", "."))
            return round(v * rate, 2)
        except (ValueError, TypeError):
            return None
    
    parts = ["\n📊 <b>Итоговый расчёт</b>\n"]
    if ts_val:
        parts.append(f"💰 Таможенная стоимость: <code>{ts_val} {currency}</code>")
        rub = to_rub(ts_val)
        if rub:
            rub_str = f"{rub:,.2f}".replace(",", " ")
            parts.append(f"   ~ <code>{rub_str} ₽</code>")
        parts.append("")
    
    for key, emoji in (("пошлина", "📋"), ("ндс", "🧾"), ("сбор", "⚡")):
        if key in data:
            lbl = {"пошлина": duty_label, "ндс": vat_label, "сбор": fee_label}[key]
            parts.append(f"{emoji} {lbl}: <code>{data[key]} {currency}</code>")
            rub = to_rub(data[key])
            if rub and rub > 0:
                rub_str = f"{rub:,.2f}".replace(",", " ")
                parts.append(f"   ~ <code>{rub_str} ₽</code>")
    
    if "итого" in data:
        parts.append("─────────────────────")
        parts.append(f"💵 <b>ИТОГО:</b> <code>{data['итого']} {currency}</code>")
        rub = to_rub(data['итого'])
        if rub:
            rub_str = f"{rub:,.2f}".replace(",", " ")
            parts.append(f"   ~ <code>{rub_str} ₽</code>")
    
    return "\n".join(parts)
