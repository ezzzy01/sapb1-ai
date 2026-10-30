"""Charts for the chat, drawn from the synced data (never from numbers typed by the model).

Each tool returns {"_display": <markdown image>, "summary": ...}. The agent shows the image to the
user directly and gives the model only the summary, so the image is never re-typed by the model.
"""

import base64
import io
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .sync import DATA_DIR  # noqa: E402

# SAP Fiori Horizon palette
BLUE, RED, ORANGE, GREEN, GREY, TEXT, GRID = "#0070f2", "#d20a0a", "#e76500", "#30914c", "#788fa6", "#1d2d3e", "#e5e5e5"
SERIES = [BLUE, "#c87b00", "#75980b", "#df1278", "#8b47d7", "#049f9a", "#5d36ff", "#a00875"]
STATUS_COLOURS = {"LINE STOP RISK": RED, "STOCKOUT RISK": ORANGE, "REORDER": BLUE, "OVERSTOCK": "#8b47d7",
                  "DEAD STOCK": GREY, "OK": GREEN}


def _currency() -> str:
    return json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or ""


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=TEXT, pad=12)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _image(fig, title: str) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return f"![{title}](data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()})"


def _money(cur: str):
    return FuncFormatter(lambda v, _: f"{cur}{v / 1e6:,.1f}M" if abs(v) >= 1e6 else f"{cur}{v / 1e3:,.0f}k" if abs(v) >= 1e3 else f"{cur}{v:,.0f}")


