"""Charts for the chat, drawn from the synced data (never from numbers typed by the model).

Each tool returns {"_display": <markdown image>, "summary": ...}. The agent shows the image to the user directly and
gives the model only the summary, so the image is never re-typed by the model.

One house style for every chart, so they read as one set: a headline that says what the chart shows, a grey line
saying what the numbers are (period, unit, currency), one blue for the data, hairline grid, short numbers (1.2M,
not 1e6), names written flat (horizontal bars for long names) and values printed only where they help.
Each question has its own chart; custom_chart is the last resort for figures from another tool.
"""

import base64
import io
import json
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .numbers import label, money, pct, qty  # noqa: E402
from .sync import DATA_DIR  # noqa: E402

# SAP Fiori Horizon colours (as the chat site); text never wears a data colour.
BLUE, LIGHT_BLUE, RED, ORANGE, GREEN = "#0070f2", "#9cc6fb", "#d20a0a", "#e76500", "#30914c"
TEXT, MUTED, GRID, OTHER = "#1d2d3e", "#556b82", "#e4e8ec", "#b8c2cc"
STATUS_COLOURS = {"LINE STOP RISK": RED, "STOCKOUT RISK": ORANGE, "REORDER": BLUE, "OVERSTOCK": "#8b47d7",
                  "DEAD STOCK": "#788fa6", "OK": GREEN}
STATUS_MEANING = {"LINE STOP RISK": "can stop production", "STOCKOUT RISK": "may run out", "REORDER": "order now",
                  "OVERSTOCK": "more than needed", "DEAD STOCK": "not sold in a long time", "OK": "fine"}
AGE_COLOURS = ["#9cb3c9", "#f7b9a8", "#ee8a6f", "#d9583b", "#a8321d"]  # not due yet, then one red getting darker with age

plt.rcParams.update({"font.size": 9, "text.color": TEXT, "axes.labelcolor": MUTED, "xtick.color": MUTED,
                     "ytick.color": TEXT, "axes.edgecolor": GRID, "font.family": "DejaVu Sans",
                     "text.parse_math": False})  # "$" is a currency here, not a formula
WIDTH = 9.0  # inches; about 1000 px wide in the chat


# ---------- shared pieces ----------

def _meta() -> dict:
    return json.loads((DATA_DIR / "sync_meta.json").read_text())


def _currency() -> str:
    return label(_meta().get("currency") or "")


def _synced() -> pd.Timestamp:
    return pd.Timestamp(_meta().get("synced_at") or pd.Timestamp.today()).normalize()


def _axis(v: float, cur: str = "") -> str:
    """Axis steps are round numbers (RM 15,000); every figure written on a chart is exact (numbers.money)."""
    return money(v, cur, decimals=0)


def _figure(height: float, title: str, subtitle: str, left: float = 0.08):
    """A figure with the headline and its grey explanation line above the plot, both flush left."""
    fig, ax = plt.subplots(figsize=(WIDTH, height))
    top = 1 - 0.85 / height
    fig.subplots_adjust(left=left, right=0.97, top=top, bottom=0.55 / height + 0.05)
    fig.text(0.012, 1 - 0.28 / height, title, fontsize=13.5, fontweight="bold", color=TEXT, va="center")
    fig.text(0.012, 1 - 0.58 / height, subtitle, fontsize=9.5, color=MUTED, va="center")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0, pad=6)
    ax.set_axisbelow(True)
    return fig, ax


def _bars_left(names: list[str]) -> float:
    """Room on the left for horizontal-bar names, so they are written flat and never cut."""
    longest = max((len(n) for n in names), default=10)
    return min(0.42, (longest * 0.068 + 0.35) / WIDTH)


def _image(fig, title: str) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight", pad_inches=0.18, facecolor="white")
    plt.close(fig)
    return f"![{title}](data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()})"


def _hbar(ax, names: list[str], values: list[float], labels: list[str], colours: list[str] | str = BLUE) -> None:
    """Horizontal bars, biggest at the top, each with its value written at the tip; no value axis (it would repeat)."""
    y = list(range(len(names)))[::-1]
    ax.barh(y, values, color=colours, height=0.62)
    ax.set_yticks(y, names)
    ax.set_ylim(len(names) - 0.4 - max(len(names), 3), len(names) - 0.4)  # few bars keep the normal thickness
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.axvline(0, color=GRID, linewidth=1)
    hi, lo = max(values + [0]), min(values + [0])
    span = (hi - lo) or 1
    ax.set_xlim(lo - (span * 0.02 if lo < 0 else 0), hi + span * 0.5)
    for yi, v, text in zip(y, values, labels):
        ax.annotate(text, (max(v, 0), yi), xytext=(5, 0), textcoords="offset points", va="center", fontsize=9, color=TEXT)


