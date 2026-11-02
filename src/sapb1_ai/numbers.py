"""One way to write numbers everywhere (chat text, charts, Excel, briefing), so the same figure always looks the same.

Money is exact to the cent with thousands separators and the currency in front: RM 31,382.00, $1,228,841.75.
Quantities are whole units unless they have decimals: 1,234 or 12.5. Percentages have one decimal: 26.7%.
Nothing is shortened to K/M or rounded to whole currency units; only chart axis steps are round numbers.
"""

import json
import math

# How a B1 currency code is written for people (the B1 code is used when it is not listed here).
CURRENCY_LABELS = {"MYR": "RM"}

MONEY_XL = "#,##0.00;[Red]-#,##0.00"  # the Excel equivalents
QTY_XL = "#,##0"
PCT_XL = '0.0"%"'


def label(currency: str) -> str:
    """'MYR' -> 'RM'; '$' stays '$'."""
    currency = (currency or "").strip()
    return CURRENCY_LABELS.get(currency.upper(), currency)


def company_currency() -> str:
    """This company's currency as people write it, from the last data sync."""
    from .sync import DATA_DIR
    try:
        return label(json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or "")
    except (OSError, ValueError):
        return ""


def _prefix(cur: str) -> str:
    cur = label(cur)
    return f"{cur} " if cur[-1:].isalpha() else cur  # "RM 5.00", "$5.00"


def money(v, cur: str = "", decimals: int = 2) -> str:
    """RM 31,382.00 · -RM 1,250.50 · $1,228,841.75 (decimals=0 only for chart axis steps)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    sign = "-" if v < 0 else ""
    return f"{sign}{_prefix(cur)}{abs(v):,.{decimals}f}"


def qty(v) -> str:
    """1,234 · 12.5 · 0.125 (up to 3 decimals, only when the number has them)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def pct(v, decimals: int = 1) -> str:
    """26.7%"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{decimals}f}%"


def prompt_rule(cur: str) -> str:
    """The same rule for the model's own text."""
    example = money(31382, cur)
    return (f"Number format (always): write money exactly to the cent with thousands separators and the currency in "
            f"front, e.g. {example} or {money(-1250.5, cur)}; write the currency as {label(cur) or 'the tools give it'}. "
            "Never shorten to K/M/k, never round to whole amounts, never write 'about' or 'around' for an exact figure. "
            "Quantities as whole units (1,234) unless they have decimals; percentages with one decimal (26.7%).")
