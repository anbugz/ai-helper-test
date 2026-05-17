"""
calc_engine.py — логика расчёта ТП и форматирования ответа.
Вся математика здесь, не в handlers.py.
"""
import re
from typing import Dict, Optional


def _strip_deepseek_dup(answer: str) -> str:
    """Вырезает дубли от DeepSeek: шапку с кодом и блок Платежи."""
    lines = answer.split("\n")
    pay_start = -1
    for i, line in enumerate(lines):
        ls = line.strip().lower()
        if (
            ls in ("платежи:", "платежи")
            or ls.startswith("📊 платежи:")
            or ls.startswith("📊 **платежи**")
            or "**платежи**" in ls
            or "платежи в валюте" in ls
        ):
            pay_start = i
            break
    if pay_start >= 0:
        lines = lines[:pay_start]
    tc_start = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("📊 Таможенная стоимость:") or line.strip().startswith(
            "📊 Таможенная стоим"
        ):
            tc_start = i
            break
    if tc_start > 0:
        header_end = tc_start
        has_header = False
        for j in range(tc_start - 1, -1, -1):
            ls = lines[j].strip()
            if any(
                ls.startswith(k)
                for k in ("📋 Код:", "📋 **Код:**", "🔧", "💰", "🧾", "⚡")
            ):
                has_header = True
                header_end = j
            elif ls == "":
                continue
            else:
                break
        if has_header:
            lines = lines[:header_end] + lines[tc_start:]
    return "\n".join(lines).strip()


def _format_payments_box(
    answer: str,
    currency: str,
    rates: dict = None,
    tariff_info: dict = None,
    is_radio: bool = False,
    customs_fee_rub: float = 0,
    vat_rate: float = 0.22,
    ts_fallback: float = None,
) -> str:
    """Красивый итоговый расчёт с ТС, эмодзи и рублевым эквивалентом.
    Если DeepSeek не вывёл ТС — использует ts_fallback из запроса пользователя.
    """
    lines = answer.split("\n")
    data: dict = {}
    ts_val = None
    cur_pat = {
        "USD": r"USD|\$",
        "EUR": r"EUR|€",
        "CNY": r"CNY|CNH|¥",
        "RUB": r"RUB|₽|руб|р\.",
    }
    cur_re = cur_pat.get(currency, re.escape(currency))

    for line in lines:
        ls = line.strip().lower()
        if any(k in ls for k in ("тс:", "таможенная стоимость:", "тс ", "итоговая стоимость")):
            m = re.search(
                r"([\d\s,.]+)\s*(?:" + cur_re + r"|₽|руб|р\.)",
                line,
                re.IGNORECASE,
            )
            if m:
                ts_val = m.group(1).strip().replace(" ", "")
                break

    if not ts_val and ts_fallback:
        ts_val = f"{ts_fallback:,.2f}".replace(",", " ")

    if not ts_val or not tariff_info:
        return ""

    try:
        ts_num = float(ts_val.replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return ""

    pt = tariff_info.get("parsed_tariff", {})
    rate = float(pt.get("percent", 0)) if pt.get("percent") else 0

    duty = round(ts_num * rate / 100, 2) if rate else 0
    data["пошлина"] = f"{duty:,.2f}".replace(",", " ")

    vat = round((ts_num + duty) * vat_rate, 2)
    data["ндс"] = f"{vat:,.2f}".replace(",", " ")

    fee_rub = customs_fee_rub
    fee_cur = 0.0
    if fee_rub > 0:
        if currency == "RUB":
            fee_cur = fee_rub
        elif rates and currency in rates:
            try:
                fee_cur = round(fee_rub / float(rates[currency]), 2)
            except (ValueError, TypeError, ZeroDivisionError):
                fee_cur = 0.0
    data["сбор"] = f"{fee_cur:,.2f}".replace(",", " ") if fee_cur > 0 else "0"

    total = duty + vat + fee_cur
    data["итого"] = f"{total:,.2f}".replace(",", " ")

    duty_label = "Пошлина"
    if tariff_info:
        pt = tariff_info.get("parsed_tariff", {})
        if pt.get("type") == "percent":
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
        elif pt.get("type") in ("min", "plus", "fixed_eur"):
            duty_label = f"Пошлина {tariff_info.get('tariff', '')}"
            if pt.get("type") in ("min", "plus"):
                data["предупреждение"] = (
                    "⚠️ Нужен вес нетто (кг) для точного расчёта EUR/кг."
                )

    vat_label = f"НДС {int(vat_rate * 100)}%"
    fee_label = "Сбор (радио) 73 860 ₽" if is_radio else "Сбор"

    def to_rub(val_str):
        if not rates or currency not in rates or currency == "RUB":
            return None
        try:
            rate = float(rates[currency])
            v = float(val_str.replace(" ", "").replace(",", "."))
            return round(v * rate, 2)
        except (ValueError, TypeError):
            return None

    parts = ["\n📊 <b>Итоговый расчёт</b>\n"]
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

    if "предупреждение" in data:
        parts.append(f"\n<i>{data['предупреждение']}</i>")

    if "итого" in data:
        parts.append("─────────────────────")
        parts.append(f"💵 <b>ИТОГО:</b> <code>{data['итого']} {currency}</code>")
        rub = to_rub(data["итого"])
        if rub:
            rub_str = f"{rub:,.2f}".replace(",", " ")
            parts.append(f"   ~ <code>{rub_str} ₽</code>")

    return "\n".join(parts)