def _month_ticks(ax, months: pd.PeriodIndex) -> None:
    """Months written flat: 'Oct' under each bar, the year only under January and the first bar."""
    labels = [f"{p:%b}\n{p:%Y}" if i == 0 or p.month == 1 else f"{p:%b}" for i, p in enumerate(months)]
    ax.set_xticks(range(len(months)), labels)
    ax.tick_params(axis="x", colors=MUTED)


def _ar_lines(months: int, item_code: str = "") -> tuple[pd.DataFrame, pd.PeriodIndex, pd.Timestamp]:
    """Invoice lines of the last `months` months (up to the last sync), net of document discounts, with gross profit."""
    ar = pd.read_parquet(DATA_DIR / "ar_lines.parquet")
    ar["DocDate"] = pd.to_datetime(ar.DocDate)
    if item_code:
        ar = ar[ar.ItemCode.str.upper() == item_code.upper()]
    disc = ar.DocDisc.fillna(0) / 100
    ar = ar.assign(revenue=ar.LineTotal * (1 - disc), gross_profit=ar.GrssProfit - ar.LineTotal * disc)
    synced = _synced()
    end = synced.to_period("M")
    idx = pd.period_range(end - months + 1, end, freq="M")
    ar = ar[(ar.DocDate.dt.to_period("M") >= idx[0]) & (ar.DocDate <= synced + pd.Timedelta(days=1))]
    return ar, idx, synced


# ---------- the charts ----------

