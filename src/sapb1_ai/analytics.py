"""Management dashboards, LIVE from B1 (read-only SQL), in the same style as the sales analysis:
charts in the chat, a short table, and an Excel dashboard to download. Every figure is exact.

    compare_periods          this month / quarter / year against the one before or the same one last year, and what caused it
    quotation_pipeline       open quotations, win rate, ageing, which ones to follow up
    customer_insights        growing, steady, declining, at risk, new and lost customers; how often they buy; how they pay
    product_profitability    margin per item and item group, ABC ranking, items whose price or margin is slipping
    salesperson_performance  sales, margin, customers and quotation win rate per salesperson, against the period before
    delivery_performance     on-time delivery to customers, late order lines (back orders), days from order to delivery
    inventory_health         stock value, turnover, days of stock, items not moving
    profit_and_loss          monthly P&L from the accounts, and budget vs actual where a budget is set in B1
"""

import datetime as dt
import logging

import pandas as pd

from .charts import BLUE, GREEN, OTHER, RED, _bars_left, _figure, _hbar, _image
from .db import query
from .export import excel_link
from .numbers import label, money, pct, qty

TOP = 10
log = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------- shared pieces

def _cur() -> str:
    return label(query("SELECT MainCurncy FROM OADM").MainCurncy.iloc[0])


def _today() -> dt.date:
    return dt.date.today()


def _months_ago(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 - months, 12)
    return dt.date(d.year + y, m + 1, 1)


def _sales_lines(d_from: dt.date, d_to: dt.date) -> pd.DataFrame:
    """Invoice lines minus credit memo lines (net of header discount), with gross profit, customer, salesperson, group."""
    part = """SELECT h.DocEntry, h.DocDate, h.CardCode, h.CardName, h.SlpCode, l.ItemCode, l.Dscription AS ItemName,
                     ISNULL(g.ItmsGrpNam, '(no group)') AS ItemGroup, {s} l.Quantity AS Qty,
                     {s} l.LineTotal * (1 - ISNULL(h.DiscPrcnt, 0) / 100) AS Net,
                     {s} (l.GrssProfit - l.LineTotal * ISNULL(h.DiscPrcnt, 0) / 100) AS GP
              FROM {lines} l JOIN {head} h ON h.DocEntry = l.DocEntry
              LEFT JOIN OITM i ON i.ItemCode = l.ItemCode LEFT JOIN OITB g ON g.ItmsGrpCod = i.ItmsGrpCod
              WHERE h.CANCELED = 'N' AND h.DocDate >= ? AND h.DocDate < ?"""
    end = d_to + dt.timedelta(days=1)
    df = query(part.format(s="", lines="INV1", head="OINV") + " UNION ALL " + part.format(s="-", lines="RIN1", head="ORIN"),
               (d_from, end, d_from, end))
    df["DocDate"] = pd.to_datetime(df.DocDate)
    for c in ("Qty", "Net", "GP"):
        df[c] = df[c].astype(float)
    return df


def _salespeople() -> dict:
    r = query("SELECT SlpCode, SlpName FROM OSLP")
    return dict(zip(r.SlpCode, r.SlpName))


def _hchart(title: str, subtitle: str, names: list[str], values: list[float], labels: list[str], colours=BLUE) -> str:
    names = [str(n)[:38] for n in names]
    fig, ax = _figure(0.42 * max(len(names), 1) + 1.3, title, subtitle, left=_bars_left(names))
    _hbar(ax, names, values, labels, colours)
    return _image(fig, title)


def _vchart(title: str, subtitle: str, names: list[str], values: list[float], cur: str, colours=None, line=None) -> str:
    """Vertical bars (months), each labelled exactly; optional second series drawn as a line (e.g. budget)."""
    fig, ax = _figure(3.6, title, subtitle)
    x = range(len(names))
    ax.bar(x, values, color=colours or [BLUE if v >= 0 else RED for v in values], width=0.62)
    if line is not None:
        ax.plot(x, line, color="#556b82", linewidth=2, marker="o", markersize=4, label="Budget")
        ax.legend(frameon=False, fontsize=9, loc="upper left")
    ax.set_xticks(list(x), names)
    ax.tick_params(axis="x", colors="#556b82", labelsize=8.5)
    ax.yaxis.set_visible(False)
    ax.axhline(0, color="#e4e8ec", linewidth=1)
    for i, v in enumerate(values):
        if v:
            ax.annotate(money(v, cur), (i, v), xytext=(0, 4 if v >= 0 else -11), textcoords="offset points", ha="center",
                        fontsize=7.5 if len(values) > 8 else 9, color="#1d2d3e")
    lo, hi = min(list(values) + [0] + list(line or [])), max(list(values) + [0] + list(line or []))
    ax.set_ylim(lo * 1.18 if lo < 0 else 0, (hi or 1) * 1.18)
    return _image(fig, title)


def _md(df: pd.DataFrame, cols: dict[str, str], money_cols: tuple = (), cur: str = "", pct_cols: tuple = (), n: int = TOP) -> list[str]:
    """A markdown table of the first n rows: cols = {column: heading}."""
    head = "| " + " | ".join(cols.values()) + " |"
    sep = "|" + "|".join("---:" if c in money_cols or c in pct_cols or pd.api.types.is_numeric_dtype(df[c]) else "---"
                         for c in cols) + "|"
    rows = []
    for _, r in df.head(n).iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if c in money_cols:
                cells.append(money(float(v), cur) if pd.notna(v) else "-")
            elif c in pct_cols:
                cells.append(pct(float(v)) if pd.notna(v) else "-")
            elif isinstance(v, float):
                cells.append(qty(v))
            elif isinstance(v, (pd.Timestamp, dt.date)):
                cells.append(f"{pd.Timestamp(v):%d %b %Y}" if pd.notna(v) else "-")
            else:
                cells.append(str(v))
        rows.append("| " + " | ".join(cells) + " |")
    return [head, sep, *rows]


def _result(display: list[str], facts: dict, what: str) -> dict:
    return {"_display": "\n".join(display) + "\n", **facts,
            "instruction": f"The {what} with its charts, tables and Excel download is already shown to the user. Do not "
                           "repeat the tables or call more tools for it. Add only 3-5 short insights using these numbers "
                           "exactly; do not calculate new ones. Mention the Excel download in one short line."}


import contextvars

SKIP_EXCEL: contextvars.ContextVar[bool] = contextvars.ContextVar("skip_excel", default=False)  # set by the management pack


def _excel(name: str, sheets: dict, money_cols: tuple, title: str, subtitle: str, tiles: list, charts: list) -> str:
    if SKIP_EXCEL.get():
        return ""
    try:
        return excel_link(name, sheets, money_cols=money_cols, title=title, subtitle=subtitle,
                          summary={"tiles": tiles, "charts": charts, "note": "Full figures are on the next sheets."})
    except Exception:
        log.exception("Excel for %s failed", name)
        return "*(The Excel file could not be made this time.)*"


# ---------------------------------------------------------------- 1. period comparison

