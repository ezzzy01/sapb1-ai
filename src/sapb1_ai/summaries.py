"""One-page summaries (Joule-style "summarize"): a customer, a supplier, or any B1 document by its number.

Master data (contact, terms, credit limit) and documents are read live from B1 with the read-only login; history
(sales, purchases, payments, deliveries) comes from the nightly Parquet files. Nothing is written anywhere.
Amounts are in the company's local currency unless a field says otherwise; invoice and order totals include tax,
sales and purchase figures are before tax and after discounts.
"""

import json
from datetime import date

import pandas as pd

from .db import query
from .numbers import label
from .sync import DATA_DIR

RECENT = 5  # recent documents / top items shown


def _read(name: str, *dates: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{name}.parquet")
    for d in dates:
        df[d] = pd.to_datetime(df[d])
    return df


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.replace([float("inf"), float("-inf")], None)
    df = df.round({c: 2 for c in df.select_dtypes("number").columns})
    return json.loads(df.to_json(orient="records", date_format="iso").replace("T00:00:00.000", ""))


def _num(v) -> float:
    return round(float(v), 2) if pd.notna(v) else 0.0


def _currency() -> str:
    try:
        return label(json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or "")
    except (OSError, ValueError):
        return ""


def _find_partner(text: str, card_type: str) -> dict:
    """Business partner by exact code, else by name; {'match': row} or {'candidates': [...]} or {'error': ...}."""
    kind = "customer" if card_type == "C" else "supplier"
    text = (text or "").strip()
    if not text:
        return {"error": f"Give the {kind} name or code."}
    sql = ("SELECT TOP 10 p.CardCode, p.CardName, p.Phone1, p.Cellular, p.E_Mail, p.CntctPrsn, p.City, p.Country, "
           "p.Currency, p.Balance, p.OrdersBal, p.DNotesBal, p.CreditLine, t.PymntGroup, s.SlpName, p.frozenFor, "
           "p.CreateDate FROM OCRD p LEFT JOIN OCTG t ON t.GroupNum = p.GroupNum LEFT JOIN OSLP s ON s.SlpCode = p.SlpCode "
           "WHERE p.CardType = ? AND {cond} ORDER BY p.CardCode")
    rows = query(sql.format(cond="UPPER(p.CardCode) = UPPER(?)"), (card_type, text))
    if rows.empty:
        rows = query(sql.format(cond="(p.CardName LIKE ? OR p.CardCode LIKE ?)"), (card_type, f"%{text}%", f"%{text}%"))
    if rows.empty:
        return {"error": f"No {kind} matching '{text}'. Check the spelling or give the {kind} code."}
    exact = rows[rows.CardName.str.lower() == text.lower()]
    if len(rows) > 1 and len(exact) != 1:
        return {"candidates": _records(rows[["CardCode", "CardName", "City"]]),
                "note": f"Several {kind}s match '{text}'. Ask the user which one, or call again with the code."}
    return {"match": (exact if len(exact) == 1 else rows).iloc[0].to_dict()}


def _partner_head(p: dict) -> dict:
    return {
        "code": p["CardCode"], "name": p["CardName"],
        "contact_person": p["CntctPrsn"], "phone": p["Phone1"] or p["Cellular"], "email": p["E_Mail"],
        "city": p["City"], "country": p["Country"], "payment_terms": p["PymntGroup"],
        "status": "Inactive" if p["frozenFor"] == "Y" else "Active",
        "partner_since": str(pd.Timestamp(p["CreateDate"]).date()) if pd.notna(p["CreateDate"]) else None,
        "account_currency": label(p["Currency"] or ""),
    }


def _period_sums(df: pd.DataFrame, date_col: str, value_col: str, today: pd.Timestamp) -> dict:
    last = df[df[date_col] > today - pd.DateOffset(months=12)][value_col].sum()
    before = df[(df[date_col] <= today - pd.DateOffset(months=12)) & (df[date_col] > today - pd.DateOffset(months=24))][value_col].sum()
    return {"last_12_months": _num(last), "previous_12_months": _num(before),
            "change_pct": _num((last / before - 1) * 100) if before else None}


def customer_summary(customer: str, limit: int = RECENT) -> dict:
    """Everything about one customer on one page."""
    limit = max(1, min(int(limit or RECENT), 200))
    found = _find_partner(customer, "C")
    if "match" not in found:
        return found
    p = found["match"]
    code, today, cur = p["CardCode"], pd.Timestamp(date.today()), _currency()
    out = {"currency": cur, "as_of": str(today.date()), "customer": _partner_head(p) | {
        "salesperson": p["SlpName"] if p["SlpName"] and not str(p["SlpName"]).startswith("-") else None,
        "credit_limit": _num(p["CreditLine"]) or None}}

    # What they owe us (open invoices, incl. tax)
    ar = _read("ar_open", "DocDate", "DocDueDate")
    ar = ar[ar.CardCode == code].assign(days_late=lambda d: (today - d.DocDueDate).dt.days.clip(lower=0))
    owed, overdue = ar.Balance.sum(), ar.Balance[ar.days_late > 0].sum()
    out["owes_us"] = {"total": _num(owed), "overdue": _num(overdue), "open_invoices": len(ar),
                      "oldest_days_late": int(ar.days_late.max()) if len(ar) else 0}
    if p["CreditLine"]:
        out["owes_us"]["credit_used_pct"] = _num((owed + p["OrdersBal"]) / p["CreditLine"] * 100)
    if len(ar):
        out["unpaid_invoices"] = _records(ar.sort_values("DocDueDate").head(limit)[
            ["DocNum", "DocDate", "DocDueDate", "DocTotal", "Balance", "days_late"]])

    # Open sales orders not yet delivered (before tax)
    so = _read("open_sales", "NeedDate")
    so = so[so.CardCode == code]
    if len(so):
        g = so.groupby("DocNum").agg(open_value=("OpenValue", "sum"), lines=("ItemCode", "size"), due=("NeedDate", "min")).reset_index()
        g["late"] = g.due < today
        out["open_orders"] = {"orders": len(g), "open_value": _num(g.open_value.sum()), "late_orders": int(g.late.sum()),
                              "list": _records(g.sort_values("due").head(limit))}
    else:
        out["open_orders"] = {"orders": 0, "open_value": 0.0}

    # Sales (before tax, after discounts) and gross profit
    lines = _read("ar_lines", "DocDate")
    lines = lines[lines.CardCode == code]
    disc = lines.DocDisc.fillna(0) / 100
    lines = lines.assign(net=lines.LineTotal * (1 - disc), gp=lines.GrssProfit - lines.LineTotal * disc)
    recent = lines[lines.DocDate > today - pd.DateOffset(months=12)]
    out["sales"] = _period_sums(lines, "DocDate", "net", today) | {
        "gross_profit_last_12_months": _num(recent.gp.sum()),
        "margin_pct_last_12_months": _num(recent.gp.sum() / recent.net.sum() * 100) if recent.net.sum() else None,
        "invoices_last_12_months": int(recent.DocEntry.nunique()),
        "first_invoice": str(lines.DocDate.min().date()) if len(lines) else None,
        "last_invoice": str(lines.DocDate.max().date()) if len(lines) else None,
        "lifetime_sales": _num(lines.net.sum())}
    if len(recent):
        top = (recent.groupby(["ItemCode", "Dscription"]).agg(qty=("InvQty", "sum"), net_sales=("net", "sum"))
               .reset_index().sort_values("net_sales", ascending=False).head(limit))
        out["top_items_last_12_months"] = _records(top)

    # Payment habits: how many days after the due date they really pay (weighted by amount)
    pay = _read("ar_payments", "PayDate", "DocDueDate")
    pay = pay[(pay.CardCode == code) & (pay.PayDate > today - pd.DateOffset(months=24))]
    if len(pay) and pay.SumApplied.sum():
        late = (pay.PayDate - pay.DocDueDate).dt.days
        out["payment_habits"] = {
            "avg_days_paid_after_due": _num((late * pay.SumApplied).sum() / pay.SumApplied.sum()),
            "paid_on_time_pct": _num((late <= 0).mean() * 100), "payments_counted": len(pay),
            "note": "Negative days = paid before the due date. Last 24 months."}

    # Flags from the nightly analysis
    flags = []
    try:
        fin = json.loads((DATA_DIR / "finance.json").read_text())
        for c in fin.get("churn", {}).get("customers", []):
            if c.get("CardCode") == code:
                flags.append(f"Stopped ordering: silent {c['days_silent']} days, usually orders every "
                             f"{c['usual_days_between_orders']} days.")
        for c in fin.get("late_payers", {}).get("worst_payers", []):
            if c.get("CardCode") == code:
                flags.append("One of the latest payers.")
    except (OSError, ValueError):
        pass
    if overdue > 0:
        flags.append(f"Has overdue invoices (oldest {out['owes_us']['oldest_days_late']} days late).")
    if p["CreditLine"] and out["owes_us"].get("credit_used_pct", 0) > 100:
        flags.append("Over the credit limit (open invoices plus open orders).")
    out["flags"] = flags
    return out


def supplier_summary(supplier: str, limit: int = RECENT) -> dict:
    """Everything about one supplier on one page."""
    limit = max(1, min(int(limit or RECENT), 200))
    found = _find_partner(supplier, "S")
    if "match" not in found:
        return found
    p = found["match"]
    code, today, cur = p["CardCode"], pd.Timestamp(date.today()), _currency()
    out = {"currency": cur, "as_of": str(today.date()), "supplier": _partner_head(p)}

    # What we owe them (open A/P invoices, incl. tax)
    ap = _read("ap_invoices", "DocDate", "DocDueDate")
    ap = ap[(ap.CardCode == code) & (ap.DocStatus == "O")].assign(Balance=lambda d: d.DocTotal - d.PaidToDate)
    ap = ap[ap.Balance > 0].assign(days_to_due=lambda d: (d.DocDueDate - today).dt.days)
    out["we_owe"] = {"total": _num(ap.Balance.sum()), "overdue": _num(ap.Balance[ap.days_to_due < 0].sum()),
                     "due_next_14_days": _num(ap.Balance[ap.days_to_due.between(0, 14)].sum()), "open_invoices": len(ap)}
    if len(ap):
        out["unpaid_invoices"] = _records(ap.sort_values("DocDueDate").head(limit)[
            ["DocNum", "NumAtCard", "DocDate", "DocDueDate", "DocTotal", "Balance", "days_to_due"]])

    # Open purchase orders (before tax)
    po = _read("open_purchases", "DueDate")
    po = po[po.CardCode == code]
    if len(po):
        g = po.groupby("DocNum").agg(open_value=("OpenValue", "sum"), lines=("ItemCode", "size"), due=("DueDate", "min")).reset_index()
        g["late"] = g.due < today
        out["open_purchase_orders"] = {"orders": len(g), "open_value": _num(g.open_value.sum()), "late_orders": int(g.late.sum()),
                                       "list": _records(g.sort_values("due").head(limit))}
    else:
        out["open_purchase_orders"] = {"orders": 0, "open_value": 0.0}

    # Purchases (before tax, after discounts) and price trend
    lines = _read("ap_lines", "DocDate")
    lines = lines[lines.CardCode == code]
    lines = lines.assign(net=lines.LineTotal * (1 - lines.DocDisc.fillna(0) / 100))
    recent = lines[lines.DocDate > today - pd.DateOffset(months=12)]
    out["purchases"] = _period_sums(lines, "DocDate", "net", today) | {
        "invoices_last_12_months": int(recent.DocEntry.nunique()),
        "last_invoice": str(lines.DocDate.max().date()) if len(lines) else None}
    if len(recent):
        top = (recent.groupby(["ItemCode", "Dscription"]).agg(qty=("InvQty", "sum"), spend=("net", "sum"))
               .reset_index().sort_values("spend", ascending=False).head(limit))
        top["avg_unit_price"] = top.spend / top.qty.where(top.qty != 0)
        before = lines[(lines.DocDate <= today - pd.DateOffset(months=12)) & (lines.DocDate > today - pd.DateOffset(months=24))]
        b = before.groupby("ItemCode").agg(spend=("net", "sum"), qty=("InvQty", "sum"))
        prev = (b.spend / b.qty.where(b.qty != 0)).reindex(top.ItemCode).values
        top["price_change_pct_vs_year_before"] = (top.avg_unit_price.values / prev - 1) * 100
        out["top_items_last_12_months"] = _records(top)

    # Delivery: purchase order to goods receipt, last 24 months
    lt = _read("lead_times", "GRPODate")
    lt = lt[(lt.CardCode == code) & (lt.GRPODate > today - pd.DateOffset(months=24)) & (lt.LeadDays >= 0)]
    if len(lt):
        out["delivery"] = {"receipts": len(lt), "on_time_pct": _num((lt.DaysLate <= 0).mean() * 100),
                           "median_lead_days": _num(lt.LeadDays.median()),
                           "avg_days_late_when_late": _num(lt.DaysLate[lt.DaysLate > 0].mean()) if (lt.DaysLate > 0).any() else 0.0,
                           "note": "Per receipt line; on time = received by the PO due date. Last 24 months."}

    # How we pay them
    pay = _read("ap_payments_applied", "PayDate", "DocDueDate")
    pay = pay[(pay.CardCode == code) & (pay.PayDate > today - pd.DateOffset(months=24))]
    if len(pay) and pay.SumApplied.sum():
        late = (pay.PayDate - pay.DocDueDate).dt.days
        out["our_payments"] = {"avg_days_paid_after_due": _num((late * pay.SumApplied).sum() / pay.SumApplied.sum()),
                               "payments_counted": len(pay), "note": "Negative = we paid before the due date."}

    # Open red flags from the nightly checks
    flags = []
    path = DATA_DIR / "anomalies.csv"
    if path.exists():
        an = pd.read_csv(path)
        an = an[(an.partner == p["CardName"]) | an.doc.astype(str).str.contains(code, regex=False)]
        flags = [f"{r.type} ({r.severity}): {r.reason}" for r in an.head(5).itertuples()]
    if out["we_owe"]["overdue"] > 0:
        flags.append("We have overdue invoices with this supplier.")
    out["flags"] = flags
    return out


# ---------- documents ----------

# kind -> (header table, lines table, object type, name, where to open it in B1)
DOCS = {
    "sales_quotation": ("OQUT", "QUT1", 23, "Sales Quotation", "Sales - A/R > Sales Quotation"),
    "sales_order": ("ORDR", "RDR1", 17, "Sales Order", "Sales - A/R > Sales Order"),
    "delivery": ("ODLN", "DLN1", 15, "Delivery", "Sales - A/R > Delivery"),
    "return": ("ORDN", "RDN1", 16, "Return", "Sales - A/R > Return"),
    "ar_down_payment": ("ODPI", "DPI1", 203, "A/R Down Payment Invoice", "Sales - A/R > A/R Down Payment Invoice"),
    "ar_invoice": ("OINV", "INV1", 13, "A/R Invoice", "Sales - A/R > A/R Invoice"),
    "ar_credit_memo": ("ORIN", "RIN1", 14, "A/R Credit Memo", "Sales - A/R > A/R Credit Memo"),
    "purchase_quotation": ("OPQT", "PQT1", 540000006, "Purchase Quotation", "Purchasing - A/P > Purchase Quotation"),
    "purchase_order": ("OPOR", "POR1", 22, "Purchase Order", "Purchasing - A/P > Purchase Order"),
    "goods_receipt_po": ("OPDN", "PDN1", 20, "Goods Receipt PO", "Purchasing - A/P > Goods Receipt PO"),
    "goods_return": ("ORPD", "RPD1", 21, "Goods Return", "Purchasing - A/P > Goods Return"),
    "ap_invoice": ("OPCH", "PCH1", 18, "A/P Invoice", "Purchasing - A/P > A/P Invoice"),
    "ap_credit_memo": ("ORPC", "RPC1", 19, "A/P Credit Memo", "Purchasing - A/P > A/P Credit Memo"),
    "incoming_payment": ("ORCT", "RCT2", 24, "Incoming Payment", "Banking > Incoming Payments > Incoming Payments"),
    "outgoing_payment": ("OVPM", "VPM2", 46, "Outgoing Payment", "Banking > Outgoing Payments > Outgoing Payments"),
    "return_request": ("ORRR", "RRR1", 234000031, "Return Request", "Sales - A/R > Return Request"),
    "purchase_request": ("OPRQ", "PRQ1", 1470000113, "Purchase Request", "Purchasing - A/P > Purchase Request"),
    "goods_return_request": ("OPRR", "PRR1", 234000032, "Goods Return Request", "Purchasing - A/P > Goods Return Request"),
    "goods_receipt": ("OIGN", "IGN1", 59, "Goods Receipt", "Inventory > Inventory Transactions > Goods Receipt"),
    "goods_issue": ("OIGE", "IGE1", 60, "Goods Issue", "Inventory > Inventory Transactions > Goods Issue"),
    "inventory_transfer": ("OWTR", "WTR1", 67, "Inventory Transfer", "Inventory > Inventory Transactions > Inventory Transfer"),
    "transfer_request": ("OWTQ", "WTQ1", 1250000001, "Inventory Transfer Request",
                         "Inventory > Inventory Transactions > Inventory Transfer Request"),
}
BY_OBJTYPE = {v[2]: k for k, v in DOCS.items()}
ALIASES = {  # words people use -> kind
    "quotation": "sales_quotation", "quote": "sales_quotation", "sq": "sales_quotation",
    "order": "sales_order", "so": "sales_order", "sales order": "sales_order",
    "do": "delivery", "delivery order": "delivery", "delivery note": "delivery",
    "invoice": "ar_invoice", "inv": "ar_invoice", "sales invoice": "ar_invoice", "a/r invoice": "ar_invoice",
    "credit note": "ar_credit_memo", "credit memo": "ar_credit_memo", "cn": "ar_credit_memo",
    "down payment": "ar_down_payment", "po": "purchase_order", "purchase order": "purchase_order",
    "grpo": "goods_receipt_po", "goods receipt": "goods_receipt_po", "bill": "ap_invoice",
    "supplier invoice": "ap_invoice", "purchase invoice": "ap_invoice", "a/p invoice": "ap_invoice",
    "supplier credit note": "ap_credit_memo", "receipt": "incoming_payment", "payment received": "incoming_payment",
    "payment": "outgoing_payment", "payment made": "outgoing_payment",
    "return request": "return_request", "purchase request": "purchase_request", "pr": "purchase_request",
    "goods return request": "goods_return_request", "goods issue": "goods_issue", "transfer": "inventory_transfer",
    "stock transfer": "inventory_transfer", "inventory transfer": "inventory_transfer", "transfer request": "transfer_request",
}


def _kind(text: str) -> str | None:
    t = (text or "").strip().lower().replace("-", " ").replace("_", " ")
    for k in DOCS:
        if t == k.replace("_", " "):
            return k
    return ALIASES.get(t)


def _status(h: dict) -> str:
    if str(h.get("CANCELED", h.get("Canceled", "N"))) in ("Y", "C"):
        return "Cancelled"
    return {"O": "Open", "C": "Closed"}.get(h.get("DocStatus"), "Posted")


def _address(a) -> str | None:
    lines = [x.strip() for x in str(a or "").replace("\r\n", "\r").replace("\n", "\r").split("\r") if x.strip()]
    return ", ".join(lines) or None


def _payment(kind: str, number: int) -> list[dict]:
    head, lines, _, name, menu = DOCS[kind]
    inv_types = {13: "OINV", 14: "ORIN", 18: "OPCH", 19: "ORPC", 203: "ODPI"}
    h = query(f"SELECT h.DocEntry, h.DocNum, n.SeriesName, h.DocDate, h.CardCode, h.CardName, h.DocCurr AS DocCur, "
              f"h.DocTotal, h.CashSum, h.TrsfrSum, h.CheckSum, h.CreditSum, h.TrsfrRef, h.Comments, h.Canceled "
              f"FROM {head} h LEFT JOIN NNM1 n ON n.Series = h.Series WHERE h.DocNum = ?", (number,))
    found = []
    for r in h.to_dict("records"):
        applied = query(f"SELECT InvType, DocEntry, SumApplied FROM {lines} WHERE DocNum = ?", (r["DocEntry"],))
        paid = []
        for a in applied.to_dict("records"):
            t = inv_types.get(int(a["InvType"]))
            num = query(f"SELECT DocNum FROM {t} WHERE DocEntry = ?", (a["DocEntry"],)).DocNum if t else pd.Series(dtype=int)
            kind_paid = BY_OBJTYPE.get(int(a["InvType"]))
            paid.append({"document": f"{DOCS[kind_paid][3]} {int(num.iloc[0])}" if kind_paid and len(num) else f"type {a['InvType']}",
                         "amount_applied": _num(a["SumApplied"])})
        method = {k: _num(r[c]) for k, c in (("cash", "CashSum"), ("bank_transfer", "TrsfrSum"), ("cheque", "CheckSum"),
                                             ("credit_card", "CreditSum")) if r[c]}
        found.append({"type": name, "number": int(r["DocNum"]), "series": r["SeriesName"], "status": _status(r),
                      "date": str(pd.Timestamp(r["DocDate"]).date()), "partner": f"{r['CardName']} ({r['CardCode']})",
                      "currency": label(r["DocCur"] or ""), "total": _num(r["DocTotal"]), "paid_by": method,
                      "reference": r["TrsfrRef"], "remarks": r["Comments"], "applied_to": paid, "where_in_b1": menu})
    return found


def document(kind: str, number: int) -> dict:
    """One B1 document by type and number: header, lines, what it was copied from and copied to."""
    k = _kind(kind)
    if not k:
        return {"error": f"Unknown document type '{kind}'. Use one of: {', '.join(DOCS)}."}
    try:
        number = int(str(number).strip().lstrip("#"))
    except ValueError:
        return {"error": f"'{number}' is not a document number."}
    cur = _currency()
    if k in ("incoming_payment", "outgoing_payment"):
        found = _payment(k, number)
    else:
        head, lines, _, name, menu = DOCS[k]
        h = query(f"SELECT h.DocEntry, h.DocNum, n.SeriesName, h.DocDate, h.DocDueDate, h.CardCode, h.CardName, h.NumAtCard, "
                  f"h.DocStatus, h.CANCELED, h.DocCur, h.DocRate, h.DocTotal, h.DocTotalFC, h.VatSum, h.DiscPrcnt, "
                  f"h.PaidToDate, h.Comments, s.SlpName, t.PymntGroup, h.Address, h.Address2 "
                  f"FROM {head} h LEFT JOIN NNM1 n ON n.Series = h.Series LEFT JOIN OSLP s ON s.SlpCode = h.SlpCode "
                  f"LEFT JOIN OCTG t ON t.GroupNum = h.GroupNum WHERE h.DocNum = ?", (number,))
        found = []
        for r in h.to_dict("records"):
            ln = query(f"SELECT LineNum, ItemCode, Dscription, Quantity, unitMsr, Price, Currency, DiscPrcnt, LineTotal, TotalFrgn, WhsCode, "
                       f"LineStatus, OpenQty, BaseType, BaseRef, TargetType, TrgetEntry FROM {lines} WHERE DocEntry = ? "
                       f"ORDER BY VisOrder", (r["DocEntry"],))
            copied_from = sorted({f"{DOCS[BY_OBJTYPE[int(b)]][3]} {ref}" for b, ref in zip(ln.BaseType, ln.BaseRef)
                                  if pd.notna(b) and int(b) in BY_OBJTYPE and ref})
            copied_to = set()
            for t, e in {(int(t), int(e)) for t, e in zip(ln.TargetType, ln.TrgetEntry) if pd.notna(t) and int(t) in BY_OBJTYPE and pd.notna(e)}:
                n = query(f"SELECT DocNum FROM {DOCS[BY_OBJTYPE[t]][0]} WHERE DocEntry = ?", (e,)).DocNum
                if len(n):
                    copied_to.add(f"{DOCS[BY_OBJTYPE[t]][3]} {int(n.iloc[0])}")
            foreign = bool(r["DocCur"]) and label(r["DocCur"]) != cur
            cols = ["LineNum", "ItemCode", "Description", "Quantity", "Unit", "Price", "PriceCurrency", "Disc%",
                    f"LineTotal_before_tax ({cur})"] + (["LineTotal_doc_currency"] if foreign else []) + ["WhsCode"]
            if k in ("sales_quotation", "sales_order", "delivery", "purchase_quotation", "purchase_order", "goods_receipt_po") \
                    and r["DocStatus"] == "O" and r["CANCELED"] == "N":
                cols += ["LineStatus", "OpenQty"]  # what is still to be delivered / received / copied on
            ln = ln.assign(Currency=ln.Currency.fillna("").map(label), LineStatus=ln.LineStatus.map({"O": "Open", "C": "Closed"}))
            doc = {"type": name, "number": int(r["DocNum"]), "series": r["SeriesName"], "status": _status(r),
                   "date": str(pd.Timestamp(r["DocDate"]).date()), "due_date": str(pd.Timestamp(r["DocDueDate"]).date()),
                   "partner": f"{r['CardName']} ({r['CardCode']})", "partner_reference": r["NumAtCard"],
                   "salesperson_or_buyer": r["SlpName"] if r["SlpName"] and not str(r["SlpName"]).startswith("-") else None,
                   "payment_terms": r["PymntGroup"], "currency": label(r["DocCur"] or ""),
                   f"total_incl_tax ({label(r['DocCur'] or '') or cur})": _num(r["DocTotalFC"] if foreign else r["DocTotal"]),
                   f"tax ({cur})": _num(r["VatSum"]),
                   "document_discount_pct": _num(r["DiscPrcnt"]), "remarks": r["Comments"],
                   "bill_to": _address(r["Address"]), "ship_to": _address(r["Address2"]),
                   "lines": _records(ln.rename(columns={"Dscription": "Description", "unitMsr": "Unit", "DiscPrcnt": "Disc%",
                                                        "Currency": "PriceCurrency", "LineTotal": f"LineTotal_before_tax ({cur})",
                                                        "TotalFrgn": "LineTotal_doc_currency"})[cols]),
                   "copied_from": copied_from, "copied_to": sorted(copied_to), "where_in_b1": menu}
            if foreign:
                doc[f"total_incl_tax_in_local_currency ({cur})"] = _num(r["DocTotal"])
                doc["note"] = (f"Document in {label(r['DocCur'])}; line prices are in PriceCurrency, line totals in {cur}. "
                               "Always name the currency next to each amount.")
            if k in ("ar_invoice", "ap_invoice", "ar_down_payment"):
                doc["paid"] = _num(r["PaidToDate"])
                doc["balance_due"] = _num(r["DocTotal"] - r["PaidToDate"]) if doc["status"] == "Open" else 0.0
                late = (pd.Timestamp(date.today()) - pd.Timestamp(r["DocDueDate"])).days
                if doc["balance_due"] > 0:
                    doc["days_overdue"] = max(late, 0)
            found.append(doc)
    if not found:
        return {"error": f"No {DOCS[k][3]} number {number} in B1.",
                "note": "Check the number and the document type (e.g. sales order vs invoice)."}
    today = str(date.today())
    if len(found) == 1:
        return {"local_currency": cur, "today": today, "document": found[0]}
    return {"local_currency": cur, "today": today, "documents": found[:3],
            "note": f"{len(found)} documents have this number (different numbering series); ask which series if unclear."}