def sales_trend_chart(months: int = 12, item_code: str = "", measure: str = "sales") -> dict:
    """Net sales, gross profit or gross margin per month."""
    months = max(3, min(int(months or 12), 36))
    measure = (measure or "sales").lower()
    measure = "profit" if "profit" in measure else "margin" if "margin" in measure else "sales"
    ar, idx, synced = _ar_lines(months, item_code)
    if item_code and ar.empty:
        return {"error": f"No invoiced sales of {item_code} in the last {months} months."}
    g = ar.groupby(ar.DocDate.dt.to_period("M")).agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum")).reindex(idx, fill_value=0)
    g["margin"] = (g.gp / g.revenue.where(g.revenue != 0) * 100)
    cur = _currency()
    started = ""
    active = g.index[(g.revenue != 0) | (g.gp != 0)]
    if len(active) and active[0] > idx[0]:  # no invoices in the first months: start where sales start
        started = f" · no invoices before {active[0]:%b %Y}"
        g = g.loc[active[0]:]
        idx = g.index
    partial = synced.day < synced.days_in_month  # the current month is not over yet
    col = {"sales": "revenue", "profit": "gp", "margin": "margin"}[measure]
    s = g[col]
    what = {"sales": "Net sales", "profit": "Gross profit", "margin": "Gross margin"}[measure]
    item = ""
    if item_code:
        items = pd.read_parquet(DATA_DIR / "items.parquet").set_index("ItemCode").ItemName
        item = f" of {items.get(item_code.upper(), item_code.upper())}"
    total_rev, total_gp = g.revenue.sum(), g.gp.sum()
    overall = total_gp / total_rev * 100 if total_rev else None
    if measure == "margin":
        sub = (f"Gross profit as % of net sales · last {months} months, invoiced"
               + (f" · {pct(overall)} over the whole period" if overall is not None else ""))
    else:
        sub = f"{cur} · last {months} months, invoiced, excluding tax · total {money(s.sum(), cur)}"
    if partial:
        sub += f" · {idx[-1]:%b} so far (to {synced:%d %b})"
    sub += started

    fig, ax = _figure(4.3, f"{what}{item} by month", sub)
    x = list(range(len(idx)))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    if measure == "margin":
        ax.plot(x, s.values, color=BLUE, linewidth=2, marker="o", markersize=5, markeredgecolor="white", markeredgewidth=1.5)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
        vals = s.dropna()
        lo_y = min(0, vals.min() - 5) if len(vals) else 0
        ax.set_ylim(lo_y, (vals.max() + 8) if len(vals) else 100)
        fmt = pct
    else:
        colours = [BLUE] * len(idx)
        if partial:
            colours[-1] = LIGHT_BLUE
        ax.bar(x, s.values, color=colours, width=0.62)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _axis(v, cur)))
        ax.axhline(0, color=GRID, linewidth=1)
        fmt = lambda v: money(v, cur)
        complete = s[:-1] if partial else s
        if len(complete) >= 3:  # an average needs a few complete months
            avg = complete.mean()
            ax.axhline(avg, color=MUTED, linestyle=(0, (4, 3)), linewidth=0.9)
            ax.annotate(f"average {money(avg, cur)}", (len(idx) - 0.5, avg), xytext=(2, 3), textcoords="offset points",
                        fontsize=8.5, color=MUTED, ha="right", va="bottom")
        ax.set_ylim(min(0, s.min() * 1.15), max(s.max(), 0) * 1.18 or 1)
    ax.yaxis.set_major_locator(MaxNLocator(5))
    ax.set_xlim(-0.6, max(len(idx), 6) - 0.4)  # with only a few months, bars keep their normal width
    _month_ticks(ax, idx)
    # Label only what matters: the best month and the latest one.
    full = s[:-1] if partial and len(s) > 1 else s
    marks = {}
    if full.notna().any() and full.max() > 0 and len(full) > 1:
        marks[int(pd.Series(full.values).idxmax())] = "best"
    known = [i for i, v in enumerate(s.values) if pd.notna(v)]
    if known:
        marks.setdefault(known[-1], "latest")
    for i, kind in marks.items():
        v = s.iloc[i]
        if pd.isna(v):
            continue
        ax.annotate(fmt(v) + (" so far" if partial and i == len(idx) - 1 else ""), (i, max(v, 0) if measure != "margin" else v),
                    xytext=(0, 5 if measure != "margin" else 8), textcoords="offset points", ha="center",
                    fontsize=9, fontweight="bold" if kind == "best" else "normal", color=TEXT)
    title = f"{what} by month"
    best = full.idxmax() if full.notna().any() and full.max() > 0 else None
    return {"_display": _image(fig, title),
            "summary": {"chart": title + item, "measure": measure, "currency": cur, "period_months": months,
                        "months": {str(k): (round(float(v), 1) if measure == "margin" else round(float(v), 2)) if pd.notna(v) else None for k, v in s.items()},
                        "best_month": str(best) if best is not None else None,
                        "total_net_sales": round(float(total_rev), 2), "total_gross_profit": round(float(total_gp), 2),
                        "margin_pct_whole_period": round(overall, 1) if overall is not None else None,
                        "current_month_incomplete": bool(partial), "data_up_to": str(synced.date())}}


def top_chart(by: str = "customer", measure: str = "sales", months: int = 12, top: int = 10) -> dict:
    """Ranking: the biggest customers, items, item groups or salespeople by net sales or gross profit."""
    by = (by or "customer").lower()
    by = next((k for k in ("customer", "item_group", "item", "salesperson") if k.split("_")[-1] in by or k in by), "customer")
    measure = "profit" if "profit" in (measure or "") else "sales"
    months = max(1, min(int(months or 12), 36))
    top = max(3, min(int(top or 10), 20))
    ar, idx, synced = _ar_lines(months)
    if ar.empty:
        return {"error": f"No invoiced sales in the last {months} months."}
    items = pd.read_parquet(DATA_DIR / "items.parquet").set_index("ItemCode")
    if by == "customer":
        key = ar.CardName.fillna(ar.CardCode)
    elif by == "item":
        key = ar.ItemCode.map(items.ItemName).fillna(ar.Dscription)
    elif by == "item_group":
        key = ar.ItemCode.map(items.ItmsGrpNam).fillna("No group")
    else:
        slp = pd.read_parquet(DATA_DIR / "salespeople.parquet").set_index("SlpCode").SlpName
        key = ar.SlpCode.map(slp).fillna("No salesperson")
    col = "gross_profit" if measure == "profit" else "revenue"
    g = ar.groupby(key)[col].sum().sort_values(ascending=False)
    total = g.sum()
    head, rest = g.head(top), g.iloc[top:]
    if len(rest) == 1:  # one left over: show it by name, not as "Other 1"
        head, rest = g, g.iloc[0:0]
    names = [str(n)[:38] for n in head.index]
    values = list(head.values)
    colours = [BLUE] * len(head)
    noun = {"customer": "customers", "item": "items", "item_group": "item groups", "salesperson": "salespeople"}[by]
    if len(rest):
        names.append(f"Other {len(rest)} {noun}")
        values.append(float(rest.sum()))
        colours.append(OTHER)
    cur = _currency()
    labels = [f"{money(v, cur)}  ·  {pct(v / total * 100)}" if total else money(v, cur) for v in values]
    what = "gross profit" if measure == "profit" else "net sales"
    share = head.sum() / total * 100 if total else 0
    title = f"Top {len(head)} {noun} by {what}" if len(rest) else f"{noun.capitalize()} by {what}"
    sub = (f"{cur} · last {months} months, invoiced, excluding tax · % = share of the total {money(total, cur)}"
           + (f" · the top {len(head)} make {pct(share)}" if len(rest) else ""))
    fig, ax = _figure(0.42 * len(names) + 1.3, title, sub, left=_bars_left(names))
    _hbar(ax, names, values, labels, colours)
    return {"_display": _image(fig, title),
            "summary": {"chart": title, "currency": cur, "period_months": months, "total": round(float(total), 2),
                        "top": [{"name": n, measure: round(float(v), 2), "share_pct": round(v / total * 100, 1) if total else None}
                                for n, v in zip(head.index, head.values)],
                        "others": {"count": len(rest), measure: round(float(rest.sum()), 2)} if len(rest) else None,
                        "data_up_to": str(synced.date())}}