def _window(period: str, which: str) -> tuple[dt.date, dt.date, str]:
    """The period asked for: 'this' (to date), 'last' (last complete one, default) or 'YYYY-MM' / 'YYYY-Qn' / 'YYYY'."""
    t = _today()
    p = (period or "month").lower()
    w = (which or "last").strip().lower()
    if p.startswith("q"):
        start_q = dt.date(t.year, 3 * ((t.month - 1) // 3) + 1, 1)
        if w.startswith("this"):
            a, b = start_q, t
        elif len(w) == 7 and w[4] in "-q" and w[-1].isdigit():
            y, q = int(w[:4]), int(w[-1])
            a = dt.date(y, 3 * (q - 1) + 1, 1)
            b = _months_ago(a, -3) - dt.timedelta(days=1)
        else:
            a, b = _months_ago(start_q, 3), start_q - dt.timedelta(days=1)
        return a, b, f"Q{(a.month - 1) // 3 + 1} {a.year}" + (" (to date)" if b == t else "")
    if p.startswith("y"):
        if w.startswith("this"):
            a, b = dt.date(t.year, 1, 1), t
        elif w.isdigit() and len(w) == 4:
            a, b = dt.date(int(w), 1, 1), dt.date(int(w), 12, 31)
        else:
            a, b = dt.date(t.year - 1, 1, 1), dt.date(t.year - 1, 12, 31)
        return a, b, f"{a.year}" + (" (to date)" if b == t else "")
    if w.startswith("this"):
        a, b = t.replace(day=1), t
    elif len(w) == 7 and w[4] == "-":
        a = dt.date(int(w[:4]), int(w[5:]), 1)
        b = _months_ago(a, -1) - dt.timedelta(days=1)
    else:
        a = _months_ago(t.replace(day=1), 1)
        b = t.replace(day=1) - dt.timedelta(days=1)
    return a, b, f"{a:%B %Y}" + (" (to date)" if b == t else "")


def _shift(a: dt.date, b: dt.date, period: str, back: str) -> tuple[dt.date, dt.date]:
    if back == "last_year":
        return a.replace(year=a.year - 1), (b.replace(year=b.year - 1) if not (b.month == 2 and b.day == 29) else b.replace(year=b.year - 1, day=28))
    months = 12 if (period or "").lower().startswith("y") else 3 if (period or "").lower().startswith("q") else 1
    a2 = _months_ago(a, months)
    days = (b - a).days
    end_full = _months_ago(a, 0) - dt.timedelta(days=1)  # day before a
    return a2, min(a2 + dt.timedelta(days=days), end_full)


def compare_periods(period: str = "month", which: str = "last", against: str = "previous", by: str = "customer") -> dict:
    """Net sales and gross profit of one period against the previous period (or the same period last year), and which
    customers / items / item groups / salespeople caused the change."""
    against = "last_year" if "year" in (against or "") else "previous"
    a, b, name = _window(period, which)
    a2, b2 = _shift(a, b, period, against)
    cur = _cur()
    now, before = _sales_lines(a, b), _sales_lines(a2, b2)
    by = (by or "customer").lower()
    key = {"item": "ItemName", "product": "ItemName", "group": "ItemGroup", "item_group": "ItemGroup",
           "salesperson": "SlpCode", "sales person": "SlpCode"}.get(by, "CardName")
    by_word = {"ItemName": "item", "ItemGroup": "item group", "SlpCode": "salesperson", "CardName": "customer"}[key]
    slp = _salespeople()

    def totals(df):
        return float(df.Net.sum()), float(df.GP.sum()), int(df[df.Net > 0].DocEntry.nunique())
    n1, g1, i1 = totals(now)
    n0, g0, i0 = totals(before)
    change = lambda x, y: (x - y) / abs(y) * 100 if y else None
    cmp_name = f"{a2:%d %b %Y} to {b2:%d %b %Y}"
    m = (now.groupby(key).Net.sum().rename("now").to_frame().join(before.groupby(key).Net.sum().rename("before"), how="outer")
         .fillna(0.0))
    m["change"] = m["now"] - m["before"]
    m = m.reset_index().rename(columns={key: "name"})
    if key == "SlpCode":
        m["name"] = m["name"].map(lambda c: slp.get(c, "No salesperson") if c and c > 0 else "No salesperson")
    m = m.sort_values("change")
    ups, downs = m[m.change > 0].sort_values("change", ascending=False).head(5), m[m.change < 0].head(5)
    drivers = pd.concat([ups, downs.iloc[::-1]]) if len(ups) or len(downs) else m.head(0)

    title = f"{name} compared with {'the same period last year' if against == 'last_year' else 'the period before'}"
    out = [f"## {title}", f"{a:%d %b %Y} to {b:%d %b %Y} vs {cmp_name} · {cur}, excluding tax, after credit memos", ""]
    pc = lambda x, y: (f"{change(x, y):+.1f}%" if change(x, y) is not None else "new" if x else "-")
    if not n1 and not n0 and now.empty and before.empty:
        return {"note": f"No sales in {name} ({a:%d %b %Y} to {b:%d %b %Y}) nor in {cmp_name}. Say so plainly; suggest another period."}
    out += ["| | This period | Compared with | Change |", "|---|---:|---:|---:|",
            f"| Net sales | {money(n1, cur)} | {money(n0, cur)} | {money(n1 - n0, cur)} ({pc(n1, n0)}) |",
            f"| Gross profit | {money(g1, cur)} | {money(g0, cur)} | {money(g1 - g0, cur)} ({pc(g1, g0)}) |",
            f"| Margin | {pct(g1 / n1 * 100) if n1 else '-'} | {pct(g0 / n0 * 100) if n0 else '-'} | |",
            f"| Invoices | {i1:,} | {i0:,} | {i1 - i0:+,} |", ""]
    if len(drivers):
        out.append(_hchart(f"What changed net sales, by {by_word}", f"{cur} · {name} vs {cmp_name} · green = up, red = down",
                           list(drivers.name), list(drivers.change), [money(v, cur) for v in drivers.change],
                           [GREEN if v > 0 else RED for v in drivers.change]) + "\n")
        out += [f"### Biggest changes by {by_word}"] + _md(drivers.assign(name=drivers.name.astype(str)),
                                                            {"name": by_word.capitalize(), "now": "This period", "before": "Compared with", "change": "Change"},
                                                            ("now", "before", "change"), cur, n=10)
    link = _excel(f"Comparison_{a:%Y%m%d}", {"Summary": pd.DataFrame({
        "Figure": ["Net sales", "Gross profit", "Invoices"], "This period": [n1, g1, i1], "Compared with": [n0, g0, i0],
        "Change": [n1 - n0, g1 - g0, i1 - i0]}), f"By {by_word}": m.sort_values("now", ascending=False).rename(
        columns={"name": by_word.capitalize(), "now": "This period", "before": "Compared with", "change": "Change"})},
        ("This period", "Compared with", "Change"), title, f"{a:%d %b %Y}-{b:%d %b %Y} vs {cmp_name} · {cur}",
        [{"label": f"Net sales ({cur})", "value": round(n1, 2), "note": f"{pc(n1, n0)} vs {money(n0, cur)}"},
         {"label": f"Gross profit ({cur})", "value": round(g1, 2), "note": f"{pc(g1, g0)} vs {money(g0, cur)}"}],
        [{"sheet": f"By {by_word}", "category": by_word.capitalize(), "value": "Change", "type": "bar", "top": 10,
          "title": f"Change by {by_word}", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"period": [str(a), str(b)], "compared_with": [str(a2), str(b2)],
                         "net_sales": round(n1, 2), "net_sales_before": round(n0, 2), "change_pct": round(change(n1, n0), 1) if n0 else None,
                         "gross_profit": round(g1, 2), "gross_profit_before": round(g0, 2),
                         "biggest_increases": [{"name": str(r["name"]), "change": round(r.change, 2)} for _, r in ups.iterrows()],
                         "biggest_decreases": [{"name": str(r["name"]), "change": round(r.change, 2)} for _, r in downs.iterrows()]},
                   "comparison")


# ---------------------------------------------------------------- 2. quotation pipeline

def quotation_pipeline(months: int = 6) -> dict:
    """Quotations of the last months: open value, win rate (turned into an order), expired, and which to follow up."""
    months = max(1, min(int(months or 6), 36))
    t = _today()
    start = _months_ago(t.replace(day=1), months - 1)
    cur = _cur()
    q = query("""SELECT h.DocEntry, h.DocNum, h.DocDate, h.DocDueDate, h.CardCode, h.CardName, h.SlpCode,
                        h.DocTotal - h.VatSum AS Net, h.DocStatus,
                        CASE WHEN EXISTS (SELECT 1 FROM QUT1 l WHERE l.DocEntry = h.DocEntry AND l.TargetType = 17) THEN 1 ELSE 0 END AS Won
                 FROM OQUT h WHERE h.CANCELED = 'N' AND h.DocDate >= ? ORDER BY h.DocDate""", (start,))
    if q.empty:
        return {"note": f"No quotations in the last {months} months. Say so plainly."}
    q["Net"] = q.Net.astype(float)
    q["DocDate"], q["DocDueDate"] = pd.to_datetime(q.DocDate), pd.to_datetime(q.DocDueDate)
    slp = _salespeople()
    q["Salesperson"] = q.SlpCode.map(lambda c: slp.get(c, "-") if c and c > 0 else "-")
    q["Status"] = "Lost / closed"
    q.loc[q.Won == 1, "Status"] = "Won (ordered)"
    is_open = (q.DocStatus == "O") & (q.Won == 0)
    q.loc[is_open, "Status"] = "Open"
    q.loc[is_open & (q.DocDueDate.dt.date < t), "Status"] = "Open, expired"
    q["Age (days)"] = (pd.Timestamp(t) - q.DocDate).dt.days
    decided = q[q.Status.isin(["Won (ordered)", "Lost / closed"])]
    won_v, dec_v = float(decided[decided.Won == 1].Net.sum()), float(decided.Net.sum())
    win_rate = won_v / dec_v * 100 if dec_v else None
    win_count = len(decided[decided.Won == 1]) / len(decided) * 100 if len(decided) else None
    open_q = q[q.Status.str.startswith("Open")].sort_values("Net", ascending=False)
    status = q.groupby("Status").agg(n=("DocNum", "count"), value=("Net", "sum")).reindex(
        ["Open", "Open, expired", "Won (ordered)", "Lost / closed"]).fillna(0)
    by_slp = q.groupby("Salesperson").apply(lambda g: pd.Series({
        "Quotations": len(g), "Value": g.Net.sum(), "Won": g[g.Won == 1].Net.sum(), "Open": g[g.Status.str.startswith("Open")].Net.sum(),
        "Win rate %": (g[g.Won == 1].Net.sum() / g[g.Status.isin(['Won (ordered)', 'Lost / closed'])].Net.sum() * 100)
        if g[g.Status.isin(['Won (ordered)', 'Lost / closed'])].Net.sum() else None}), include_groups=False).reset_index()

    scope = f"last {months} months ({start:%d %b %Y} to {t:%d %b %Y})"
    out = [f"## Quotation pipeline: {scope}", f"{cur}, excluding tax", "",
           "| | Quotations | Value |", "|---|---:|---:|"]
    out += [f"| {s} | {int(r.n)} | {money(r.value, cur)} |" for s, r in status.iterrows()]
    out += [f"| **Win rate** (by value, of decided quotations) | {pct(win_count) if win_count is not None else '-'} of quotations | "
            f"**{pct(win_rate) if win_rate is not None else '-'}** |", ""]
    out.append(_hchart("Quotations by status", f"{cur} · {scope}", list(status.index), list(status.value),
                       [f"{money(v, cur)} · {int(n)}" for v, n in zip(status.value, status.n)],
                       [BLUE, "#e76500", GREEN, OTHER]) + "\n")
    if len(open_q):
        out += ["### Follow up first (open quotations, biggest first)"] + _md(
            open_q, {"DocNum": "No.", "CardName": "Customer", "Net": "Value", "DocDate": "Date", "DocDueDate": "Valid until",
                     "Age (days)": "Age (days)", "Salesperson": "Salesperson", "Status": "Status"}, ("Net",), cur)
    link = _excel(f"Quotation_pipeline_{t:%Y%m%d}", {
        "Status": status.reset_index().rename(columns={"index": "Status", "n": "Quotations", "value": "Value"}),
        "Open quotations": open_q[["DocNum", "CardCode", "CardName", "Net", "DocDate", "DocDueDate", "Age (days)", "Salesperson", "Status"]]
        .rename(columns={"DocNum": "No.", "CardCode": "Customer code", "CardName": "Customer", "Net": "Value", "DocDate": "Date", "DocDueDate": "Valid until"}),
        "By salesperson": by_slp,
        "All quotations": q[["DocNum", "DocDate", "CardName", "Net", "Status", "Salesperson"]].rename(
            columns={"DocNum": "No.", "DocDate": "Date", "CardName": "Customer", "Net": "Value"})},
        ("Value", "Won", "Open"), "Quotation pipeline", f"{scope} · {cur}, excluding tax",
        [{"label": f"Open ({cur})", "value": round(float(open_q.Net.sum()), 2), "note": f"{len(open_q)} quotations"},
         {"label": "Win rate (by value)", "value": round(win_rate, 1) if win_rate is not None else 0, "note": "won / decided"}],
        [{"sheet": "Status", "category": "Status", "value": "Value", "type": "bar", "title": "Quotations by status", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, "open_value": round(float(open_q.Net.sum()), 2), "open_count": len(open_q),
                         "expired_open": int((q.Status == "Open, expired").sum()),
                         "win_rate_by_value_pct": round(win_rate, 1) if win_rate is not None else None,
                         "win_rate_by_count_pct": round(win_count, 1) if win_count is not None else None,
                         "biggest_open": [{"no": int(r.DocNum), "customer": r.CardName, "value": round(r.Net, 2)} for _, r in open_q.head(5).iterrows()]},
                   "quotation pipeline")


# ---------------------------------------------------------------- 3. customer insights

def customer_insights(months: int = 12) -> dict:
    """Customers sorted into growing / steady / declining / at risk / new / lost (this period vs the one before), with
    how often they buy, when they last bought, and how many days late they pay on average."""
    months = max(3, min(int(months or 12), 24))
    t = _today()
    a = _months_ago(t.replace(day=1), months - 1)
    a0 = _months_ago(a, months)
    cur = _cur()
    lines = _sales_lines(a0, t)
    if lines.empty:
        return {"note": "No sales in this period. Say so plainly."}
    lines["now"] = lines.DocDate.dt.date >= a
    inv = lines[lines.Net > 0].groupby(["CardCode", "DocEntry"]).DocDate.min().reset_index()
    g = lines.groupby("CardCode").agg(Customer=("CardName", "first"))
    g["Sales now"] = lines[lines.now].groupby("CardCode").Net.sum()
    g["Sales before"] = lines[~lines.now].groupby("CardCode").Net.sum()
    g = g.fillna({"Sales now": 0.0, "Sales before": 0.0})
    g["Change %"] = ((g["Sales now"] - g["Sales before"]) / g["Sales before"].abs() * 100).where(g["Sales before"] != 0)
    g["Last invoice"] = inv.groupby("CardCode").DocDate.max()
    gaps = inv.sort_values("DocDate").groupby("CardCode").DocDate.apply(lambda s: s.diff().dt.days.median())
    g["Buys every (days)"] = gaps.round(0)
    g["Days since last"] = (pd.Timestamp(t) - g["Last invoice"]).dt.days
    pay = query("""SELECT i.CardCode, SUM(r.SumApplied) AS paid,
                          SUM(r.SumApplied * DATEDIFF(day, i.DocDueDate, p.DocDate)) AS weighted
                   FROM RCT2 r JOIN ORCT p ON p.DocEntry = r.DocNum JOIN OINV i ON i.DocEntry = r.DocEntry
                   WHERE r.InvType = 13 AND p.Canceled = 'N' AND p.DocDate >= ? GROUP BY i.CardCode""", (a,))
    pay = pay.set_index("CardCode")
    g["Pays days late (avg)"] = (pay.weighted.astype(float) / pay.paid.astype(float)).reindex(g.index).round(1)

    def segment(r):
        if r["Sales before"] <= 0 and r["Sales now"] > 0:
            return "New"
        if r["Sales now"] <= 0 < r["Sales before"]:
            return "Lost"
        usual = r["Buys every (days)"]
        if pd.notna(usual) and r["Days since last"] > max(2 * usual, 60):
            return "At risk (gone quiet)"
        if pd.notna(r["Change %"]) and r["Change %"] >= 20:
            return "Growing"
        if pd.notna(r["Change %"]) and r["Change %"] <= -20:
            return "Declining"
        return "Steady"
    g["Segment"] = g.apply(segment, axis=1)
    g = g[(g["Sales now"] != 0) | (g["Sales before"] != 0)].reset_index().sort_values("Sales now", ascending=False)
    order = ["Growing", "Steady", "New", "Declining", "At risk (gone quiet)", "Lost"]
    seg = g.groupby("Segment").agg(n=("CardCode", "count"), now=("Sales now", "sum"), before=("Sales before", "sum")).reindex(order).dropna(how="all").fillna(0)
    scope = f"last {months} months vs the {months} before"
    out = [f"## Customer insights: {scope}", f"{cur}, excluding tax, after credit memos · this period {a:%d %b %Y} to {t:%d %b %Y}", "",
           "| Segment | Customers | Sales this period | Sales the period before |", "|---|---:|---:|---:|"]
    out += [f"| {s} | {int(r.n)} | {money(r.now, cur)} | {money(r.before, cur)} |" for s, r in seg.iterrows()]
    out.append("")
    colour = {"Growing": GREEN, "Steady": BLUE, "New": "#5d36ff", "Declining": "#e76500", "At risk (gone quiet)": RED, "Lost": OTHER}
    out.append(_hchart("Customers by segment", f"Number of customers · {scope}", list(seg.index), list(seg.n),
                       [f"{int(n)} · {money(v, cur)} this period" for n, v in zip(seg.n, seg.now)], [colour[s] for s in seg.index]) + "\n")
    watch = g[g.Segment.isin(["At risk (gone quiet)", "Declining", "Lost"])].sort_values("Sales before", ascending=False)
    if len(watch):
        out += ["### Customers to call (biggest first)"] + _md(watch, {
            "Customer": "Customer", "Segment": "Segment", "Sales now": "This period", "Sales before": "Period before",
            "Last invoice": "Last invoice", "Buys every (days)": "Usually buys every (days)", "Pays days late (avg)": "Pays days late (avg)"},
            ("Sales now", "Sales before"), cur)
    link = _excel(f"Customer_insights_{t:%Y%m%d}", {
        "Segments": seg.reset_index().rename(columns={"index": "Segment", "n": "Customers", "now": "Sales this period", "before": "Sales period before"}),
        "Customers": g.rename(columns={"CardCode": "Customer code", "Sales now": "Sales this period", "Sales before": "Sales period before"})},
        ("Sales this period", "Sales period before"), "Customer insights", f"{scope} · {cur}",
        [{"label": "Customers buying", "value": int((g["Sales now"] > 0).sum()), "note": f"{int((g.Segment == 'New').sum())} new"},
         {"label": "To call", "value": len(watch), "note": "declining, gone quiet or lost"}],
        [{"sheet": "Segments", "category": "Segment", "value": "Customers", "type": "bar", "title": "Customers by segment", "subtitle": scope}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, "segments": {s: {"customers": int(r.n), "sales_now": round(r.now, 2), "sales_before": round(r.before, 2)} for s, r in seg.iterrows()},
                         "to_call": [{"customer": r.Customer, "segment": r.Segment, "sales_now": round(r["Sales now"], 2),
                                      "sales_before": round(r["Sales before"], 2), "days_since_last": int(r["Days since last"]) if pd.notna(r["Days since last"]) else None}
                                     for _, r in watch.head(5).iterrows()],
                         "slowest_payers": [{"customer": r.Customer, "pays_days_late_avg": r["Pays days late (avg)"]}
                                            for _, r in g.dropna(subset=["Pays days late (avg)"]).sort_values("Pays days late (avg)", ascending=False).head(3).iterrows()]},
                   "customer insights")


# ---------------------------------------------------------------- 4. product profitability

def product_profitability(months: int = 12, by: str = "item") -> dict:
    """Margin per item (or item group), ABC ranking by net sales, and items whose price or margin is slipping."""
    months = max(3, min(int(months or 12), 36))
    t = _today()
    a = _months_ago(t.replace(day=1), months - 1)
    recent = _months_ago(t.replace(day=1), 2)
    cur = _cur()
    s = _sales_lines(a, t)
    if s.empty:
        return {"note": "No sales in this period. Say so plainly."}
    key = "ItemGroup" if "group" in (by or "") else "ItemName"
    s["recent"] = s.DocDate.dt.date >= recent
    g = s.groupby(key).agg(Code=("ItemCode", "first"), Qty=("Qty", "sum"), Net=("Net", "sum"), GP=("GP", "sum"))
    g = g[g.Net != 0].sort_values("Net", ascending=False)
    g["Margin %"] = (g.GP / g.Net * 100).round(1)
    total = g.Net.clip(lower=0).sum()
    g["Share %"] = (g.Net / total * 100).round(1) if total else 0
    cum = g.Net.clip(lower=0).cumsum() / total * 100 if total else g.Net * 0
    g["ABC"] = ["A" if c <= 80 or i == 0 else "B" if c <= 95 else "C" for i, c in enumerate(cum)]
    r_ = s[s.recent].groupby(key).agg(n=("Net", "sum"), gp=("GP", "sum"), q=("Qty", "sum"))
    o_ = s[~s.recent].groupby(key).agg(n=("Net", "sum"), gp=("GP", "sum"), q=("Qty", "sum"))
    g["Margin last 3 months %"] = (r_.gp / r_.n * 100).round(1).reindex(g.index)
    g["Margin before %"] = (o_.gp / o_.n * 100).round(1).reindex(g.index)
    g["Price last 3 months"] = (r_.n / r_.q).where(r_.q > 0).reindex(g.index)
    g["Price before"] = (o_.n / o_.q).where(o_.q > 0).reindex(g.index)
    slipping = g[((g["Margin last 3 months %"] < g["Margin before %"] - 5) |
                  (g["Price last 3 months"] < g["Price before"] * 0.95)) & g["Margin last 3 months %"].notna()].copy()
    g = g.reset_index().rename(columns={key: "Item group" if key == "ItemGroup" else "Item"})
    name_col = g.columns[0]
    abc = g.groupby("ABC").agg(n=(name_col, "count"), net=("Net", "sum"), gp=("GP", "sum")).reindex(["A", "B", "C"]).fillna(0)
    scope = f"last {months} months ({a:%d %b %Y} to {t:%d %b %Y})"
    what = "item group" if key == "ItemGroup" else "item"
    out = [f"## Product profitability by {what}: {scope}", f"{cur}, excluding tax, after credit memos", "",
           "| Class | Meaning | " + ("Groups" if key == "ItemGroup" else "Items") + " | Net sales | Gross profit | Margin |", "|---|---|---:|---:|---:|---:|"]
    meaning = {"A": "top sellers: 80% of sales", "B": "next 15% of sales", "C": "last 5% of sales"}
    out += [f"| {c} | {meaning[c]} | {int(r.n)} | {money(r.net, cur)} | {money(r.gp, cur)} | {pct(r.gp / r.net * 100) if r.net else '-'} |"
            for c, r in abc.iterrows()]
    out.append("")
    top = g.head(TOP)
    out.append(_hchart(f"Top {len(top)} {what}s by gross profit" if len(g) > TOP else f"Gross profit by {what}",
                       f"{cur} · {scope} · label = gross profit · margin", list(top[name_col]), list(top.GP),
                       [f"{money(v, cur)} · {pct(m)}" for v, m in zip(top.GP, top["Margin %"])],
                       [GREEN if m >= 20 else BLUE if m >= 0 else RED for m in top["Margin %"]]) + "\n")
    out += [f"### {what.capitalize()}s"] + _md(g, {name_col: what.capitalize(), "ABC": "Class", "Net": "Net sales", "GP": "Gross profit",
                                                   "Margin %": "Margin", "Share %": "Share"}, ("Net", "GP"), cur, ("Margin %", "Share %"))
    if len(slipping):
        sl = slipping.reset_index().rename(columns={key: name_col})
        out += ["", "### Price or margin slipping (last 3 months vs before)"] + _md(sl, {
            name_col: what.capitalize(), "Margin before %": "Margin before", "Margin last 3 months %": "Margin last 3 months",
            "Price before": "Avg price before", "Price last 3 months": "Avg price last 3 months"},
            ("Price before", "Price last 3 months"), cur, ("Margin before %", "Margin last 3 months %"))
    link = _excel(f"Product_profitability_{t:%Y%m%d}", {
        "ABC": abc.reset_index().rename(columns={"index": "Class", "n": "Count", "net": "Net sales", "gp": "Gross profit"}),
        name_col + "s": g.rename(columns={"Net": "Net sales", "GP": "Gross profit"}),
        "Slipping": slipping.reset_index().rename(columns={key: name_col})},
        ("Net sales", "Gross profit", "Price before", "Price last 3 months"), "Product profitability", f"{scope} · {cur}",
        [{"label": f"Gross profit ({cur})", "value": round(float(g.GP.sum()), 2), "note": f"{pct(g.GP.sum() / g.Net.sum() * 100) if g.Net.sum() else '-'} margin"},
         {"label": "A items", "value": int(abc.loc["A", "n"]), "note": "make 80% of sales"}],
        [{"sheet": name_col + "s", "category": name_col, "value": "Gross profit", "type": "bar", "top": 10, "title": "Gross profit", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, "abc": {c: {"count": int(r.n), "net_sales": round(r.net, 2)} for c, r in abc.iterrows()},
                         "top": [{"name": r[name_col], "net_sales": round(r.Net, 2), "gross_profit": round(r.GP, 2), "margin_pct": r["Margin %"]} for _, r in g.head(5).iterrows()],
                         "lowest_margin": [{"name": r[name_col], "margin_pct": r["Margin %"]} for _, r in g[g.Net > 0].sort_values("Margin %").head(3).iterrows()],
                         "slipping": [str(x) for x in slipping.index[:5]]}, "product profitability")


# ---------------------------------------------------------------- 5. salespeople

def salesperson_performance(months: int = 3) -> dict:
    """Per salesperson: net sales, gross profit, margin, customers, quotation win rate, against the period before."""
    months = max(1, min(int(months or 3), 24))
    t = _today()
    a = _months_ago(t.replace(day=1), months - 1)
    a0 = _months_ago(a, months)
    cur = _cur()
    slp = _salespeople()
    s = _sales_lines(a0, t)
    if s.empty:
        return {"note": "No sales in this period. Say so plainly."}
    s["Salesperson"] = s.SlpCode.map(lambda c: slp.get(c, "No salesperson") if c and c > 0 else "No salesperson")
    now, before = s[s.DocDate.dt.date >= a], s[s.DocDate.dt.date < a]
    g = now.groupby("Salesperson").agg(Net=("Net", "sum"), GP=("GP", "sum"), Customers=("CardCode", "nunique"))
    g["Margin %"] = (g.GP / g.Net * 100).round(1)
    g["Net before"] = before.groupby("Salesperson").Net.sum().reindex(g.index).fillna(0)
    g["Change %"] = ((g.Net - g["Net before"]) / g["Net before"].abs() * 100).where(g["Net before"] != 0).round(1)
    q = query("""SELECT h.SlpCode, h.DocTotal - h.VatSum AS Net, h.DocStatus,
                        CASE WHEN EXISTS (SELECT 1 FROM QUT1 l WHERE l.DocEntry = h.DocEntry AND l.TargetType = 17) THEN 1 ELSE 0 END AS Won
                 FROM OQUT h WHERE h.CANCELED = 'N' AND h.DocDate >= ?""", (a,))
    if len(q):
        q["Salesperson"] = q.SlpCode.map(lambda c: slp.get(c, "No salesperson") if c and c > 0 else "No salesperson")
        q["Net"] = q.Net.astype(float)
        dec = q[(q.Won == 1) | (q.DocStatus == "C")]
        g["Quote win rate %"] = (dec[dec.Won == 1].groupby("Salesperson").Net.sum() / dec.groupby("Salesperson").Net.sum() * 100).round(1).reindex(g.index)
        g["Open quotations"] = q[(q.DocStatus == "O") & (q.Won == 0)].groupby("Salesperson").Net.sum().reindex(g.index).fillna(0)
    g = g.sort_values("Net", ascending=False).reset_index()
    scope = f"last {months} month{'s' if months > 1 else ''} ({a:%d %b %Y} to {t:%d %b %Y}) vs the {months} before"
    out = [f"## Salesperson performance: {scope}", f"{cur}, excluding tax, after credit memos", ""]
    out.append(_hchart("Net sales by salesperson", f"{cur} · {scope} · % = change", list(g.Salesperson), list(g.Net),
                       [f"{money(v, cur)}" + (f" · {c:+.1f}%" if pd.notna(c) else "") for v, c in zip(g.Net, g["Change %"])]) + "\n")
    cols = {"Salesperson": "Salesperson", "Net": "Net sales", "Net before": "Period before", "Change %": "Change", "GP": "Gross profit",
            "Margin %": "Margin", "Customers": "Customers"}
    if "Quote win rate %" in g:
        cols.update({"Quote win rate %": "Quote win rate", "Open quotations": "Open quotations"})
    out += _md(g, cols, ("Net", "Net before", "GP", "Open quotations"), cur, ("Change %", "Margin %", "Quote win rate %"))
    link = _excel(f"Salesperson_performance_{t:%Y%m%d}", {"Salespeople": g.rename(columns={"Net": "Net sales", "GP": "Gross profit", "Net before": "Net sales before"})},
                  ("Net sales", "Gross profit", "Net sales before", "Open quotations"), "Salesperson performance", f"{scope} · {cur}",
                  [{"label": f"Net sales ({cur})", "value": round(float(g.Net.sum()), 2), "note": f"{len(g)} salespeople"}],
                  [{"sheet": "Salespeople", "category": "Salesperson", "value": "Net sales", "type": "bar", "title": "Net sales", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, "salespeople": [{"name": r.Salesperson, "net_sales": round(r.Net, 2), "change_pct": r["Change %"] if pd.notna(r["Change %"]) else None,
                                                           "margin_pct": r["Margin %"], "quote_win_rate_pct": r.get("Quote win rate %") if pd.notna(r.get("Quote win rate %")) else None}
                                                          for _, r in g.iterrows()]}, "salesperson performance")


# ---------------------------------------------------------------- 6. delivery performance

def delivery_performance(months: int = 6) -> dict:
    """Deliveries made from sales orders: on time (by the order line's delivery date) or late, days from order to delivery,
    and order lines that are already late and not delivered (back orders)."""
    months = max(1, min(int(months or 6), 24))
    t = _today()
    a = _months_ago(t.replace(day=1), months - 1)
    cur = _cur()
    d = query("""SELECT dh.DocDate AS Delivered, o.DocDate AS Ordered, ol.ShipDate AS Promised, dh.CardName, dl.LineTotal
                 FROM DLN1 dl JOIN ODLN dh ON dh.DocEntry = dl.DocEntry
                 JOIN RDR1 ol ON ol.DocEntry = dl.BaseEntry AND ol.LineNum = dl.BaseLine JOIN ORDR o ON o.DocEntry = ol.DocEntry
                 WHERE dl.BaseType = 17 AND dh.CANCELED = 'N' AND dh.DocDate >= ?""", (a,))
    late = query("""SELECT o.DocNum, o.CardName, l.ItemCode, l.Dscription, l.OpenQty, l.ShipDate, l.Price * l.OpenQty AS OpenValue
                    FROM RDR1 l JOIN ORDR o ON o.DocEntry = l.DocEntry
                    WHERE o.CANCELED = 'N' AND l.LineStatus = 'O' AND l.OpenQty > 0 AND l.ShipDate < ? ORDER BY l.ShipDate""", (t,))
    if d.empty and late.empty:
        return {"note": f"No deliveries from sales orders in the last {months} months and nothing late. Say so plainly."}
    for c in ("Delivered", "Ordered", "Promised"):
        d[c] = pd.to_datetime(d[c])
    d["LineTotal"] = d.LineTotal.astype(float)
    d["Days late"] = (d.Delivered - d.Promised).dt.days
    d["On time"] = d["Days late"] <= 0
    d["Lead days"] = (d.Delivered - d.Ordered).dt.days
    on_time = d["On time"].mean() * 100 if len(d) else None
    d["Month"] = d.Delivered.dt.strftime("%b %Y")
    month = d.groupby(d.Delivered.dt.to_period("M")).agg(on_time=("On time", "mean"), lines=("On time", "size"))
    month["on_time"] *= 100
    by_c = d.groupby("CardName").agg(lines=("On time", "size"), on_time=("On time", "mean"), late_days=("Days late", lambda x: x[x > 0].mean())).sort_values("lines", ascending=False)
    by_c["on_time"] *= 100
    late["ShipDate"] = pd.to_datetime(late.ShipDate)
    late["Days overdue"] = (pd.Timestamp(t) - late.ShipDate).dt.days
    late["OpenValue"] = late.OpenValue.astype(float)
    late["OpenQty"] = late.OpenQty.astype(float)
    scope = f"last {months} months ({a:%d %b %Y} to {t:%d %b %Y})"
    out = [f"## Delivery performance: {scope}", "Deliveries made from sales orders, by order line", "",
           "| | |", "|---|---:|",
           f"| Order lines delivered | {len(d):,} |",
           f"| **Delivered on time** | **{pct(on_time) if on_time is not None else '-'}** |",
           f"| Average days late (late lines) | {d[d['Days late'] > 0]['Days late'].mean():.1f} |" if (d['Days late'] > 0).any() else "| Average days late (late lines) | - |",
           f"| Average days from order to delivery | {d['Lead days'].mean():.1f} |" if len(d) else "| Average days from order to delivery | - |",
           f"| Order lines late and not delivered | {len(late):,} ({money(float(late.OpenValue.sum()), cur)}) |", ""]
    if len(month):
        out.append(_vchart_pct("On-time delivery by month", f"% of order lines delivered by their delivery date · {scope}",
                               [f"{p:%b %y}" for p in month.index], list(month.on_time)) + "\n")
    if len(late):
        out += ["### Late and not yet delivered (oldest first)"] + _md(late, {
            "DocNum": "Order", "CardName": "Customer", "Dscription": "Item", "OpenQty": "Qty open", "ShipDate": "Due",
            "Days overdue": "Days overdue", "OpenValue": "Value"}, ("OpenValue",), cur)
    link = _excel(f"Delivery_performance_{t:%Y%m%d}", {
        "By month": month.reset_index().assign(Delivered=lambda x: x.Delivered.astype(str)).rename(columns={"Delivered": "Month", "on_time": "On time %", "lines": "Lines"}),
        "By customer": by_c.reset_index().rename(columns={"CardName": "Customer", "lines": "Lines", "on_time": "On time %", "late_days": "Avg days late"}),
        "Late open lines": late.rename(columns={"DocNum": "Order", "CardName": "Customer", "Dscription": "Item", "OpenQty": "Qty open", "ShipDate": "Due", "OpenValue": "Value"})},
        ("Value",), "Delivery performance", scope,
        [{"label": "On time", "value": round(on_time, 1) if on_time is not None else 0, "note": f"{len(d):,} lines"},
         {"label": "Late, not delivered", "value": len(late), "note": money(float(late.OpenValue.sum()), cur)}],
        [{"sheet": "By month", "category": "Month", "value": "On time %", "title": "On-time %", "subtitle": scope}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, "on_time_pct": round(on_time, 1) if on_time is not None else None, "lines_delivered": len(d),
                         "late_open_lines": len(late), "late_open_value": round(float(late.OpenValue.sum()), 2),
                         "worst_customers": [{"customer": c, "on_time_pct": round(r.on_time, 1)} for c, r in by_c[by_c.lines >= 3].sort_values("on_time").head(3).iterrows()]},
                   "delivery performance")


def _vchart_pct(title: str, subtitle: str, names: list[str], values: list[float]) -> str:
    fig, ax = _figure(3.4, title, subtitle)
    x = range(len(names))
    ax.bar(x, values, color=[GREEN if v >= 90 else BLUE if v >= 70 else RED for v in values], width=0.62)
    ax.set_xticks(list(x), names)
    ax.tick_params(axis="x", colors="#556b82", labelsize=8.5)
    ax.yaxis.set_visible(False)
    ax.set_ylim(0, 115)
    for i, v in enumerate(values):
        ax.annotate(pct(v), (i, v), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=9, color="#1d2d3e")
    return _image(fig, title)


# ---------------------------------------------------------------- 7. inventory health

def inventory_health() -> dict:
    """Stock value, how fast it turns, days of stock left at the current rate, and items that have not moved."""
    t = _today()
    a = t - dt.timedelta(days=365)
    cur = _cur()
    st = query("""SELECT w.ItemCode, i.ItemName, ISNULL(g.ItmsGrpNam, '(no group)') AS ItemGroup, SUM(w.OnHand) AS OnHand,
                         MAX(CASE WHEN i.AvgPrice > 0 THEN i.AvgPrice ELSE i.LastPurPrc END) AS Cost
                  FROM OITW w JOIN OITM i ON i.ItemCode = w.ItemCode LEFT JOIN OITB g ON g.ItmsGrpCod = i.ItmsGrpCod
                  WHERE i.InvntItem = 'Y' GROUP BY w.ItemCode, i.ItemName, g.ItmsGrpNam HAVING SUM(w.OnHand) > 0""")
    if st.empty:
        return {"note": "No stock on hand. Say so plainly."}
    mv = query("""SELECT ItemCode, SUM(CASE WHEN TransType IN (13, 15, 60) AND DocDate >= ? THEN OutQty ELSE 0 END) AS Used,
                         MAX(CASE WHEN OutQty > 0 AND TransType IN (13, 15, 60) THEN DocDate END) AS LastOut,
                         MAX(CASE WHEN InQty > 0 THEN DocDate END) AS LastIn
                  FROM OINM GROUP BY ItemCode""", (a,)).set_index("ItemCode")
    st = st.set_index("ItemCode")
    st = st.join(mv, how="left")
    for c in ("OnHand", "Cost", "Used"):
        st[c] = st[c].astype(float).fillna(0)
    st["Value"] = st.OnHand * st.Cost
    st["LastOut"], st["LastIn"] = pd.to_datetime(st.LastOut), pd.to_datetime(st.LastIn)
    st["Days of stock"] = (st.OnHand / (st.Used / 365)).where(st.Used > 0).round(0)
    st["Turns a year"] = (st.Used / st.OnHand).round(1)
    st["Days since moved out"] = (pd.Timestamp(t) - st.LastOut).dt.days

    def status(r):
        if r.Used <= 0:
            return "Not moving (no use in 12 months)"
        if r["Days of stock"] > 365:
            return "Overstock (over a year)"
        if r["Days of stock"] > 180:
            return "Slow (6-12 months)"
        return "Healthy (under 6 months)"
    st["Status"] = st.apply(status, axis=1)
    order = ["Healthy (under 6 months)", "Slow (6-12 months)", "Overstock (over a year)", "Not moving (no use in 12 months)"]
    agg = st.groupby("Status").agg(n=("OnHand", "size"), value=("Value", "sum")).reindex(order).dropna(how="all").fillna(0)
    total = float(st.Value.sum())
    turns = float((st.Used * st.Cost).sum() / total) if total else None
    out = ["## Inventory health", f"Stock on hand now, valued at item cost ({cur}); use = sold, delivered or issued in the last 12 months", "",
           "| | |", "|---|---:|", f"| Stock value | {money(total, cur)} |", f"| Items in stock | {len(st):,} |",
           f"| Stock turns a year (overall) | {turns:.2f} |" if turns is not None else "| Stock turns a year (overall) | - |",
           f"| Value not moving in 12 months | {money(float(agg.loc[order[3], 'value']) if order[3] in agg.index else 0.0, cur)} |", ""]
    out.append(_hchart("Stock value by health", f"{cur} · % = share of {money(total, cur)}", list(agg.index), list(agg.value),
                       [f"{money(v, cur)} · {pct(v / total * 100) if total else '-'} · {int(n)} items" for v, n in zip(agg.value, agg.n)],
                       [GREEN, BLUE, "#e76500", RED][:len(agg)] if list(agg.index) == order[:len(agg)] else BLUE) + "\n")
    worst = st[st.Status != order[0]].sort_values("Value", ascending=False).reset_index()
    if len(worst):
        out += ["### Most money tied up in slow or idle stock"] + _md(worst, {
            "ItemName": "Item", "OnHand": "On hand", "Value": "Value", "Days of stock": "Days of stock",
            "Days since moved out": "Days since last use", "Status": "Status"}, ("Value",), cur)
    sheet = st.reset_index().rename(columns={"ItemCode": "Item code", "ItemName": "Item", "ItemGroup": "Item group", "OnHand": "On hand",
                                             "Used": "Used in 12 months", "LastOut": "Last used", "LastIn": "Last received"})
    link = _excel(f"Inventory_health_{t:%Y%m%d}", {"Health": agg.reset_index().rename(columns={"index": "Status", "n": "Items", "value": "Value"}),
                                                    "Items": sheet.sort_values("Value", ascending=False),
                                                    "By item group": sheet.groupby("Item group").agg(Items=("Item", "count"), Value=("Value", "sum")).reset_index()},
                  ("Value", "Cost"), "Inventory health", f"Stock value at item cost · {cur}",
                  [{"label": f"Stock value ({cur})", "value": round(total, 2), "note": f"{len(st):,} items"},
                   {"label": "Turns a year", "value": round(turns, 1) if turns else 0, "note": "use ÷ stock value"}],
                  [{"sheet": "Health", "category": "Status", "value": "Value", "type": "bar", "title": "Stock value by health", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"stock_value": round(total, 2), "turns_a_year": round(turns, 1) if turns else None,
                         "by_status": {s: {"items": int(r.n), "value": round(r.value, 2)} for s, r in agg.iterrows()},
                         "biggest_idle": [{"item": r.ItemName, "value": round(r.Value, 2), "status": r.Status} for _, r in worst.head(5).iterrows()]},
                   "inventory health")


# ---------------------------------------------------------------- 8. profit and loss

GROUPS = {4: "Revenue", 5: "Cost of sales", 6: "Operating expenses", 7: "Financing", 8: "Other revenue and expenses"}


def profit_and_loss(months: int = 12) -> dict:
    """Monthly profit and loss from the posted journal entries (revenue, cost of sales, expenses, profit), with budget vs
    actual from B1's main budget scenario where one exists."""
    months = max(1, min(int(months or 12), 24))
    t = _today()
    a = _months_ago(t.replace(day=1), months - 1)
    cur = _cur()
    j = query("""SELECT YEAR(j.RefDate) AS y, MONTH(j.RefDate) AS m, a.GroupMask, a.AcctCode, a.AcctName,
                        SUM(j.Credit - j.Debit) AS Amount
                 FROM JDT1 j JOIN OACT a ON a.AcctCode = j.Account
                 WHERE a.GroupMask BETWEEN 4 AND 8 AND j.RefDate >= ? AND j.RefDate < ?
                 GROUP BY YEAR(j.RefDate), MONTH(j.RefDate), a.GroupMask, a.AcctCode, a.AcctName""", (a, t + dt.timedelta(days=1)))
    if j.empty:
        return {"note": "No postings to profit and loss accounts in this period. Say so plainly."}
    j["Amount"] = j.Amount.astype(float)
    j["Month"] = pd.to_datetime(dict(year=j.y, month=j.m, day=1)).dt.to_period("M")
    months_idx = pd.period_range(a, t, freq="M")
    piv = j.pivot_table(index="GroupMask", columns="Month", values="Amount", aggfunc="sum").reindex(columns=months_idx, fill_value=0).fillna(0)
    row = lambda g: piv.loc[g] if g in piv.index else pd.Series(0.0, index=months_idx)
    rev, cos, opx, fin, oth = row(4), row(5), row(6), row(7), row(8)
    gp, op, net = rev + cos, rev + cos + opx, rev + cos + opx + fin + oth
    lines = pd.DataFrame({"Revenue": rev, "Cost of sales": cos, "Gross profit": gp, "Operating expenses": opx,
                          "Operating profit": op, "Financing": fin, "Other revenue and expenses": oth, "Net profit": net}).T
    lines.columns = [f"{p:%b %Y}" for p in months_idx]
    lines["Total"] = lines.sum(axis=1)
    budget = None
    try:
        b = query("""SELECT a.GroupMask, b.FinancYear, l.Line_ID, SUM(l.CredLTotal - l.DebLTotal) AS Budget
                     FROM BGT1 l JOIN OBGT b ON b.AbsId = l.BudgId JOIN OBGS s ON s.AbsId = b.Instance AND s.IsMain = 'Y'
                     JOIN OACT a ON a.AcctCode = b.AcctCode WHERE a.GroupMask BETWEEN 4 AND 8
                     GROUP BY a.GroupMask, b.FinancYear, l.Line_ID""")
        if len(b):
            b["Month"] = [(pd.Timestamp(fy) + pd.DateOffset(months=int(li))).to_period("M") for fy, li in zip(b.FinancYear, b.Line_ID)]
            bp = b.pivot_table(index="GroupMask", columns="Month", values="Budget", aggfunc="sum").reindex(columns=months_idx).fillna(0)
            if bp.to_numpy().any():
                budget = bp.sum(axis=0).astype(float)  # budgeted net profit per month
    except Exception:
        log.exception("Budget lookup failed")
    labels_ = [f"{p:%b %y}" for p in months_idx]
    scope = f"{a:%b %Y} to {t:%b %Y}" + (" (this month to date)" if t.day < 28 else "")
    out = [f"## Profit and loss: {scope}", f"{cur}, from posted journal entries", ""]
    tot = lines["Total"]
    out += ["| | Total |" + (" Budget |" if budget is not None else ""), "|---|---:|" + ("---:|" if budget is not None else "")]
    for name in lines.index:
        bold = name in ("Gross profit", "Operating profit", "Net profit")
        v = money(tot[name], cur)
        extra = f" {money(float(budget.sum()), cur)} |" if budget is not None and name == "Net profit" else (" |" if budget is not None else "")
        out.append(f"| {'**' + name + '**' if bold else name} | {'**' + v + '**' if bold else v} |" + extra)
    out.append("")
    out.append(_vchart("Net profit by month", f"{cur} · {scope}" + (" · line = budget" if budget is not None else ""),
                       labels_, list(net.values.astype(float)), cur, line=list(budget.values) if budget is not None else None) + "\n")
    out.append(_vchart("Revenue by month", f"{cur} · {scope}", labels_, list(rev.values.astype(float)), cur,
                       colours=[BLUE] * len(labels_)) + "\n")
    exp = j[j.GroupMask.isin([5, 6])].groupby("AcctName").Amount.sum().sort_values().head(TOP)
    if len(exp):
        out.append(_hchart("Biggest costs", f"{cur} · {scope} · cost of sales and expenses", list(exp.index), list(-exp.values),
                           [money(-v, cur) for v in exp.values], RED) + "\n")
    accounts = (j.groupby(["GroupMask", "AcctCode", "AcctName"]).Amount.sum().reset_index()
                .assign(Group=lambda x: x.GroupMask.map(GROUPS)).drop(columns="GroupMask")
                .rename(columns={"AcctCode": "Account", "AcctName": "Name"}))
    sheets = {"P&L by month": lines.reset_index().rename(columns={"index": "Line"}), "Accounts": accounts}
    if budget is not None:
        sheets["Budget vs actual"] = pd.DataFrame({"Month": labels_, "Net profit": net.values.astype(float), "Budget": budget.values,
                                                   "Difference": net.values.astype(float) - budget.values})
    link = _excel(f"Profit_and_loss_{t:%Y%m%d}", sheets, tuple(lines.columns) + ("Amount", "Net profit", "Budget", "Difference"),
                  "Profit and loss", f"{scope} · {cur}",
                  [{"label": f"Revenue ({cur})", "value": round(float(tot['Revenue']), 2), "note": scope},
                   {"label": f"Net profit ({cur})", "value": round(float(tot['Net profit']), 2),
                    "note": f"budget {money(float(budget.sum()), cur)}" if budget is not None else "no budget in B1"}],
                  [{"sheet": "P&L by month", "category": "Line", "value": "Total", "type": "bar", "title": "P&L totals", "subtitle": cur}])
    out[1:1] = ["", link]
    return _result(out, {"period": scope, **{k.lower().replace(" ", "_"): round(float(v), 2) for k, v in tot.items()},
                         "budget_net_profit": round(float(budget.sum()), 2) if budget is not None else None,
                         "biggest_costs": [{"account": n, "amount": round(-float(v), 2)} for n, v in exp.head(5).items()]},
                   "profit and loss")
