"""Sales analysis by invoice, finished in one lookup (live from B1, read-only).

AR invoices net of credit memos: key figures, by month / customer / item, open and overdue balances, invoice list.
The report goes to the user as it is (like a chart); the model only gets the headline figures to add a few insights.
Without this, "sales analysis" questions made the model build the report from raw run_sql rows, one query after another.
"""

import datetime as dt
import logging
import re

import pandas as pd

from .db import query
from .export import excel_link

MAX_INVOICE_ROWS = 30  # in the chat; the Excel download has every document
TOP = 10

# CANCELED: 'N' normal, 'Y' cancelled original, 'C' the cancellation document; only 'N' counts.
DOC_SQL = """
    SELECT 'Invoice' AS DocType, DocNum, DocDate, DocDueDate, CardCode, CardName,
           DocTotal - VatSum AS NetTotal, VatSum, DocTotal, PaidToDate, DocStatus
    FROM OINV WHERE CANCELED = 'N' AND DocDate >= ? AND DocDate < ?{c}
    UNION ALL
    SELECT 'Credit memo', DocNum, DocDate, DocDueDate, CardCode, CardName,
           -(DocTotal - VatSum), -VatSum, -DocTotal, -PaidToDate, DocStatus
    FROM ORIN WHERE CANCELED = 'N' AND DocDate >= ? AND DocDate < ?{c}
"""
ITEM_SQL = """
    SELECT L.ItemCode, MAX(L.Dscription) AS ItemName, SUM(L.Quantity) AS Qty, SUM(L.LineTotal) AS Net, SUM(L.GrssProfit) AS GP
    FROM INV1 L JOIN OINV H ON H.DocEntry = L.DocEntry
    WHERE H.CANCELED = 'N' AND H.DocDate >= ? AND H.DocDate < ?{c} GROUP BY L.ItemCode
    UNION ALL
    SELECT L.ItemCode, MAX(L.Dscription), -SUM(L.Quantity), -SUM(L.LineTotal), -SUM(L.GrssProfit)
    FROM RIN1 L JOIN ORIN H ON H.DocEntry = L.DocEntry
    WHERE H.CANCELED = 'N' AND H.DocDate >= ? AND H.DocDate < ?{c} GROUP BY L.ItemCode
"""