def stock_levels_chart(search: str = "", warehouse: str = "", top: int = 15) -> dict:
    """Units on hand per item (all warehouses or one), optionally only items matching words such as 'server'."""
    stock = pd.read_parquet(DATA_DIR / "stock.parquet")
    items = pd.read_parquet(DATA_DIR / "items.parquet")
    if warehouse:
        stock = stock[stock.WhsCode.astype(str) == str(warehouse)]
    g = stock.groupby("ItemCode")[["OnHand", "IsCommited", "OnOrder"]].sum().join(
        items.set_index("ItemCode")[["ItemName", "ItmsGrpNam", "InvntryUom"]], how="left")
    words = [w for w in re.split(r"\s+", (search or "").lower()) if w]
    if words:
        words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]  # "servers" finds "Server"
        text = (g.index.astype(str) + " " + g.ItemName.fillna("") + " " + g.ItmsGrpNam.fillna("")).str.lower()
        g = g[text.apply(lambda t: all(w in t for w in words))]
    if g.empty:
        return {"error": f"No stocked items match '{search}'." if search else "No stock found."}
    top = max(3, min(int(top or 15), 25))
    g = g.assign(available=g.OnHand - g.IsCommited).sort_values("OnHand", ascending=False)
    shown = g.head(top)
    names = [f"{(n or c)[:34]}" for c, n in zip(shown.index, shown.ItemName)]
    labels = []
    for _, r in shown.iterrows():
        bits = [qty(r.OnHand)]
        if r.OnHand <= 0:
            bits = ["0, out of stock" if r.OnHand == 0 else f"{qty(r.OnHand)}, short"]
        elif r.IsCommited > 0:
            bits.append(f"{qty(max(r.available, 0))} free")
        if r.OnOrder > 0:
            bits.append(f"+{qty(r.OnOrder)} on order")
        labels.append("  ·  ".join(bits))
    uom = shown.InvntryUom.dropna()
    unit = uom.iloc[0] if len(uom) and uom.nunique() == 1 and uom.iloc[0] else "units"
    where = f"warehouse {warehouse}" if warehouse else "all warehouses"
    title = f"Stock on hand{': ' + search.strip() if search else ''}"
    sub = (f"{unit.capitalize()}, {where}, as of {_synced():%d %b %Y} · free = not yet promised to customers"
           + (f" · top {top} of {len(g)} items" if len(g) > top else ""))
    fig, ax = _figure(0.42 * len(names) + 1.3, title, sub, left=_bars_left(names))
    _hbar(ax, names, [max(v, 0) for v in shown.OnHand], labels)
    return {"_display": _image(fig, title),
            "summary": {"chart": title, "unit": unit, "warehouse": warehouse or "all", "items_matching": len(g),
                        "items": [{"item_code": c, "item": r.ItemName, "on_hand": float(r.OnHand),
                                   "committed_to_customers": float(r.IsCommited), "available": float(r.available),
                                   "on_order": float(r.OnOrder)} for c, r in shown.iterrows()],
                        "data_as_of": str(_synced().date())}}


