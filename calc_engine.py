"""
calc_engine.py — логика расчёта ТП и форматирования ответа.
Вся математика здесь, не в handlers.py.
"""
import re
from typing import Dict, Optional


def _strip_deepseek_dup(answer: str) -> str:
    """Вырезает дубли от DeepSeek: шапку с кодом и блок Платежи.
    Осторожно: не трогаем хороший развёрнутый расчёт с "Итоговый расчёт" или "ИТОГО платежей".
    """
    lower = answer.lower()
    if any(k in lower for k in ("итого платежей", "итоговый расчёт", "итоговый расчет", "📊 итоговый")):
        return answer.strip()

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


def _format_calculation_fallback(
    code: Optional[str],
    name: Optional[str],
    currency: str,
    rates: dict,
    tariff_info: dict,
    is_radio: bool = False,
    customs_fee_rub: float = 0,
    vat_rate: float = 0.22,
    ts_fallback: Optional[float] = None,
    ts_components: Optional[Dict] = None,
    weight_kg: Optional[float] = None,
) -> str:
    """Компактный fallback-расчёт без пошаговой арифметики.
    Формат: Исходные данные → Конвертация → Итоговый расчёт.
    """
    if not ts_fallback or not tariff_info:
        return ""

    try:
        ts_num = float(ts_fallback)
    except (ValueError, TypeError):
        return ""

    pt = tariff_info.get("parsed_tariff", {})
    rate = float(pt.get("percent", 0)) if pt.get("percent") else 0

    duty = round(ts_num * rate / 100, 2) if rate else 0.0
    vat = round((ts_num + duty) * vat_rate, 2)

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

    total = duty + vat + fee_cur

    rate_cur = None
    if rates and currency in rates:
        try:
            rate_cur = float(rates[currency])
        except (ValueError, TypeError):
            rate_cur = None

    def fmt(num: float) -> str:
        return f"{num:,.2f}".replace(",", " ")

    def to_rub(val: float) -> str:
        if rate_cur and currency != "RUB":
            return f"{fmt(round(val * rate_cur, 2))} ₽"
        return ""

    lines = []

    # 📋 Исходные данные
    lines.append("📋 <b>Исходные данные:</b>")
    if code:
        lines.append(f"• Код ТН ВЭД: <code>{code}</code> — {name or '—'}")

    tariff_str = tariff_info.get("tariff", "—")
    duty_type = "адвалорная"
    if pt.get("type") in ("min", "plus", "fixed_eur"):
        duty_type = f"комбинированная ({pt.get('formula', '')})"
    elif pt.get("type") == "fixed_usd":
        duty_type = f"специфическая ({pt.get('formula', '')})"

    lines.append(f"• Пошлина: <b>{tariff_str}</b> ({duty_type})")
    lines.append(f"• НДС: <b>{int(vat_rate * 100)}%</b> ({'льготная' if vat_rate == 0.10 else 'базовая'})")
    if is_radio:
        lines.append("• Сбор: <b>73 860 ₽</b> (фиксированный, код в перечне радиоэлектроники)")
    else:
        if fee_rub > 0:
            lines.append(f"• Сбор: <b>{fee_rub:,.0f} ₽</b> (по шкале ПП РФ №1637)")
        else:
            lines.append("• Сбор: <b>0 ₽</b>")

    # Компоненты ТС с валютами
    if ts_components:
        for key, label in (("invoice", "Инвойс"), ("freight", "Фрахт"), ("insurance", "Страховка")):
            if key in ts_components:
                comp = ts_components[key]
                val = comp["value"]
                cur = comp["currency"]
                if cur == currency:
                    lines.append(f"• {label}: <code>{fmt(val)} {cur}</code>")
                else:
                    lines.append(f"• {label}: <code>{fmt(val)} {cur}</code> → <code>{fmt(comp['converted'])} {currency}</code>")
        lines.append(f"• <b>ТС:</b> <code>{fmt(ts_num)} {currency}</code>")
    else:
        lines.append(f"• Стоимость: <code>{fmt(ts_num)} {currency}</code>")
    if weight_kg:
        lines.append(f"• Вес: <code>{weight_kg} кг</code>")
    else:
        lines.append("• Вес: не указан")
    lines.append("")

    # 🔄 Конвертация
    conv_lines = []
    if ts_components:
        for key, label in (("freight", "Фрахт"), ("insurance", "Страховка")):
            if key in ts_components:
                comp = ts_components[key]
                if comp["currency"] != currency and comp.get("rate"):
                    conv_lines.append(f"• {label}: {comp['rate']}")
    # Евро-компонента пошлины
    if pt.get("type") in ("min", "plus", "fixed_eur") and rates and "EUR" in rates:
        eur_r = rates.get("EUR", "—")
        cny_r = rates.get("CNY", "—")
        usd_r = rates.get("USD", "—")
        conv_lines.append(f"• Курс EUR: 1 EUR = {eur_r} ₽ (для конвертации EUR/кг)")
        if currency == "CNY" and eur_r not in ("—", "", None) and cny_r not in ("—", "", None):
            try:
                cross = round(float(eur_r) / float(cny_r), 4)
                conv_lines.append(f"• Кросс EUR→CNY: 1 EUR = {cross} CNY")
            except (ValueError, TypeError):
                pass
        elif currency == "USD" and eur_r not in ("—", "", None) and usd_r not in ("—", "", None):
            try:
                cross = round(float(eur_r) / float(usd_r), 4)
                conv_lines.append(f"• Кросс EUR→USD: 1 EUR = {cross} USD")
            except (ValueError, TypeError):
                pass

    if conv_lines:
        lines.append(f"🔄 <b>Конвертация в валюту инвойса ({currency}):</b>")
        lines.extend(conv_lines)
        lines.append("")

    # 📊 Итоговый расчёт
    lines.append("📊 <b>Итоговый расчёт</b>")
    lines.append(f"Таможенная стоимость:  {fmt(ts_num):>12} {currency}   (~ {to_rub(ts_num)})")
    lines.append(f"Пошлина {rate}%:        {fmt(duty):>12} {currency}   (~ {to_rub(duty)})")
    lines.append(f"НДС {int(vat_rate * 100)}%:              {fmt(vat):>12} {currency}   (~ {to_rub(vat)})")
    if fee_cur > 0:
        fee_label = "Сбор (радио):" if is_radio else "Сбор:"
        lines.append(f"{fee_label:<22} {fmt(fee_cur):>12} {currency}   (~ {to_rub(fee_cur)})")
    lines.append("────────────────────────────────────────────────")
    lines.append(f"<b>ИТОГО платежей:</b>     {fmt(total):>12} {currency}   (~ {to_rub(total)})")

    return "\n".join(lines)