def stock_projection_chart(item_code: str, warehouse: str = "") -> dict:
    proj = pd.read_parquet(DATA_DIR / "projection.parquet")
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    p = proj[proj.ItemCode.str.upper() == item_code.upper()]
    if warehouse:
        p = p[p.WhsCode == warehouse]
    if p.empty:
        return {"error": f"No projection for {item_code}{' in whs ' + warehouse if warehouse else ''}."}
    whs = p.WhsCode.iloc[0] if not warehouse else warehouse
    p = p[p.WhsCode == whs]
    r = rep[(rep.ItemCode == p.ItemCode.iloc[0]) & (rep.WhsCode == whs)].iloc[0]

    fig, ax = plt.subplots(figsize=(9, 4.2))
    _style(ax, f"{r.ItemName} (whs {whs}): projected stock, next {len(p)} weeks")
    ax.bar(p.week, p.supply, width=4, color=GREEN, alpha=0.35, label="Incoming (POs / production)")
    ax.step(p.week, p.projected, where="post", color=BLUE, linewidth=2.2, label="Projected stock")
    ax.fill_between(p.week, p.projected, 0, where=p.projected < 0, step="post", color=RED, alpha=0.12)
    ax.axhline(0, color=RED, linewidth=1)
    ax.axhline(r.safety_stock, color=ORANGE, linestyle="--", linewidth=1.2, label=f"Safety stock ({r.safety_stock:.0f})")
    ax.axhline(r.reorder_point, color=GREY, linestyle=":", linewidth=1.2, label=f"Reorder point ({r.reorder_point:.0f})")
    arrival = pd.Timestamp.today().normalize() + pd.Timedelta(days=float(r.lead_days))
    ax.axvline(arrival, color=GREY, linewidth=1)
    ax.annotate(f"new order could arrive\n({r.lead_days:.0f} d lead time)", (arrival, ax.get_ylim()[1]), fontsize=8, color=GREY,
                xytext=(4, -24), textcoords="offset points")
    if pd.notna(r.runout_date):
        ro = pd.Timestamp(r.runout_date)
        ax.axvline(ro, color=RED, linewidth=1.2)
        ax.annotate("runs out", (ro, 0), fontsize=8, color=RED, xytext=(4, 6), textcoords="offset points")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.set_ylabel("units", color=TEXT, fontsize=9)
    ax.legend(fontsize=8, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    title = f"Stock projection {r.ItemCode}"
    return {"_display": _image(fig, title),
            "summary": {"chart": "stock projection", "item": r.ItemCode, "warehouse": whs, "status": r.status,
                        "on_hand": r.OnHand, "projected_now": p.projected.iloc[0], "lowest_projected": p.projected.min(),
                        "runout_date": r.runout_date, "lead_days": r.lead_days,
                        "new_order_placed_today_arrives": str(arrival.date()), "safety_stock": r.safety_stock,
                        "reorder_point": r.reorder_point, "incoming_total": p.supply.sum(), "action": r.action}}


def sales_trend_chart(months: int = 12, item_code: str = "") -> dict:
    sales = pd.read_parquet(DATA_DIR / "sales.parquet")
    s = sales.assign(DocDate=pd.to_datetime(sales.DocDate))
    if item_code:
        s = s[s.ItemCode.str.upper() == item_code.upper()]
        if s.empty:
            return {"error": f"No sales for {item_code}."}
    months = max(3, min(int(months), 36))
    end = s.DocDate.max().to_period("M")
    idx = pd.period_range(end - months + 1, end, freq="M")
    m = s.groupby(s.DocDate.dt.to_period("M")).LineTotal.sum().reindex(idx, fill_value=0)
    cur = _currency()

    fig, ax = plt.subplots(figsize=(9, 4))
    _style(ax, f"Monthly sales{' of ' + item_code.upper() if item_code else ''}, last {months} months")
    ax.bar([p.strftime("%b %y") for p in m.index], m.values, color=BLUE, width=0.65)
    avg = m.mean()
    ax.axhline(avg, color=GREY, linestyle="--", linewidth=1)
    ax.annotate(f"average {cur}{avg:,.0f}", (0, avg), fontsize=8, color=GREY, xytext=(0, 4), textcoords="offset points")
    ax.yaxis.set_major_formatter(_money(cur))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    return {"_display": _image(fig, "Sales trend"),
            "summary": {"chart": "monthly sales", "item": item_code or "all", "currency": cur,
                        "months": {str(k): round(float(v), 2) for k, v in m.items()}, "average": round(float(avg), 2)}}


def inventory_status_chart() -> dict:
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    rep["value"] = rep.OnHand.clip(lower=0) * rep.unit_cost.fillna(0)
    g = rep.groupby("status").agg(value=("value", "sum"), lines=("ItemCode", "size"))
    g = g.reindex([s for s in STATUS_COLOURS if s in g.index])
    cur = _currency()

    fig, ax = plt.subplots(figsize=(9, 3.8))
    _style(ax, "Stock value by status")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    bars = ax.barh(g.index[::-1], g.value[::-1], color=[STATUS_COLOURS[s] for s in g.index[::-1]], height=0.6)
    for b, (v, n) in zip(bars, zip(g.value[::-1], g.lines[::-1])):
        ax.annotate(f"{cur}{v:,.0f} · {n} line(s)", (b.get_width(), b.get_y() + b.get_height() / 2), fontsize=8, color=TEXT,
                    xytext=(4, 0), textcoords="offset points", va="center")
    ax.xaxis.set_major_formatter(_money(cur))
    ax.set_xlim(0, g.value.max() * 1.35 if g.value.max() > 0 else 1)
    return {"_display": _image(fig, "Stock value by status"),
            "summary": {"chart": "stock value by status", "currency": cur,
                        "by_status": {s: {"value": round(float(v), 2), "lines": int(n)} for s, v, n in zip(g.index, g.value, g.lines)}}}


def custom_chart(title: str, chart_type: str, labels: list, values: list, unit: str = "") -> dict:
    """For comparisons not covered above. Values must come from tool results."""
    if not labels or len(labels) != len(values):
        return {"error": "labels and values must be non-empty lists of the same length."}
    values = [float(v) for v in values]
    labels = [str(lbl) for lbl in labels][:30]
    values = values[:30]
    fig, ax = plt.subplots(figsize=(9, 4))
    _style(ax, title)
    kind = chart_type.lower()
    if kind == "pie":
        ax.grid(False)
        ax.pie(values, labels=labels, colors=SERIES * 4, autopct="%1.0f%%", textprops={"fontsize": 8, "color": TEXT},
               wedgeprops={"linewidth": 1, "edgecolor": "white"})
        ax.axis("equal")
    elif kind == "line":
        ax.plot(labels, values, color=BLUE, linewidth=2, marker="o")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    elif kind in ("barh", "horizontal"):
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID)
        ax.barh(labels[::-1], values[::-1], color=BLUE, height=0.6)
    else:
        ax.bar(labels, values, color=BLUE, width=0.65)
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    if unit and kind != "pie":
        (ax.set_xlabel if kind in ("barh", "horizontal") else ax.set_ylabel)(unit, color=TEXT, fontsize=9)
    return {"_display": _image(fig, title), "summary": {"chart": title, "type": kind, "points": len(values)}}