def inventory_status_chart() -> dict:
    """Where the money in stock sits: value of stock per status (from line-stop risk to dead stock)."""
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    rep["value"] = rep.OnHand.clip(lower=0) * rep.unit_cost.fillna(0)
    g = rep.groupby("status").agg(value=("value", "sum"), lines=("ItemCode", "size"))
    g = g.reindex([s for s in STATUS_COLOURS if s in g.index])
    cur = _currency()
    total = g.value.sum()
    names = [f"{s if s == 'OK' else s.capitalize()} ({STATUS_MEANING[s]})" for s in g.index]
    labels = [f"{money(v, cur)}  ·  {n} item-warehouse line{'s' if n != 1 else ''}" for v, n in zip(g.value, g.lines)]
    title = "Stock value by status"
    sub = f"{cur}, stock on hand at cost · total {money(total, cur)} · most urgent at the top"
    fig, ax = _figure(0.5 * len(names) + 1.3, title, sub, left=_bars_left(names))
    _hbar(ax, names, list(g.value), labels, [STATUS_COLOURS[s] for s in g.index])
    return {"_display": _image(fig, title),
            "summary": {"chart": title, "currency": cur, "total_value": round(float(total), 2),
                        "by_status": {s: {"value": round(float(v), 2), "lines": int(n), "meaning": STATUS_MEANING[s]}
                                      for s, v, n in zip(g.index, g.value, g.lines)}}}


