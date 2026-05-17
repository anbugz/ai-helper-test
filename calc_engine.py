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
    # Если DeepSeek вывёл полный красивый расчёт — оставляем как есть
    lower = answer.lower()
    if any(k in lower for k in ("итого платежей", "итоговый расчёт", "итоговый расчет")):
        return answer.strip()

    lines = answer.split("\n")
    # ШАГ 1: найти и вырезать блок "ПЛАТЕЖИ:" или "📊 Платежи:"
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
    # ШАГ 2: найти и вырезать шапку с кодом (перед 📊 ТС)
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
    weight_kg: Optional[float] = None,
) -> str:
    """Красивый fallback-расчёт, если DeepSeek не вывёл полный ответ.
    Формат «как в примере пользователя».
    """
    if not ts_fallback or not tariff_info:
        return ""

    try:
        ts_num = float(ts_fallback)
    except (ValueError, TypeError):
        return ""

    pt = tariff_info.get("parsed_tariff", {})
    rate = float(pt.get("percent", 0)) if pt.get("percent") else 0

    # Пошлина (процентная часть)
    duty = round(ts_num * rate / 100, 2) if rate else 0.0

    # НДС
    vat = round((ts_num + duty) * vat_rate, 2)

    # Сбор в валюте
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

    # Курс для конвертации
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

    # --- Собираем текст ---
    lines = []

    # Исходные данные
    lines.append("📋 <b>Исходные данные:</b>")
    if code:
        lines.append(f"• Код ТН ВЭД: <code>{code}</code> — {name or '—'}")
    lines.append(f"• Стоимость: <code>{fmt(ts_num)} {currency}</code>")
    if weight_kg:
        lines.append(f"• Вес: <code>{weight_kg} кг</code>")
    else:
        lines.append("• Вес: не указан")
    lines.append("")

    # Ставки
    lines.append("📊 <b>Ставки:</b>")
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
    lines.append("")

    # Расчёт
    lines.append("📈 <b>Расчёт:</b>")
    lines.append(f"1. Таможенная стоимость: <code>{fmt(ts_num)} {currency}</code>")
    if rate_cur and currency != "RUB":
        lines.append(f"2. ТС в рублях: {fmt(ts_num)} × {rate_cur} = <code>{to_rub(ts_num)}</code>")
    if pt.get("type") in ("min", "plus"):
        lines.append(f"3. Пошлина ({tariff_str}): <code>{fmt(duty)} {currency}</code> (процентная часть)")
        lines.append("   ⚠️ Для точного расчёта нужен вес нетто (кг)")
    else:
        lines.append(f"3. Пошлина ({rate}%): {fmt(ts_num)} × {rate}% = <code>{fmt(duty)} {currency}</code>")
    lines.append(f"4. НДС {int(vat_rate * 100)}%: ({fmt(ts_num)} + {fmt(duty)}) × {int(vat_rate * 100)}% = <code>{fmt(vat)} {currency}</code>")
    if fee_cur > 0:
        if is_radio:
            lines.append(f"5. Сбор: 73 860 ₽ → в {currency}: 73 860 / {rate_cur or '—'} = <code>{fmt(fee_cur)} {currency}</code>")
        else:
            lines.append(f"5. Сбор: {fee_rub:,.0f} ₽ → в {currency}: {fee_rub:,.0f} / {rate_cur or '—'} = <code>{fmt(fee_cur)} {currency}</code>")
    lines.append("")

    # Итоговая таблица
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
