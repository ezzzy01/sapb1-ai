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


# ---------- the model's own text: shortened amounts back to the exact figure ----------

import re  # noqa: E402

_SCALES = {"k": 1e3, "thousand": 1e3, "ribu": 1e3, "m": 1e6, "mil": 1e6, "mln": 1e6, "million": 1e6, "juta": 1e6,
           "b": 1e9, "bn": 1e9, "billion": 1e9, "bilion": 1e9}
_CUR = r"(?:RM|MYR|USD|EUR|SGD|CAN|CAD|\$)"
# "$1.67M", "RM 8.1K", "$1.34 million", "RM2.5 juta"; a short suffix must be attached, a word may follow a space.
SHORT_AMOUNT = re.compile(rf"(?P<cur>{_CUR})\s?(?P<num>\d{{1,3}}(?:\.\d{{1,3}})?)"
                          r"(?:(?P<abbr>[KkMmBb]|bn|mil|mln)|\s?(?P<word>thousand|million|billion|ribu|juta|bilion))(?![A-Za-z])")
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class ExactAmounts:
    """Fixes amounts the model shortened ("$1.67M") in a streamed answer, using the exact figures in the tool results
    of this request ($1,670,701.79). Left as written when no figure, or more than one different figure, fits."""

    HOLD = 30  # characters kept back so an amount split over two chunks is still seen whole

    def __init__(self):
        self.values: set[float] = set()
        self.buf = ""

    def learn(self, text: str) -> None:
        for n in _NUMBER.findall(text or ""):
            try:
                v = abs(float(n.replace(",", "")))
            except ValueError:
                continue
            if v >= 1000:
                self.values.add(round(v, 2))

    def _exact(self, m: re.Match) -> str:
        num = m["num"]
        scale = _SCALES[(m["abbr"] or m["word"]).lower()]
        decimals = len(num.split(".")[1]) if "." in num else 0
        shown = float(num)
        fits = {v for v in self.values if round(v / scale, decimals) == shown}
        if len(fits) != 1:
            return m.group(0)
        return money(fits.pop(), m["cur"])

    def fix(self, text: str) -> str:
        return SHORT_AMOUNT.sub(self._exact, text)

    def feed(self, chunk: str) -> str:
        if "data:image" in chunk:  # a chart image: pass through untouched
            out, self.buf = self.fix(self.buf) + chunk, ""
            return out
        self.buf = self.fix(self.buf + chunk)
        cut = max(0, len(self.buf) - self.HOLD)
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self) -> str:
        out, self.buf = self.fix(self.buf), ""
        return out

    async def wrap(self, chunks):
        async for chunk in chunks:
            if out := self.feed(chunk):
                yield out
        if out := self.flush():
            yield out