def stock_projection_chart(item_code: str, warehouse: str = "") -> dict:
    """One item's stock week by week: when it runs short, what is already coming in, and when a new order could arrive."""
    proj = pd.read_parquet(DATA_DIR / "projection.parquet")
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    p = proj[proj.ItemCode.str.upper() == item_code.upper()]
    if p.empty and item_code.strip():  # an item name instead of a code: the best match by name
        names = pd.read_parquet(DATA_DIR / "items.parquet").set_index("ItemCode").ItemName.fillna("")
        hits = [c for c, n in names.items() if item_code.strip().lower() in n.lower() and c in set(proj.ItemCode)]
        if hits:
            p = proj[proj.ItemCode == min(hits, key=lambda c: len(names[c]))]
    if warehouse:
        p = p[p.WhsCode == warehouse]
    if p.empty:
        return {"error": f"No week-by-week projection for {item_code}{' in warehouse ' + warehouse if warehouse else ''} "
                         "(it has no forecast demand or is not a stocked item).",
                "note": "Show its stock with stock_levels_chart (search = its name) instead, and say why."}
    whs = warehouse or p.WhsCode.iloc[0]
    p = p[p.WhsCode == whs].sort_values("week")
    r = rep[(rep.ItemCode == p.ItemCode.iloc[0]) & (rep.WhsCode == whs)].iloc[0]
    weeks = pd.to_datetime(p.week)
    low = p.projected.min()
    arrival = _synced() + pd.Timedelta(days=float(r.lead_days))

    # The headline answers the question: is it short, when does it run out, or is it fine?
    if p.projected.iloc[0] < 0:
        headline = f"{r.ItemName} is already short by {qty(round(-p.projected.iloc[0], 3))} units"
    elif pd.notna(r.runout_date):
        headline = f"{r.ItemName} runs out around {pd.Timestamp(r.runout_date):%d %b %Y}"
    elif low < r.safety_stock:
        headline = f"{r.ItemName} drops below its safety stock"
    else:
        headline = f"{r.ItemName} has enough stock for the next {len(p)} weeks"
    sub = (f"Projected units in warehouse {whs}, week by week · demand forecast minus incoming orders · "
           f"a new order takes about {r.lead_days:.0f} days")
    fig, ax = _figure(4.4, headline, sub)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.fill_between(weeks, p.projected, 0, where=p.projected < 0, step="post", color=RED, alpha=0.10, linewidth=0)
    ax.step(weeks, p.projected, where="post", color=BLUE, linewidth=2.2)
    ax.axhline(0, color=RED, linewidth=1)
    end = weeks.iloc[-1]
    ax.axhline(r.safety_stock, color=ORANGE, linestyle=(0, (4, 3)), linewidth=1.1)
    ax.annotate(f"safety stock {qty(round(r.safety_stock, 3))}", (end, r.safety_stock), xytext=(-2, 3), textcoords="offset points",
                ha="right", va="bottom", fontsize=8.5, color=MUTED)
    if low < 0:
        ax.annotate("below 0 = short", (end, 0), xytext=(-2, -4), textcoords="offset points", ha="right", va="top",
                    fontsize=8.5, color=MUTED)
    # Deliveries already on order: a dot with the quantity, where the line steps up.
    for w, q, v in zip(weeks, p.supply, p.projected):
        if q > 0:
            ax.plot([w], [v], marker="o", markersize=7, color=GREEN, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
            ax.annotate(f"+{qty(q)} arriving", (w, v), xytext=(6, 6), textcoords="offset points", fontsize=8.5, color=TEXT)
    if weeks.iloc[0] <= arrival <= end:
        ax.axvline(arrival, color=MUTED, linewidth=0.9, linestyle=(0, (2, 2)))
        ax.annotate(f"order today, arrives ~{arrival:%d %b}", (arrival, 1), xycoords=("data", "axes fraction"),
                    xytext=(4, -2), textcoords="offset points", fontsize=8.5, color=MUTED, va="top")
    hi = max(p.projected.max(), r.safety_stock, 0)
    lo = min(low, 0)
    pad = (hi - lo) * 0.15 or 1
    ax.set_ylim(lo - pad * 0.5, hi + pad)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.tick_params(axis="x", colors=MUTED)
    title = f"Stock projection {r.ItemCode}"
    return {"_display": _image(fig, title),
            "summary": {"chart": "stock projection", "headline": headline, "item": r.ItemCode, "warehouse": whs,
                        "status": r.status, "on_hand": r.OnHand, "projected_now": p.projected.iloc[0], "lowest_projected": low,
                        "runout_date": r.runout_date, "lead_days": r.lead_days,
                        "new_order_placed_today_arrives": str(arrival.date()), "safety_stock": r.safety_stock,
                        "reorder_point": r.reorder_point, "incoming_total": p.supply.sum(), "action": r.action}}


def receivables_chart(by: str = "age", top: int = 10) -> dict:
    """Money customers still owe: by how late it is, or per customer (overdue part and not-yet-due part)."""
    ar = pd.read_parquet(DATA_DIR / "ar_open.parquet")
    if ar.empty:
        return {"error": "No unpaid customer invoices."}
    ar["DocDueDate"] = pd.to_datetime(ar.DocDueDate)
    today = _synced()
    late = (today - ar.DocDueDate).dt.days
    cur = _currency()
    total = ar.Balance.sum()
    overdue = ar.Balance[late > 0].sum()
    if "customer" in (by or "").lower():
        top = max(3, min(int(top or 10), 20))
        ar["part"] = (late > 0).map({True: "overdue", False: "due"})
        g = ar.pivot_table(index="CardName", columns="part", values="Balance", aggfunc="sum", fill_value=0)
        for c in ("overdue", "due"):
            if c not in g:
                g[c] = 0.0
        g["total"] = g.overdue + g.due
        g = g.sort_values("total", ascending=False)
        head = g.head(top)
        names = [str(n)[:36] for n in head.index]
        title = (f"Who owes us the most: top {len(head)} customers" if len(g) > len(head)
                 else f"Who owes us: {len(head)} customer{'s' if len(head) != 1 else ''}")
        sub = (f"{cur}, unpaid invoices as of {today:%d %b %Y} · red = overdue, grey = not due yet · "
               f"total owed {money(total, cur)}, of which {money(overdue, cur)} overdue")
        fig, ax = _figure(max(2.6, 0.42 * len(names) + 1.5), title, sub, left=_bars_left(names))
        y = list(range(len(head)))[::-1]
        ax.barh(y, head.overdue, color=AGE_COLOURS[3], height=0.62, label="Overdue")
        ax.barh(y, head.due, left=head.overdue, color=AGE_COLOURS[0], height=0.62, label="Not due yet")
        ax.set_yticks(y, names)
        ax.set_ylim(len(names) - 0.4 - max(len(names), 3), len(names) - 0.4)
        ax.xaxis.set_visible(False)
        ax.spines["bottom"].set_visible(False)
        ax.set_xlim(0, head.total.max() * 1.75)
        for yi, (_, r) in zip(y, head.iterrows()):
            text = money(r.total, cur) + (f"  ·  {money(r.overdue, cur)} overdue" if r.overdue > 0 else "  ·  none overdue")
            ax.annotate(text, (r.total, yi), xytext=(5, 0), textcoords="offset points", va="center", fontsize=9)
        if (head.overdue > 0).any() and (head.due > 0).any():  # a legend only when both colours are on the chart
            ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8.5, ncols=2)
        return {"_display": _image(fig, title),
                "summary": {"chart": title, "currency": cur, "total_owed": round(float(total), 2),
                            "overdue": round(float(overdue), 2),
                            "customers": [{"customer": n, "owed": round(float(r.total), 2), "overdue": round(float(r.overdue), 2)}
                                          for n, r in head.iterrows()], "as_of": str(today.date())}}
    buckets = ["Not due yet", "1–30 days late", "31–60 days late", "61–90 days late", "Over 90 days late"]
    b = pd.cut(late, [-10**6, 0, 30, 60, 90, 10**6], labels=buckets)
    g = ar.groupby(b, observed=False).Balance.agg(["sum", "size"]).reindex(buckets, fill_value=0)
    title = "Unpaid customer invoices by how late they are"
    sub = (f"{cur}, as of {today:%d %b %Y} · total owed {money(total, cur)}, "
           f"of which {money(overdue, cur)} ({pct(overdue / total * 100 if total else 0)}) overdue")
    fig, ax = _figure(4.0, title, sub)
    x = range(len(buckets))
    ax.bar(x, g["sum"], color=AGE_COLOURS, width=0.6)
    ax.set_xticks(list(x), [s.replace(" late", "\nlate") for s in buckets])
    ax.tick_params(axis="x", colors=TEXT)
    ax.yaxis.set_visible(False)
    ax.set_ylim(0, g["sum"].max() * 1.25 or 1)
    for xi, (s, n) in zip(x, zip(g["sum"], g["size"])):
        ax.annotate(f"{money(s, cur)}\n{n} invoice{'s' if n != 1 else ''}", (xi, s), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=9)
    return {"_display": _image(fig, title),
            "summary": {"chart": title, "currency": cur, "total_owed": round(float(total), 2), "overdue": round(float(overdue), 2),
                        "by_age": {k: {"amount": round(float(s), 2), "invoices": int(n)} for k, s, n in zip(buckets, g["sum"], g["size"])},
                        "as_of": str(today.date())}}


