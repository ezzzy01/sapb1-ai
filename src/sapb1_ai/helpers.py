"""Helpers that do a whole task (Joule-style "agents"), read-only:

- collections_plan: who to chase today, in which order, how much, the next step, and a ready reminder for each.
- purchasing_plan: what to order, grouped into one proposed order per supplier, with an email draft for each.

Everything is computed here from B1 data (exact figures); the drafts come from drafts.py. Nothing is sent and nothing
is written to SAP. The morning briefing uses the same plans without drafts.
"""

import json
import re
from datetime import date

import pandas as pd

from .db import query
from .drafts import payment_reminder, supplier_order_email
from .numbers import label, money, qty
from .sync import DATA_DIR

# Standard escalation by how late the oldest overdue invoice is (days): (up to, step, reminder tone)
STEPS = [(30, "Send a polite reminder", "polite"),
         (60, "Send a firm reminder and phone them", "firm"),
         (90, "Send a final notice and hold new deliveries", "final"),
         (10 ** 6, "Escalate to management; final notice, deliveries on hold", "final")]
URGENT = {"LINE STOP RISK", "STOCKOUT RISK"}


def _cur() -> str:
    return label(json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or "")


def _link(display: str) -> str:
    m = re.search(r"\]\(([^)]+\.eml)\)", display or "")
    return m.group(1) if m else ""


def _cell(text) -> str:
    return str(text).replace("|", "/")


# ---------- collections ----------

def collections_plan(top: int = 10, drafts: int = 5) -> dict:
    """Customers to chase today, most urgent first, with the next step and (for the first few) a ready reminder."""
    top = max(1, min(int(top or 10), 50))
    drafts = max(0, min(int(drafts if drafts is not None else 5), top, 10))
    today, cur = pd.Timestamp(date.today()), _cur()
    ar = pd.read_parquet(DATA_DIR / "ar_open.parquet")
    ar["DocDueDate"] = pd.to_datetime(ar.DocDueDate)
    ar["days_late"] = (today - ar.DocDueDate).dt.days
    late = ar[ar.days_late > 0]
    if late.empty:
        soon = ar[ar.days_late > -7]
        return {"currency": cur, "customers_to_chase": 0,
                "note": "Nobody is overdue today." + (f" {soon.CardCode.nunique()} customer(s) have "
                        f"{money(float(soon.Balance.sum()), cur)} falling due within 7 days." if len(soon) else "")}
    g = late.groupby(["CardCode", "CardName"]).agg(overdue=("Balance", "sum"), invoices=("DocNum", "size"),
                                                   oldest=("days_late", "max")).reset_index()
    owed = ar.groupby("CardCode").Balance.sum()
    so = pd.read_parquet(DATA_DIR / "open_sales.parquet")
    orders = so.groupby("CardCode").OpenValue.sum()
    pay = pd.read_parquet(DATA_DIR / "ar_payments.parquet")
    pay = pay[pd.to_datetime(pay.PayDate) > today - pd.DateOffset(months=24)]
    pay["late"] = (pd.to_datetime(pay.PayDate) - pd.to_datetime(pay.DocDueDate)).dt.days
    habit = (pay.late * pay.SumApplied).groupby(pay.CardCode).sum() / pay.SumApplied.groupby(pay.CardCode).sum()
    g["owed"] = g.CardCode.map(owed)
    g["open_orders"] = g.CardCode.map(orders).fillna(0)
    g["usually_pays_days_late"] = g.CardCode.map(habit)
    step = [next(s for s in STEPS if d <= s[0]) for d in g.oldest]
    g["step"], g["tone"] = [s[1] for s in step], [s[2] for s in step]
    g["priority"] = g.overdue * (1 + g.oldest.clip(upper=365) / 30)  # money at stake, weighted by how late it is
    g = g.sort_values("priority", ascending=False).reset_index(drop=True)
    total_overdue, n_all = float(g.overdue.sum()), len(g)
    g = g.head(top)

    codes = list(g.CardCode)
    contact = query("SELECT CardCode, CntctPrsn, Phone1, E_Mail FROM OCRD WHERE CardCode IN (" + ",".join("?" * len(codes)) + ")",
                    tuple(codes)).set_index("CardCode")
    rows, links = [], []
    for i, r in enumerate(g.itertuples(), 1):
        c = contact.loc[r.CardCode] if r.CardCode in contact.index else None
        who = " · ".join(x for x in ((c.CntctPrsn, c.Phone1) if c is not None else ()) if x) or "-"
        hold = r.oldest > 60 and r.open_orders > 0
        rows.append(f"| {i} | {_cell(r.CardName)} | {money(r.overdue, cur)} ({r.invoices}) | {qty(r.oldest)} days | "
                    f"{money(r.open_orders, cur) + (' ⛔ hold' if hold else '') if r.open_orders else '-'} | {r.step} | {_cell(who)} |")
        if i <= drafts:
            d = payment_reminder(r.CardCode, tone=r.tone)
            if link := _link(d.get("_display", "")):
                links.append(f"- 📧 [{r.tone.capitalize()} reminder to {r.CardName}]({link})")
    shown = (f"**Collections plan for today** · {n_all} customer(s) overdue, {money(total_overdue, cur)} in total"
             + (f" · top {len(g)} by money at stake and lateness" if n_all > len(g) else "") + "\n\n"
             "| # | Customer | Overdue (invoices) | Oldest | Open orders | Next step | Contact |\n|---|---|---|---|---|---|---|\n"
             + "\n".join(rows) + "\n")
    if links:
        shown += "\n**Ready reminders** (drafts, nothing is sent; open, check and send):\n" + "\n".join(links) + "\n"
    shown += ("\n*Steps: up to 30 days late polite reminder · 31-60 firm reminder and call · 61-90 final notice and hold "
              "deliveries · over 90 escalate. ⛔ = open orders to hold.*\n")
    return {"_display": shown, "currency": cur, "customers_overdue": n_all, "total_overdue": round(total_overdue, 2),
            "customers": json.loads(g[["CardName", "overdue", "invoices", "oldest", "owed", "open_orders",
                                       "usually_pays_days_late", "step"]].round(2).to_json(orient="records")),
            "reminders_drafted": len(links),
            "instruction": "The plan table and the reminder drafts are already shown. In 2-4 sentences say who matters most "
                           "today and why, using these exact figures; check the company's credit policy with "
                           "company_documents only if the user asks. Do not repeat the table or links."}


