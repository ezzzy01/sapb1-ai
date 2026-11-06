"""Supplier analytics: what we buy from whom, what we pay and when, and which supplier gives the best price.

All from the nightly Parquet files (A/P invoice lines, A/P invoices, outgoing payments, goods receipts), in local
currency. Spend = A/P invoice lines before tax, after line and document discounts. Unit price = spend / quantity.
"""

import json
from datetime import date

import pandas as pd

from .charts import BLUE, GREEN, OTHER, _bars_left, _figure, _hbar, _image
from .export import excel_link
from .numbers import label, money, pct, qty
from .sync import DATA_DIR

TOP = 10


def _read(name: str, *dates: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{name}.parquet")
    for d in dates:
        df[d] = pd.to_datetime(df[d])
    return df


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.round({c: 2 for c in df.select_dtypes("number").columns})
    return json.loads(df.to_json(orient="records", date_format="iso").replace("T00:00:00.000", ""))


def supplier_analytics(months: int = 12, supplier: str = "", item: str = "") -> dict:
    """Dashboard of suppliers: spend, payments, items purchased and the best price per item; charts plus an Excel file."""
    months = max(1, min(int(months or 12), 36))
    today = pd.Timestamp(date.today())
    start = today - pd.DateOffset(months=months)
    cur = label(json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or "")

    ap = _read("ap_lines", "DocDate")
    ap = ap[(ap.DocDate > start) & (ap.InvQty > 0)].copy()
    if supplier:
        s = supplier.lower()
        ap = ap[ap.CardCode.str.lower().eq(s) | ap.CardName.fillna("").str.lower().str.contains(s, regex=False)]
    if item:
        i = item.lower()
        ap = ap[ap.ItemCode.str.lower().eq(i) | ap.Dscription.fillna("").str.lower().str.contains(i, regex=False)]
    if ap.empty:
        what = " ".join(x for x in (f"supplier '{supplier}'" if supplier else "", f"item '{item}'" if item else "") if x)
        return {"error": f"No supplier invoices{' for ' + what if what else ''} in the last {months} months."}
    ap["spend"] = ap.LineTotal * (1 - ap.DocDisc.fillna(0) / 100)
    names = ap.groupby("CardCode").CardName.last()
    codes = set(ap.CardCode)

    # Money we owe now and how we pay (all suppliers in the selection, not limited to the period)
    inv = _read("ap_invoices", "DocDueDate")
    inv = inv[(inv.DocStatus == "O") & inv.CardCode.isin(codes)].assign(balance=lambda d: d.DocTotal - d.PaidToDate)
    inv = inv[inv.balance > 0]
    owed = inv.groupby("CardCode").balance.sum()
    overdue = inv[inv.DocDueDate < today].groupby("CardCode").balance.sum()
    due_14 = float(inv[(inv.DocDueDate >= today) & (inv.DocDueDate <= today + pd.Timedelta(days=14))].balance.sum())
    pa = _read("ap_payments_applied", "PayDate", "DocDueDate")
    pa = pa[(pa.PayDate > start) & pa.CardCode.isin(codes) & (pa.SumApplied > 0)]
    pa["days"] = (pa.PayDate - pa.DocDueDate).dt.days
    pay_days = (pa.days * pa.SumApplied).groupby(pa.CardCode).sum() / pa.SumApplied.groupby(pa.CardCode).sum()
    pays = _read("ap_payments", "DocDate")
    pays = pays[(pays.DocDate > start) & pays.CardCode.isin(codes)]
    lt = _read("lead_times", "GRPODate")
    lt = lt[(lt.GRPODate > start) & lt.CardCode.isin(codes) & (lt.LeadDays >= 0)]
    on_time = lt.groupby("CardCode").DaysLate.apply(lambda d: (d <= 0).mean() * 100)

    # Per supplier
    g = ap.groupby("CardCode").agg(spend=("spend", "sum"), invoices=("DocEntry", "nunique"), n_items=("ItemCode", "nunique"),
                                   last_invoice=("DocDate", "max"))
    total = float(g.spend.sum())
    sup = pd.DataFrame({
        "Supplier": names.reindex(g.index), "Code": g.index, "Spend": g.spend, "Share %": g.spend / total * 100,
        "Invoices": g.invoices, "Items": g.n_items, "We owe now": owed.reindex(g.index).fillna(0),
        "Overdue": overdue.reindex(g.index).fillna(0), "We paid days after due (avg)": pay_days.reindex(g.index),
        "On-time delivery %": on_time.reindex(g.index), "Last invoice": g.last_invoice,
    }).sort_values("Spend", ascending=False).reset_index(drop=True)

    # Per item and supplier: average unit price; the best (lowest) supplier per item, and what buying there would save
    p = ap.groupby(["ItemCode", "CardCode"]).agg(spend=("spend", "sum"), qty=("InvQty", "sum"), last=("DocDate", "max")).reset_index()
    p["unit"] = p.spend / p.qty
    last_price = (ap.sort_values("DocDate").assign(u=lambda d: d.spend / d.InvQty).groupby("ItemCode").u.last())
    desc = ap.groupby("ItemCode").Dscription.last()
    best = p.loc[p.groupby("ItemCode").unit.idxmin()].set_index("ItemCode")
    worst = p.groupby("ItemCode").unit.max()
    p["saving"] = (p.unit - p.ItemCode.map(best.unit)) * p.qty
    it = p.groupby("ItemCode").agg(qty=("qty", "sum"), spend=("spend", "sum"), suppliers=("CardCode", "nunique"),
                                   saving=("saving", "sum"))
    items = pd.DataFrame({
        "Item code": it.index, "Item": desc.reindex(it.index).values, "Qty": it.qty.values, "Spend": it.spend.values,
        "Avg price": (it.spend / it.qty).values, "Suppliers": it.suppliers.values,
        "Best supplier": names.reindex(best.CardCode.reindex(it.index)).values,
        "Best price": best.unit.reindex(it.index).values, "Highest price": worst.reindex(it.index).values,
        "Last price": last_price.reindex(it.index).values, "Possible saving": it.saving.values,
    }).sort_values("Spend", ascending=False).reset_index(drop=True)
    compare = (p.assign(Supplier=p.CardCode.map(names), Item=p.ItemCode.map(desc),
                        Best=lambda d: d.unit == d.ItemCode.map(best.unit))
               [lambda d: d.ItemCode.map(it.suppliers) > 1]
               .sort_values(["ItemCode", "unit"])
               .rename(columns={"ItemCode": "Item code", "qty": "Qty", "spend": "Spend", "unit": "Avg price", "last": "Last bought"})
               [["Item code", "Item", "Supplier", "Qty", "Spend", "Avg price", "Best", "Last bought"]])
    compare["Best"] = compare.Best.map({True: "Best price", False: ""})

    # Payments per month
    month_idx = pd.period_range(start.to_period("M") + 1, today.to_period("M"), freq="M")
    pm = pays.groupby(pays.DocDate.dt.to_period("M")).DocTotal.agg(["sum", "size"]).reindex(month_idx, fill_value=0)
    by_month = pd.DataFrame({"Month": month_idx.strftime("%b %Y"), "Paid": pm["sum"].values, "Payments": pm["size"].values})

    saving = float(items["Possible saving"].sum())
    paid = float(pays.DocTotal.sum())
    owe_total, overdue_total = float(sup["We owe now"].sum()), float(sup["Overdue"].sum())
    w = pa.SumApplied.sum()
    avg_days = float((pa.days * pa.SumApplied).sum() / w) if w else None
    scope = f"last {months} months" + (f" · supplier: {supplier}" if supplier else "") + (f" · item: {item}" if item else "")

    # ---------- charts for the chat ----------
    shown = []
    head = sup.head(TOP)
    rest = sup.iloc[TOP:]
    bar_names = [str(n)[:38] for n in head.Supplier] + ([f"Other {len(rest)} suppliers"] if len(rest) else [])
    bar_vals = list(head.Spend) + ([float(rest.Spend.sum())] if len(rest) else [])
    title = f"Top {len(head)} suppliers by spend" if len(rest) else "Spend by supplier"
    fig, ax = _figure(0.42 * len(bar_names) + 1.3, title,
                      f"{cur} · {scope} · A/P invoices before tax · % = share of {money(total, cur)}", left=_bars_left(bar_names))
    _hbar(ax, bar_names, bar_vals, [f"{money(v, cur)}  ·  {pct(v / total * 100)}" for v in bar_vals],
          [BLUE] * len(head) + [OTHER] * bool(len(rest)))
    shown.append(_image(fig, title))
    sav = items[items["Possible saving"] > 0.005].sort_values("Possible saving", ascending=False).head(TOP)
    if len(sav):
        title2 = "Where buying at the best price would have saved money"
        n2 = [str(n)[:38] for n in sav.Item]
        fig, ax = _figure(0.42 * len(n2) + 1.3, title2,
                          f"{cur} · {scope} · saving if every unit had been bought at that item's best supplier price",
                          left=_bars_left(n2))
        _hbar(ax, n2, list(sav["Possible saving"]),
              [f"{money(v, cur)}  ·  best: {b} at {money(bp, cur)}" for v, b, bp in zip(sav["Possible saving"], sav["Best supplier"], sav["Best price"])],
              GREEN)
        shown.append(_image(fig, title2))

    # ---------- Excel dashboard ----------
    key = pd.DataFrame({"Figure": ["Spend (before tax)", "Suppliers", "Supplier invoices", "Items bought", "Paid to suppliers",
                                   "We owe now", "Of which overdue", "Due in the next 14 days", "We paid days after due (avg)",
                                   "Possible saving at best prices"],
                        "Value": pd.Series([total, len(sup), int(ap.DocEntry.nunique()), len(items), paid, owe_total,
                                            overdue_total, due_14, round(avg_days, 1) if avg_days is not None else None, saving],
                                           dtype=object)})
    summary = {
        "tiles": [
            {"label": f"Spend ({cur})", "value": total, "note": f"{len(sup):,} suppliers · {len(items):,} items"},
            {"label": f"Paid ({cur})", "value": paid, "note": f"{len(pays):,} payments · " + (
                f"on average {abs(avg_days):.0f} days {'after' if avg_days > 0 else 'before'} due" if avg_days is not None else "no timing data")},
            {"label": f"We owe now ({cur})", "value": owe_total, "note": f"{money(overdue_total, cur)} overdue"},
            {"label": f"Possible saving ({cur})", "value": saving, "note": "if every item was bought at its best price"},
        ],
        "charts": [
            {"sheet": "Suppliers", "category": "Supplier", "value": "Spend", "type": "bar", "top": 10,
             "title": "Top 10 suppliers" if len(sup) > 10 else "Suppliers", "subtitle": f"Spend before tax, {cur}"},
            {"sheet": "Payments by month", "category": "Month", "value": "Paid", "wide": True,
             "title": "Paid to suppliers by month", "subtitle": f"Outgoing payments, {cur}"},
            {"sheet": "Items", "category": "Item", "value": "Spend", "type": "bar", "top": 10,
             "title": "Top 10 items bought" if len(items) > 10 else "Items bought", "subtitle": f"Spend before tax, {cur}"},
        ],
        "note": "Details: Key figures, Suppliers, Items (best price per item), Price comparison (items bought from more "
                "than one supplier) and Payments by month.",
    }
    sheets = {"Key figures": key, "Suppliers": sup, "Items": items, "Price comparison": compare, "Payments by month": by_month}
    link = excel_link(f"Supplier_analytics_{today:%Y%m%d}", sheets,
                      money_cols=("Spend", "We owe now", "Overdue", "Avg price", "Best price", "Highest price", "Last price",
                                  "Possible saving", "Paid"),
                      title="Supplier analytics", subtitle=f"{scope} · amounts in {cur}", summary=summary,
                      bars={"Suppliers": ("Spend",), "Items": ("Spend",), "Payments by month": ("Paid",)})

    return {
        "_display": "\n\n".join(shown) + "\n\n" + link + "\n",
        "currency": cur, "period": scope,
        "headline": {"spend_before_tax": round(total, 2), "suppliers": len(sup), "items": len(items),
                     "paid_to_suppliers": round(paid, 2), "payments": len(pays), "we_owe_now": round(owe_total, 2),
                     "overdue": round(overdue_total, 2), "due_next_14_days": round(due_14, 2),
                     "we_paid_days_after_due_avg": round(avg_days, 1) if avg_days is not None else None,
                     "on_time_delivery_pct_overall": round(float((lt.DaysLate <= 0).mean() * 100), 1) if len(lt) else None,
                     "possible_saving_at_best_prices": round(saving, 2)},
        "top_suppliers": _records(sup.head(TOP)[["Supplier", "Spend", "Share %", "We owe now", "Overdue",
                                                 "We paid days after due (avg)", "On-time delivery %"]]),
        "cheapest_supplier_for_how_many_items": {k: int(v) for k, v in
                                                  items[items.Suppliers > 1]["Best supplier"].value_counts().head(5).items()},
        "top_items_with_best_price": _records(items.head(TOP)[["Item", "Qty", "Spend", "Suppliers", "Best supplier",
                                                               "Best price", "Highest price", "Possible saving"]]),
        "instruction": "The charts and the Excel dashboard link are already shown to the user. Summarise the key points "
                       "in a few lines (biggest suppliers, what we owe/overdue, how we pay, best-price savings) using these "
                       "exact figures; do not repeat the link.",
    }