def cash_forecast_chart() -> dict:
    """Expected cash balance at the end of each of the next 13 weeks, with the lowest point marked."""
    path = DATA_DIR / "finance.json"
    if not path.exists():
        return {"error": "The cash forecast has not been made yet."}
    f = json.loads(path.read_text())
    c = f.get("cash_forecast") or {}
    w = pd.DataFrame(c.get("weekly") or [])
    if w.empty:
        return {"error": "The cash forecast has no weeks."}
    w["week_ending"] = pd.to_datetime(w.week_ending)
    cur = label(f.get("currency") or "") or _currency()
    lo_i = int(w.balance.values.argmin())
    lo = w.iloc[lo_i]
    if w.balance.min() >= 0:
        headline = f"Cash stays positive for the next {len(w)} weeks; lowest {money(lo.balance, cur)}"
    elif c.get("cash_today", 0) < 0:
        headline = f"Cash is below zero now and reaches {money(lo.balance, cur)} by {lo.week_ending:%d %b}"
    else:
        first = w[w.balance < 0].iloc[0]
        headline = f"Cash goes below zero in the week to {first.week_ending:%d %b}"
    sub = (f"{cur}, expected balance at the end of each week · today {money(c.get('cash_today', 0), cur)} · "
           f"in {money(c.get('total_in_13_weeks', 0), cur)}, out {money(c.get('total_out_13_weeks', 0), cur)} · "
           "invoices over 60 days late left out")
    fig, ax = _figure(4.2, headline, sub)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.axhline(0, color=RED if w.balance.min() < 0 else GRID, linewidth=1)
    ax.fill_between(w.week_ending, w.balance, 0, where=w.balance < 0, color=RED, alpha=0.08, linewidth=0, interpolate=True)
    ax.plot(w.week_ending, w.balance, color=BLUE, linewidth=2, marker="o", markersize=4.5, markeredgecolor="white",
            markeredgewidth=1.2)
    late_in_period = lo_i >= len(w) * 0.75
    ax.annotate(f"lowest {money(lo.balance, cur)}\nweek to {lo.week_ending:%d %b}", (lo.week_ending, lo.balance),
                xytext=(-8 if late_in_period else 0, 12), textcoords="offset points",
                ha="right" if late_in_period else "center", va="bottom", fontsize=9, fontweight="bold")
    last = w.iloc[-1]
    if lo_i != len(w) - 1:
        ax.annotate(money(last.balance, cur), (last.week_ending, last.balance), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9)
    span = (w.balance.max() - min(w.balance.min(), 0)) or 1
    ax.set_ylim(min(w.balance.min(), 0) - span * 0.3, max(w.balance.max(), 0) + span * 0.15)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _axis(v, cur)))
    ax.yaxis.set_major_locator(MaxNLocator(5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.tick_params(axis="x", colors=MUTED)
    title = "Cash forecast"
    return {"_display": _image(fig, title),
            "summary": {"chart": "cash balance, next 13 weeks", "headline": headline, "currency": cur,
                        "cash_today": c.get("cash_today"), "lowest_balance": round(float(lo.balance), 2),
                        "lowest_week_ending": str(lo.week_ending.date()), "balance_end": round(float(last.balance), 2),
                        "total_in": c.get("total_in_13_weeks"), "total_out": c.get("total_out_13_weeks"),
                        "left_out_as_doubtful": c.get("left_out_as_doubtful")}}


def custom_chart(title: str, chart_type: str, labels: list, values: list, unit: str = "", subtitle: str = "") -> dict:
    """Last resort, for figures already returned by another tool: one series, drawn in the house style."""
    if not labels or not values or len(labels) != len(values):
        return {"error": "labels and values must be non-empty lists of the same length."}
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        return {"error": "values must be numbers."}
    labels = [str(lbl) for lbl in labels]
    kind = (chart_type or "bar").lower()
    is_money = bool(unit) and not re.fullmatch(r"\s*(units?|pcs|%|percent|days?|weeks?|items?|qty|quantity)\s*", unit, re.I)
    fmt = pct if unit.strip() in ("%", "percent") else (lambda v: money(v, unit)) if is_money else qty
    sub = subtitle or (f"In {unit}" if unit else "")
    over_time = kind == "line"
    if not over_time:
        # Categories: horizontal bars, biggest first, the rest folded into "Other"; a pie only for a few shares of a whole.
        if kind == "pie" and len(values) <= 5 and all(v >= 0 for v in values):
            fig, ax = _figure(4.2, title, sub)
            ax.axis("off")
            total = sum(values) or 1
            wedges, _ = ax.pie(values, colors=[BLUE, "#eb6834", "#1baf7a", "#eda100", "#6250d6"][:len(values)], startangle=90,
                               counterclock=False, wedgeprops={"linewidth": 2, "edgecolor": "white", "width": 0.42})
            ax.legend(wedges, [f"{lbl}  ·  {fmt(v)}  ·  {pct(v / total * 100)}" for lbl, v in zip(labels, values)],
                      loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False, fontsize=9)
            ax.set_aspect("equal")
        else:
            order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
            names, vals = [labels[i][:38] for i in order[:15]], [values[i] for i in order[:15]]
            colours = [BLUE] * len(vals)
            if len(order) > 15:
                names.append(f"Other {len(order) - 15}")
                vals.append(sum(values[i] for i in order[15:]))
                colours.append(OTHER)
            fig, ax = _figure(0.42 * len(names) + 1.3, title, sub, left=_bars_left(names))
            _hbar(ax, names, vals, [fmt(v) for v in vals], colours)
    else:
        fig, ax = _figure(4.2, title, sub)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        x = list(range(len(values)))
        ax.plot(x, values, color=BLUE, linewidth=2, marker="o", markersize=5, markeredgecolor="white", markeredgewidth=1.5)
        step = max(1, len(labels) // 12)
        ax.set_xticks(x[::step], [lbl[:10] for lbl in labels[::step]])
        ax.tick_params(axis="x", colors=MUTED)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt(v)))
        ax.yaxis.set_major_locator(MaxNLocator(5))
        for i in {int(pd.Series(values).idxmax()), len(values) - 1}:
            ax.annotate(fmt(values[i]), (i, values[i]), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=9)
    return {"_display": _image(fig, title), "summary": {"chart": title, "type": kind, "points": len(values)}}