# ---------- purchasing ----------

def _best_supplier(items: list[str], today: pd.Timestamp) -> pd.DataFrame:
    """For items without a preferred supplier: the cheapest supplier in the last 12 months, else the latest one."""
    ap = pd.read_parquet(DATA_DIR / "ap_lines.parquet")
    ap = ap[ap.ItemCode.isin(items) & (ap.InvQty > 0)].copy()
    if ap.empty:
        return pd.DataFrame(columns=["CardCode", "why"])
    ap["DocDate"] = pd.to_datetime(ap.DocDate)
    ap["unit"] = ap.LineTotal * (1 - ap.DocDisc.fillna(0) / 100) / ap.InvQty
    recent = ap[ap.DocDate > today - pd.DateOffset(months=12)]
    out = {}
    if len(recent):
        p = recent.groupby(["ItemCode", "CardCode"]).apply(lambda d: (d.unit * d.InvQty).sum() / d.InvQty.sum(), include_groups=False)
        for item, s in p.groupby(level=0):
            out[item] = (s.idxmin()[1], "cheapest in the last 12 months")
    for item, d in ap.sort_values("DocDate").groupby("ItemCode"):
        out.setdefault(item, (d.CardCode.iloc[-1], "bought from them last"))
    return pd.DataFrame.from_dict(out, orient="index", columns=["CardCode", "why"])