def _parse_date(text: str, end: bool) -> dt.date | None:
    """YYYY-MM-DD, DD/MM/YYYY, YYYY-MM or YYYY; for an end date, YYYY-MM / YYYY mean the end of that month / year."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}-\d{1,2}", text):
        d = dt.datetime.strptime(text, "%Y-%m").date()
        return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1) if end else d
    if re.fullmatch(r"\d{4}", text):
        return dt.date(int(text), 12, 31) if end else dt.date(int(text), 1, 1)
    raise ValueError(f"Unrecognised date {text!r}: use YYYY-MM-DD, YYYY-MM or YYYY.")


def _fetch(d_from: dt.date, d_to: dt.date, customer: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    cond, params = "", [d_from, d_to + dt.timedelta(days=1)]
    if customer:
        cond = " AND (CardCode LIKE ? OR CardName LIKE ?)"
        params += [f"%{customer}%"] * 2
    docs = query(DOC_SQL.format(c=cond), tuple(params * 2))
    items = query(ITEM_SQL.format(c=cond.replace("CardCode", "H.CardCode").replace("CardName", "H.CardName")), tuple(params * 2))
    return docs, items


def _money(v: float) -> str:
    return f"{v:,.2f}"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "-"


def sales_analysis(date_from: str = "", date_to: str = "", customer: str = "") -> dict:
    today = dt.date.today()
    d_from = _parse_date(date_from, end=False)
    d_to = _parse_date(date_to, end=True) or today
    d_from = d_from or dt.date(d_to.year, 1, 1)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    customer = (customer or "").strip()
    currency = query("SELECT MainCurncy FROM OADM").MainCurncy.iloc[0]

    docs, items = _fetch(d_from, d_to, customer)
    if docs.empty and not (date_from or date_to):
        # Nothing this year: report the 12 months up to the latest invoice instead of sending the model off to guess dates.
        last = query("SELECT MAX(DocDate) AS d FROM OINV WHERE CANCELED = 'N'").d.iloc[0]
        if pd.notna(last):
            d_to = pd.Timestamp(last).date()
            d_from = (pd.Timestamp(d_to) - pd.DateOffset(months=12) + pd.Timedelta(days=1)).date()
            docs, items = _fetch(d_from, d_to, customer)

    title = f"Sales analysis by invoice: {d_from:%d %b %Y} to {d_to:%d %b %Y}" + (f" (customer: {customer})" if customer else "")
    if docs.empty:
        return {"period": [str(d_from), str(d_to)], "customer_filter": customer, "invoices": 0,
                "note": "No customer invoices or credit memos in this period. Say so and suggest another period; do not retry."}

    docs["DocDate"] = pd.to_datetime(docs.DocDate)
    docs["DocDueDate"] = pd.to_datetime(docs.DocDueDate)
    inv = docs[docs.DocType == "Invoice"]
    cm = docs[docs.DocType != "Invoice"]
    docs["balance"] = 0.0
    is_open = (docs.DocType == "Invoice") & (docs.DocStatus == "O")
    docs.loc[is_open, "balance"] = docs.DocTotal - docs.PaidToDate
    overdue = is_open & (docs.DocDueDate.dt.date < today)

    gross, credits = float(inv.NetTotal.sum()), float(cm.NetTotal.sum())
    net = gross + credits
    gp = float(items.GP.sum()) if not items.empty else 0.0
    kpi = {
        "invoices": len(inv), "credit_memos": len(cm), "gross_invoiced_excl_tax": round(gross, 2),
        "credit_memos_excl_tax": round(credits, 2), "net_sales_excl_tax": round(net, 2),
        "tax": round(float(docs.VatSum.sum()), 2), "total_incl_tax": round(float(docs.DocTotal.sum()), 2),
        "gross_profit": round(gp, 2), "margin_pct": round(gp / net * 100, 1) if net else None,
        "average_invoice_excl_tax": round(gross / len(inv), 2) if len(inv) else 0,
        "open_invoices": int(is_open.sum()), "outstanding": round(float(docs.balance.sum()), 2),
        "overdue_invoices": int(overdue.sum()), "overdue_amount": round(float(docs.loc[overdue, "balance"].sum()), 2),
    }

    month = (docs.assign(m=docs.DocDate.dt.strftime("%Y-%m"), n=(docs.DocType == "Invoice").astype(int))
             .groupby("m").agg(n=("n", "sum"), net=("NetTotal", "sum")).sort_index())
    cust = (docs.assign(n=(docs.DocType == "Invoice").astype(int))
            .groupby(["CardCode"]).agg(name=("CardName", "first"), n=("n", "sum"), net=("NetTotal", "sum"), open=("balance", "sum"))
            .sort_values("net", ascending=False))
    item = (items.assign(ItemCode=items.ItemCode.fillna("(no item)"))
            .groupby("ItemCode").agg(name=("ItemName", "first"), qty=("Qty", "sum"), net=("Net", "sum"), gp=("GP", "sum"))
            .sort_values("net", ascending=False)) if not items.empty else pd.DataFrame(columns=["name", "qty", "net", "gp"])

    out = [f"## {title}", f"All amounts in {currency}; net = excluding tax.", "", "### Key figures",
           "| | |", "|---|---:|",
           f"| Invoices / credit memos | {kpi['invoices']} / {kpi['credit_memos']} |",
           f"| Invoiced (excl. tax) | {_money(gross)} |",
           f"| Less credit memos | {_money(credits)} |",
           f"| **Net sales** | **{_money(net)}** |",
           f"| Tax charged | {_money(kpi['tax'])} |",
           f"| Total incl. tax | {_money(kpi['total_incl_tax'])} |",
           f"| Gross profit | {_money(gp)} ({_pct(gp, net)} margin) |",
           f"| Average invoice (excl. tax) | {_money(kpi['average_invoice_excl_tax'])} |",
           f"| Still to be paid | {kpi['open_invoices']} invoice(s), {_money(kpi['outstanding'])} |",
           f"| Overdue | {kpi['overdue_invoices']} invoice(s), {_money(kpi['overdue_amount'])} |", ""]

    out += ["### Net sales by month", "| Month | Invoices | Net sales | Share |", "|---|---:|---:|---:|"]
    out += [f"| {m} | {int(r.n)} | {_money(r.net)} | {_pct(r.net, net)} |" for m, r in month.iterrows()]

    out += ["", f"### Net sales by customer{f' (top {TOP} of {len(cust)})' if len(cust) > TOP else ''}",
            "| Customer | Invoices | Net sales | Share | Still to be paid |", "|---|---:|---:|---:|---:|"]
    out += [f"| {r['name']} ({c}) | {int(r.n)} | {_money(r.net)} | {_pct(r.net, net)} | {_money(r.open)} |" for c, r in cust.head(TOP).iterrows()]

    out += ["", f"### Net sales by item{f' (top {TOP} of {len(item)})' if len(item) > TOP else ''}",
            "| Item | Qty | Net sales | Gross profit | Margin |", "|---|---:|---:|---:|---:|"]
    out += [f"| {r['name']} ({c}) | {r.qty:,.0f} | {_money(r.net)} | {_money(r.gp)} | {_pct(r.gp, r.net)} |" for c, r in item.head(TOP).iterrows()]

    shown = docs.sort_values(["DocDate", "DocNum"], ascending=False).head(MAX_INVOICE_ROWS)
    out += ["", f"### Invoices ({len(shown)} of {len(docs)} documents, newest first)",
            "| Type | No. | Date | Customer | Net | Tax | Total | Still to be paid | Status |", "|---|---:|---|---|---:|---:|---:|---:|---|"]
    for i, d in shown.iterrows():
        status = "Overdue" if overdue[i] else "Open" if d.DocStatus == "O" else "Closed"
        out.append(f"| {d.DocType} | {d.DocNum} | {d.DocDate:%Y-%m-%d} | {d.CardName} | {_money(d.NetTotal)} | {_money(d.VatSum)} | "
                   f"{_money(d.DocTotal)} | {_money(d.balance)} | {status} |")

    try:
        link = _excel(title, currency, kpi, month, cust, item, docs, overdue, d_from, d_to, customer)
        out[1:1] = ["", link]
    except Exception:  # the report in the chat matters more than the download
        logging.getLogger("uvicorn.error").exception("Sales analysis Excel export failed")
        link = None

    return {
        "_display": "\n".join(out) + "\n",
        "excel_download": "included above the tables" if link else "not available this time",
        "period": [str(d_from), str(d_to)], "customer_filter": customer, "currency": currency, "key_figures": kpi,
        "by_month": [{"month": m, "invoices": int(r.n), "net_sales": round(r.net, 2)} for m, r in month.iterrows()],
        "top_customers": [{"customer": r["name"], "net_sales": round(r.net, 2), "share_pct": round(r.net / net * 100, 1) if net else None,
                           "still_to_be_paid": round(r.open, 2)}
                          for _, r in cust.head(5).iterrows()],
        "top_items": [{"item": r["name"], "net_sales": round(r.net, 2), "gross_profit": round(r.gp, 2),
                       "margin_pct": round(r.gp / r.net * 100, 1) if r.net else None} for _, r in item.head(5).iterrows()],
        "instruction": "The full report with all tables is already shown to the user. Do not repeat the tables or call more tools "
                       "for it. Add only 3-5 short insights (top customer, top item, margin, what is still to be paid or overdue, "
                       "trend) using these numbers exactly; do not calculate new ones. If an Excel download is included, mention it in one "
                       "short line; never write or invent a link yourself.",
    }


def _excel(title, currency, kpi, month, cust, item, docs, overdue, d_from, d_to, customer) -> str:
    """The whole report as a workbook: key figures, by month / customer / item, and every document (not only 30)."""
    money = ("Net sales", "Still to be paid", "Gross profit", "Net", "Tax", "Total")
    labels = {
        "invoices": "Invoices", "credit_memos": "Credit memos", "gross_invoiced_excl_tax": "Invoiced (excl. tax)",
        "credit_memos_excl_tax": "Less credit memos", "net_sales_excl_tax": "Net sales", "tax": "Tax charged",
        "total_incl_tax": "Total incl. tax", "gross_profit": "Gross profit", "margin_pct": "Margin %",
        "average_invoice_excl_tax": "Average invoice (excl. tax)", "open_invoices": "Open invoices",
        "outstanding": "Still to be paid", "overdue_invoices": "Overdue invoices", "overdue_amount": "Overdue amount",
    }
    key = pd.DataFrame([{"Figure": "Report", "Value": title}, {"Figure": "Currency", "Value": currency}]
                       + [{"Figure": labels[k], "Value": v} for k, v in kpi.items()])
    by_month = month.reset_index().rename(columns={"m": "Month", "n": "Invoices", "net": "Net sales"})
    by_cust = cust.reset_index().rename(columns={"CardCode": "Customer code", "name": "Customer", "n": "Invoices",
                                                 "net": "Net sales", "open": "Still to be paid"})
    by_item = item.reset_index().rename(columns={"ItemCode": "Item code", "name": "Item", "qty": "Qty",
                                                 "net": "Net sales", "gp": "Gross profit"})
    by_item["Margin %"] = (by_item["Gross profit"] / by_item["Net sales"] * 100).where(by_item["Net sales"] != 0).round(1)
    status = pd.Series("Closed", index=docs.index).mask(docs.DocStatus == "O", "Open").mask(overdue, "Overdue")
    documents = (docs.assign(Status=status)
                 .sort_values(["DocDate", "DocNum"], ascending=False)
                 [["DocType", "DocNum", "DocDate", "DocDueDate", "CardCode", "CardName", "NetTotal", "VatSum", "DocTotal",
                   "balance", "Status"]]
                 .rename(columns={"DocType": "Type", "DocNum": "No.", "DocDate": "Date", "DocDueDate": "Due date",
                                  "CardCode": "Customer code", "CardName": "Customer", "NetTotal": "Net", "VatSum": "Tax",
                                  "DocTotal": "Total", "balance": "Still to be paid"}))
    name = f"Sales_analysis_{d_from:%Y%m%d}-{d_to:%Y%m%d}" + (f"_{customer}" if customer else "")
    period = f"{d_from:%d %b %Y} to {d_to:%d %b %Y}" + (f" · customer: {customer}" if customer else "") + f" · amounts in {currency}, net = excluding tax"
    return excel_link(name, {"Key figures": key, "By month": by_month, "By customer": by_cust, "By item": by_item,
                             "Invoices": documents}, money_cols=money, title="Sales analysis by invoice", subtitle=period,
                      charts=[{"sheet": "By month", "category": "Month", "value": "Net sales", "title": "Net sales by month"},
                              {"sheet": "By customer", "category": "Customer", "value": "Net sales", "type": "bar", "top": 10,
                               "title": "Top customers by net sales"},
                              {"sheet": "By item", "category": "Item", "value": "Net sales", "type": "bar", "top": 10,
                               "title": "Top items by net sales"}])
