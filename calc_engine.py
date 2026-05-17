"""
calc_engine.py — логика расчёта ТП и форматирование ответа.
Вся математика здесь, не в handlers.py.
"""
import re
from typing import Dict, Optional
from utils import convert_fee_to_currency


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
    """Финальный формат расчёта ТП.

    📋 Код: 8471300000
    🔧 Портативные автоматические вычислительные машины весом не более 10 кг
    💰 Пошлина: 0% — адвалорная
    🧾 НДС: 22% (базовая)
    ⚡ Радиоэлектроника: сбор 73 860 ₽ (код 8471 в Приложении №1)

    🔄 Конвертация в валюту инвойса (CNY):
    • Инвойс: 100 000 CNY — уже в валюте инвойса
    • Фрахт: 900 USD → 65 814.75 ₽ → 6 132.48 CNY
    • Страховка: 2 500 ₽ → 232.95 CNY

    📊 Итоговый расчёт
    💰 Таможенная стоимость: 106 365.43 CNY
    📋 Пошлина 0%:                0.00 CNY
    🧾 НДС 22%:              23 400.39 CNY
    ⚡ Сбор:                  6 896.52 CNY  (73 860 ₽ → 6 896.52 CNY)
    ───────────────────────────────────
    💵 ИТОГО:                30 296.91 CNY  (~ 324 500.00 ₽)
    """
    if not ts_fallback or not tariff_info:
        return ""

    try:
        ts_num = float(ts_fallback)
    except (ValueError, TypeError):
        return ""

    pt = tariff_info.get("parsed_tariff", {})
    rate = float(pt.get("percent", 0)) if pt.get("percent") else 0
    eur_value = float(pt.get("eur_value", 0)) if pt.get("eur_value") else 0
    tariff_type = pt.get("type", "")

    # === РАСЧЁТ ПОШЛИНЫ ===
    # Адвалорная часть (всегда)
    duty_percent = round(ts_num * rate / 100, 2) if rate else 0.0

    # EUR-компонента (для комбинированных ставок)
    duty_eur_cur = 0.0  # в валюте инвойса
    eur_conv_line = None
    if eur_value and weight_kg:
        eur_total = eur_value * weight_kg  # сумма в EUR
        # Конвертируем EUR → ₽ → валюта инвойса
        if rates and "EUR" in rates and currency in rates:
            try:
                eur_rub = eur_total * float(rates["EUR"])
                duty_eur_cur = round(eur_rub / float(rates[currency]), 2)
                eur_conv_line = (
                    f"• Пошлина EUR: {eur_value} EUR/кг × {weight_kg} кг = {eur_total} EUR → "
                    f"{eur_rub:,.2f} ₽ → {fmt(duty_eur_cur)} {currency}"
                )
            except (ValueError, TypeError, ZeroDivisionError):
                pass

    # Выбираем пошлину в зависимости от типа ставки
    if tariff_type == "min":
        # Комбинированная: max(%, EUR/кг)
        duty = max(duty_percent, duty_eur_cur) if duty_eur_cur else duty_percent
    elif tariff_type in ("plus", "fixed_eur"):
        # Суммарная: % + EUR/кг или фикс EUR/кг
        duty = duty_percent + duty_eur_cur
    else:
        duty = duty_percent

    vat = round((ts_num + duty) * vat_rate, 2)

    fee_rub = customs_fee_rub
    fee_cur, _ = convert_fee_to_currency(fee_rub, currency, rates)

    total = duty + vat + fee_cur

    # Курс ЦБ РФ для валюты инвойса (для справочной конвертации ₽ в конце)
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

    # ── 1. ШАПКА ──────────────────────────────────
    if code:
        lines.append(f"📋 Код: {code}")
    if name:
        lines.append(f"🔧 {name}")

    tariff_str = tariff_info.get("tariff", "—")
    if pt.get("type") in ("min", "plus", "fixed_eur"):
        duty_type = f"комбинированная ({pt.get('formula', '')})"
    elif pt.get("type") == "fixed_usd":
        duty_type = f"специфическая ({pt.get('formula', '')})"
    elif pt.get("type") == "percent":
        duty_type = "адвалорная"
    else:
        duty_type = tariff_str

    lines.append(f"💰 Пошлина: {tariff_str} — {duty_type}")

    vat_label = "10% (льготная)" if vat_rate == 0.10 else "22% (базовая)"
    lines.append(f"🧾 НДС: {vat_label}")

    if is_radio:
        lines.append(f"⚡ Радиоэлектроника: сбор 73 860 ₽ (код {code[:4] if code else ''} в Приложении №1)")
    elif fee_rub > 0:
        lines.append(f"⚡ Сбор: {fee_rub:,.0f} ₽ (по шкале ПП РФ №1637)")

    lines.append("")

    # ── 2. КОНВЕРТАЦИЯ ────────────────────────────
    conv_lines = []
    if ts_components:
        # Инвойс
        if "invoice" in ts_components:
            inv = ts_components["invoice"]
            if inv["currency"] == currency:
                conv_lines.append(f"• Инвойс: {fmt(inv['value'])} {currency} — уже в валюте инвойса")
            else:
                # Конвертируем через ₽
                inv_rub = inv["value"] * float(rates.get(inv["currency"], 0))
                inv_cur = round(inv_rub / float(rates.get(currency, 1)), 2)
                conv_lines.append(
                    f"• Инвойс: {fmt(inv['value'])} {inv['currency']} → "
                    f"{fmt(round(inv_rub, 2))} ₽ → {fmt(inv_cur)} {currency}"
                )

        # Фрахт
        if "freight" in ts_components:
            fr = ts_components["freight"]
            if fr["currency"] == currency:
                conv_lines.append(f"• Фрахт: {fmt(fr['value'])} {currency}")
            else:
                if fr["currency"] == "RUB":
                    fr_cur = round(fr["value"] / float(rates.get(currency, 1)), 2)
                    conv_lines.append(
                        f"• Фрахт: {fmt(fr['value'])} ₽ → {fmt(fr_cur)} {currency}"
                    )
                elif fr.get("rate"):
                    conv_lines.append(f"• Фрахт: {fr['rate']}")
                else:
                    fr_rub = fr["value"] * float(rates.get(fr["currency"], 0))
                    fr_cur = round(fr_rub / float(rates.get(currency, 1)), 2)
                    conv_lines.append(
                        f"• Фрахт: {fmt(fr['value'])} {fr['currency']} → "
                        f"{fmt(round(fr_rub, 2))} ₽ → {fmt(fr_cur)} {currency}"
                    )

        # Страховка
        if "insurance" in ts_components:
            ins = ts_components["insurance"]
            if ins["currency"] == currency:
                conv_lines.append(f"• Страховка: {fmt(ins['value'])} {currency}")
            else:
                if ins["currency"] == "RUB":
                    ins_cur = round(ins["value"] / float(rates.get(currency, 1)), 2)
                    conv_lines.append(
                        f"• Страховка: {fmt(ins['value'])} ₽ → {fmt(ins_cur)} {currency}"
                    )
                elif ins.get("rate"):
                    conv_lines.append(f"• Страховка: {ins['rate']}")
                else:
                    ins_rub = ins["value"] * float(rates.get(ins["currency"], 0))
                    ins_cur = round(ins_rub / float(rates.get(currency, 1)), 2)
                    conv_lines.append(
                        f"• Страховка: {fmt(ins['value'])} {ins['currency']} → "
                        f"{fmt(round(ins_rub, 2))} ₽ → {fmt(ins_cur)} {currency}"
                    )

    # EUR-компонента пошлины в конвертацию
    if eur_conv_line:
        conv_lines.append(eur_conv_line)

    if conv_lines:
        lines.append(f"🔄 Конвертация в валюту инвойса ({currency}):")
        lines.extend(conv_lines)
        lines.append("")

    # ── БЛОК РАСЧЁТА ПОШЛИНЫ (только для комбинированных с EUR) ──
    if duty_eur_cur:
        lines.append("⚖️ Расчёт пошлины:")
        lines.append(f"• Адвалорная: {rate:g}% × {fmt(ts_num)} {currency} = {fmt(duty_percent)} {currency}")
        if tariff_type == "min":
            lines.append(f"• EUR-компонента: {eur_value} EUR/кг × {int(weight_kg)} кг = {eur_value * int(weight_kg)} EUR → {fmt(duty_eur_cur)} {currency}")
            if duty_eur_cur > duty_percent:
                lines.append(f"• Выбрано: EUR-компонента ({fmt(duty_eur_cur)} {currency} > {fmt(duty_percent)} {currency})")
            else:
                lines.append(f"• Выбрано: Адвалорная {rate:g}% ({fmt(duty_percent)} {currency} ≥ {fmt(duty_eur_cur)} {currency})")
        elif tariff_type == "plus":
            lines.append(f"• EUR-компонента: {eur_value} EUR/кг × {int(weight_kg)} кг = {eur_value * int(weight_kg)} EUR → {fmt(duty_eur_cur)} {currency}")
            lines.append(f"• Выбрано: Сумма обоих вариантов ({fmt(duty_percent)} + {fmt(duty_eur_cur)} = {fmt(duty)} {currency})")
        elif tariff_type == "fixed_eur":
            lines.append(f"• EUR-компонента: {eur_value} EUR/кг × {int(weight_kg)} кг = {eur_value * int(weight_kg)} EUR → {fmt(duty_eur_cur)} {currency}")
            lines.append(f"• Выбрано: Фиксированная EUR-компонента")
        lines.append("")

    # ── 3. ИТОГОВЫЙ РАСЧЁТ ────────────────────────
    # Собираем строки как пары (левый_текст, правый_текст) для выравнивания
    rows = []
    rows.append(("💰 Таможенная стоимость:", f"{fmt(ts_num)} {currency}"))
    # Пошлина: обычная или комбинированная с EUR
    if tariff_type == "min" and duty_eur_cur:
        rows.append(("📋 Пошлина (max):", f"{fmt(duty)} {currency}"))
    elif tariff_type in ("plus", "fixed_eur") and duty_eur_cur:
        rows.append((f"📋 Пошлина ({rate:g}% + EUR):", f"{fmt(duty)} {currency}"))
    else:
        rows.append((f"📋 Пошлина {rate:g}%:", f"{fmt(duty)} {currency}"))
    rows.append((f"🧾 НДС {int(vat_rate * 100)}%:", f"{fmt(vat)} {currency}"))
    if fee_cur > 0:
        fee_right = f"{fmt(fee_cur)} {currency}  ({fee_rub:,.0f} ₽ → {fmt(fee_cur)} {currency})"
        rows.append(("⚡ Сбор:", fee_right))

    # Находим максимальную длину левой части + отступ
    max_left = max(len(left) for left, _ in rows)
    gap = 2  # минимум 2 пробела между левой и правой частью

    lines.append("📊 Итоговый расчёт")
    for left, right in rows:
        lines.append(f"{left}{' ' * (max_left - len(left) + gap)}{right}")

    lines.append("───────────────────────────────────")

    # ИТОГО
    total_rub = to_rub(total)
    itogo_label = "💵 ИТОГО:"
    itogo_value = f"{fmt(total)} {currency}"
    itogo_rub = f"  (~ {total_rub})" if total_rub else ""
    itogo_padding = max_left - len(itogo_label) + gap
    lines.append(f"{itogo_label}{' ' * itogo_padding}{itogo_value}{itogo_rub}")

    return "\n".join(lines)