def purchasing_plan(drafts: bool = True) -> dict:
    """What to order now, as one proposed order per supplier (urgent first), each with an order email draft."""
    today, cur = pd.Timestamp(date.today()), _cur()
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    rep = rep[rep.suggested_qty > 0].copy()
    if rep.empty:
        return {"currency": cur, "suppliers": 0, "note": "Nothing needs ordering today: every item's stock and open orders "
                                                          "cover the forecast until new stock could arrive."}
    rep["supplier"], rep["chosen"] = rep.PrefVendor, "preferred supplier in B1"
    missing = rep.PrefVendor.isna()
    if missing.any():
        best = _best_supplier(list(rep.loc[missing, "ItemCode"].unique()), today)
        rep.loc[missing, "supplier"] = rep.loc[missing, "ItemCode"].map(best.CardCode)
        rep.loc[missing, "chosen"] = rep.loc[missing, "ItemCode"].map(best.why)
    names = query("SELECT CardCode, CardName FROM OCRD WHERE CardType = 'S'").set_index("CardCode").CardName
    no_supplier = rep[rep.supplier.isna()]
    rep = rep[rep.supplier.notna()]
    rep["urgent"] = rep.status.isin(URGENT)
    rep["runout"] = pd.to_datetime(rep.runout_date, errors="coerce")
    # one line per item (warehouses added up), so a supplier gets one quantity per item
    lines = rep.groupby(["supplier", "ItemCode"]).agg(qty=("suggested_qty", "sum"), value=("suggested_value", "sum"),
                                                     urgent=("urgent", "any"), runout=("runout", "min"),
                                                     chosen=("chosen", "first"), name=("ItemName", "first")).reset_index()
    per = lines.groupby("supplier").agg(items=("ItemCode", "size"), value=("value", "sum"), urgent=("urgent", "sum"),
                                         runout=("runout", "min"),
                                         suggested=("chosen", lambda c: (c != "preferred supplier in B1").any())).reset_index()
    per = per.sort_values(["urgent", "runout", "value"], ascending=[False, True, False]).reset_index(drop=True)
    rows, links, plan = [], [], []
    for i, r in enumerate(per.itertuples(), 1):
        name = names.get(r.supplier, r.supplier)
        mine = lines[lines.supplier == r.supplier].sort_values(["urgent", "runout"], ascending=[False, True])
        what = ", ".join(f"{n[:30]} × {qty(q)}" for n, q in zip(mine.name, mine.qty))
        link = ""
        if drafts:
            d = supplier_order_email(r.supplier, items=", ".join(f"{c}:{q:g}" for c, q in zip(mine.ItemCode, mine.qty)))
            link = _link(d.get("_display", ""))
        run = ("already out" if r.runout < today else f"{r.runout:%d %b %Y}") if pd.notna(r.runout) else "-"
        rows.append(f"| {i} | {_cell(name)}{' *' if r.suggested else ''} | {_cell(what)} | "
                    f"{'🛑 ' + str(int(r.urgent)) if r.urgent else '-'} | {money(r.value, cur)} | {run} | "
                    + (f"[📧 draft]({link})" if link else "-") + " |")
        plan.append({"supplier": name, "items": int(r.items), "urgent_items": int(r.urgent),
                     "estimated_value_at_cost": round(float(r.value), 2), "earliest_runout": run,
                     "lines": [{"item": n, "qty": float(q), "supplier_chosen_because": w} for n, q, w in zip(mine.name, mine.qty, mine.chosen)]})
    total = float(per.value.sum())
    shown = (f"**Purchasing plan for today** · {len(per)} proposed order(s), {money(total, cur)} at current stock cost\n\n"
             "| # | Supplier | Items × qty | Urgent | Est. value | Earliest run-out | Order email |\n|---|---|---|---|---|---|---|\n"
             + "\n".join(rows) + "\n")
    notes = []
    if per.suggested.any():
        notes.append("\\* no preferred supplier in B1: suggested from purchase history (cheapest in the last 12 months, "
                     "else the latest supplier); set the preferred supplier in the item master to fix it")
    if len(no_supplier):
        notes.append(f"{no_supplier.ItemCode.nunique()} item(s) have never been bought, so no supplier could be suggested: "
                     + ", ".join(no_supplier.ItemName.unique()[:8]))
    notes.append("Values use the stock cost of each item; the email drafts use each supplier's last price. Drafts only: "
                 "nothing is sent, and no purchase order is created in SAP")
    shown += "\n" + "\n".join(f"*{n}.*" for n in notes) + "\n"
    return {"_display": shown, "currency": cur, "suppliers": len(per), "estimated_total_at_cost": round(total, 2),
            "urgent_items": int(per.urgent.sum()), "orders": plan,
            "items_without_any_supplier": list(no_supplier.ItemName.unique()),
            "instruction": "The plan table and email drafts are already shown. In 2-4 sentences say which order matters "
                           "most and why (urgent items, run-out dates), using these exact figures. Do not repeat the table."}


def briefing_lines() -> list[str]:
    """Short to-do lines for the morning briefing (no drafts there; the chat makes them on request)."""
    out = []
    try:
        c = collections_plan(top=3, drafts=0)
        if c.get("customers_overdue"):
            out.append(f"- **Collections:** {c['customers_overdue']} customer(s) overdue, {money(c['total_overdue'], c['currency'])}. "
                       "First: " + "; ".join(f"{x['CardName']} {money(x['overdue'], c['currency'])}, {qty(x['oldest'])} days late ({x['step'][0].lower() + x['step'][1:]})"
                                             for x in c["customers"]) + ".")
        else:
            out.append(f"- **Collections:** {c.get('note', 'nobody overdue')}")
    except Exception as e:
        out.append(f"- Collections plan not available ({type(e).__name__}).")
    try:
        p = purchasing_plan(drafts=False)
        if p.get("suppliers"):
            first = p["orders"][0]
            out.append(f"- **Purchasing:** {p['suppliers']} proposed order(s), {money(p['estimated_total_at_cost'], p['currency'])}; "
                       f"first: {first['supplier']} ({first['items']} item(s)"
                       f"{', ' + str(first['urgent_items']) + ' urgent' if first['urgent_items'] else ''}, "
                       f"{'already out of stock' if first['earliest_runout'] == 'already out' else 'runs out ' + first['earliest_runout']}).")
        else:
            out.append(f"- **Purchasing:** {p.get('note', 'nothing to order')}")
    except Exception as e:
        out.append(f"- Purchasing plan not available ({type(e).__name__}).")
    out.append("- Ask the chat for the *collections plan* or *purchasing plan* to get the ready reminders and order emails.")
    return out
