"""Tools the SAP B1 AI chat assistant can call. All read-only.

Report-based tools read the files written by sync.py / forecast.py; item_details and
run_sql also query B1 live through the read-only ai_reader login.
"""

import json
import re
from datetime import datetime

import pandas as pd

from .anomalies import TYPE_NAMES
from .b1guide import b1_howto
from .charts import (cash_forecast_chart, custom_chart, inventory_status_chart, receivables_chart, sales_trend_chart,
                     stock_levels_chart, stock_projection_chart, top_chart)
from .analytics import compare_periods, customer_insights, delivery_performance, inventory_health, product_profitability, profit_and_loss, quotation_pipeline, salesperson_performance
from .sales_forecast import sales_forecast
from .pack import management_pack
from .b1create import prepare_b1_journal_entry, prepare_b1_payment, prepare_b1_production_order, prepare_b1_stock_document
from .b1drafts import create_b1_draft, prepare_b1_draft
from .db import connect, query
from .documents import company_documents
from .drafts import cover_note, payment_reminder, supplier_order_email
from .helpers import collections_plan, purchasing_plan
from .executive import executive_summary
from .export import excel_link
from .sales import sales_analysis
from .summaries import customer_summary, document, supplier_summary
from .supplier_analytics import supplier_analytics
from .numbers import label
from .sync import DATA_DIR
from .web import fx_rates, read_web_page, web_search

MAX_ROWS = 200
REPORT_COLS = [
    "status", "ItemCode", "ItemName", "WhsCode", "abc", "OnHand", "IsCommited", "available", "OnOrder", "position", "fc_weekly",
    "weeks_cover", "lead_days", "runout_date", "days_to_runout", "production_orders_at_risk", "reorder_point",
    "suggested_qty", "unit_cost", "suggested_value", "PrefVendor", "critical", "critical_reason", "action",
]


def _report() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})


def _meta() -> dict:
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    fc = pd.read_parquet(DATA_DIR / "forecast.parquet", columns=["ds"])
    meta["report_as_of_week_ending"] = str((fc.ds.min() - pd.Timedelta(weeks=1)).date())
    return meta


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.replace([float("inf"), float("-inf")], None)
    return json.loads(df.to_json(orient="records", date_format="iso"))


def stock_summary() -> dict:
    rep = _report()
    by_status = rep.groupby("status").agg(lines=("ItemCode", "size"), suggested_value=("suggested_value", "sum"))
    top = rep[rep.suggested_qty > 0].sort_values("suggested_value", ascending=False).head(5)
    return {
        "data": _meta(),
        "by_status": _records(by_status.reset_index().round(2)),
        "total_suggested_order_value": round(float(rep.suggested_value.sum()), 2),
        "top_orders_by_value": _records(top[REPORT_COLS]),
        "urgent_warnings": critical_alerts(limit=5),
    }


def critical_alerts(limit: int = 20) -> dict:
    path = DATA_DIR / "alerts.json"
    if not path.exists():
        return {"note": "No alerts generated yet."}
    alerts = json.loads(path.read_text())
    limit = max(1, min(int(limit), MAX_ROWS))
    return {"generated_at": alerts["generated_at"], "counts": alerts["counts"], "items_total": len(alerts["items"]),
            "items": alerts["items"][:limit]}


def reorder_report(status: str = "", warehouse: str = "", limit: int = 20) -> dict:
    rep = _report()
    if status:
        rep = rep[rep.status.str.upper() == status.upper()]
    else:
        rep = rep[rep.suggested_qty > 0]  # everything that needs ordering
    if warehouse:
        rep = rep[rep.WhsCode.str.upper() == warehouse.upper()]
    limit = max(1, min(int(limit), MAX_ROWS))
    rep = rep.sort_values("suggested_value", ascending=False)
    meta = _meta()
    return {"report_as_of_week_ending": meta["report_as_of_week_ending"], "currency": meta.get("currency"), "rows_total": len(rep),
            "suggested_value_total": round(float(rep.suggested_value.sum()), 2), "rows": _records(rep[REPORT_COLS].head(limit))}


def search_items(text: str) -> dict:
    items = pd.read_parquet(DATA_DIR / "items.parquet")
    hit = items[items.ItemCode.str.contains(text, case=False, regex=False) | items.ItemName.str.contains(text, case=False, regex=False)]
    return {"matches": _records(hit[["ItemCode", "ItemName", "ItmsGrpNam", "PrefVendor"]].head(30))}


def item_details(item_code: str) -> dict:
    items = pd.read_parquet(DATA_DIR / "items.parquet")
    item = items[items.ItemCode.str.upper() == item_code.upper()]
    if item.empty:
        return {"error": f"Item {item_code} not found. Use search_items to find the code."}
    code = item.ItemCode.iloc[0]
    live = query(
        "SELECT WhsCode, OnHand, IsCommited, OnOrder, OnHand - IsCommited AS Available FROM OITW "
        "WHERE ItemCode = ? AND (OnHand <> 0 OR IsCommited <> 0 OR OnOrder <> 0)",
        (code,),
    )
    rep = _report()
    fc = pd.read_parquet(DATA_DIR / "forecast.parquet")
    fc = fc[fc.unique_id.str.startswith(code + "|")]
    fc_next = fc.assign(WhsCode=fc.unique_id.str.split("|").str[1]).groupby("WhsCode").yhat.agg(next_12_weeks="sum", weekly_avg="mean").round(2)
    return {
        "item": _records(item.drop(columns=["validFor", "frozenFor"]))[0],
        "live_stock_from_b1": _records(live),
        "report_lines": _records(rep[rep.ItemCode == code][REPORT_COLS + ["lead_days_std", "safety_stock", "order_up_to", "last_sale"]]),
        "forecast_by_warehouse": _records(fc_next.reset_index()),
        "forecast_pattern": fc.pattern.iloc[0] if not fc.empty else "no recent demand",
    }


def sales_history(item_code: str, weeks: int = 26) -> dict:
    sales = pd.read_parquet(DATA_DIR / "sales.parquet")
    s = sales[sales.ItemCode.str.upper() == item_code.upper()].copy()
    if s.empty:
        return {"item_code": item_code, "weekly": [], "note": "No invoiced sales found."}
    s["week"] = pd.to_datetime(s.DocDate).dt.to_period("W-SUN").dt.end_time.dt.normalize()
    end = pd.to_datetime(sales.DocDate).max().to_period("W-SUN").end_time.normalize()
    weeks = max(1, min(int(weeks), 156))
    wk = s[s.week > end - pd.Timedelta(weeks=weeks)].groupby(["week", "WhsCode"]).agg(qty=("Qty", "sum"), revenue=("LineTotal", "sum"))
    return {"item_code": item_code, "up_to_week": str(end.date()), "weekly": _records(wk.reset_index().round(2))}


_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|into|openrowset|opendatasource|xp_\w+|sp_\w+)\b", re.I)
# Only this company's database: no Other.dbo.Table / Other..Table names, linked servers or database lists.
# (The real barrier is a SQL login per company that can open only its own database; this stops the attempt early.)
_NAME = r'(?:\[[^\]]*\]|"[^"]*"|\w+)'
_OTHER_DB = re.compile(rf"{_NAME}\s*\.\s*(?:{_NAME})?\s*\.\s*{_NAME}|\bopenquery\b|\bsys\s*\.\s*(?:databases|servers)\b", re.I)


def _sql_problem(stmt: str) -> str | None:
    if not re.match(r"^(select|with)\b", stmt, re.I) or ";" in stmt or _FORBIDDEN.search(stmt):
        return "Only a single read-only SELECT statement is allowed."
    if _OTHER_DB.search(re.sub(r"'(?:[^']|'')*'", "''", stmt)):
        return "Only this company's own database can be queried."
    return None


def run_sql(sql: str) -> dict:
    stmt = sql.strip().rstrip(";").strip()
    if problem := _sql_problem(stmt):
        return {"error": problem}
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(stmt)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:
        return {"error": str(e).split("DDBC Error: ")[-1][:500],
                "note": "This query failed. Do not run it again unchanged: fix it once, or tell the user what you could not get."}
    finally:
        conn.close()
    df = pd.DataFrame.from_records(rows[:MAX_ROWS], columns=cols)
    return {"columns": cols, "rows": _records(df.map(lambda v: float(v) if type(v).__name__ == "Decimal" else v)),
            "truncated": len(rows) > MAX_ROWS}


def market_watch() -> dict:
    path = DATA_DIR / "market_watch.json"
    if not path.exists():
        return {"note": "Market watch has not run yet."}
    mw = json.loads(path.read_text())
    return {"generated_at": mw["generated_at"], "source": mw["source"], "assessment": mw.get("assessment"), "fx": mw.get("fx"),
            "topics": [{"topic": t["topic"], "our_position": t.get("our_position"),
                        "headlines": [{"title": h["title"], "url": h["url"]} for h in t.get("headlines", [])]} for t in mw["topics"]]}


def transaction_anomalies(kind: str = "", limit: int = 20) -> dict:
    path = DATA_DIR / "anomalies.json"
    if not path.exists():
        return {"note": "Transaction checks have not run yet."}
    an = json.loads(path.read_text())
    items = [a for a in an["items"] if not kind or a["type"].upper() == kind.upper()]
    limit = max(1, min(int(limit), MAX_ROWS))
    paid = pd.Series([a["paid"] for a in items], dtype=object).value_counts().to_dict()
    return {"generated_at": an["generated_at"], "currency": an["currency"], "documents_since": an["documents_since"],
            "by_type": [t | {"meaning": TYPE_NAMES.get(t["type"], t["type"])} for t in an["by_type"]],
            "total_money_lost": round(sum(t["money_lost"] for t in an["by_type"]), 2),
            "total_amount_to_review": round(sum(t.get("amount_to_review", 0) for t in an["by_type"]), 2), "rows_total": len(items), "rows_by_payment_status": paid, "rows": items[:limit]}


FINANCE_TOPICS = ["cash_forecast", "late_payers", "supplier_payments", "margins", "churn", "cross_sell"]


def receivables(customer: str = "", limit: int = 20) -> dict:
    """Who owes us money: unpaid customer invoices per customer (owed, overdue, oldest days late), from the last sync."""
    ar = pd.read_parquet(DATA_DIR / "ar_open.parquet")
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    today = pd.Timestamp(meta.get("synced_at") or pd.Timestamp.today()).normalize()
    head = {"currency": label(meta.get("currency") or ""), "as_of": str(today.date())}
    if ar.empty:
        return head | {"note": "No unpaid customer invoices."}
    ar["DocDueDate"] = pd.to_datetime(ar.DocDueDate)
    ar["days_late"] = (today - ar.DocDueDate).dt.days.clip(lower=0)
    ar["overdue"] = ar.Balance.where(ar.days_late > 0, 0)
    if customer:
        c = customer.lower()
        ar = ar[ar.CardCode.str.lower().eq(c) | ar.CardName.fillna("").str.lower().str.contains(c, regex=False)]
        if ar.empty:
            return head | {"error": f"No unpaid invoices for a customer matching '{customer}'."}
    g = (ar.groupby(["CardCode", "CardName"])
         .agg(owed=("Balance", "sum"), overdue=("overdue", "sum"), invoices=("DocNum", "size"), oldest_days_late=("days_late", "max"))
         .reset_index().sort_values("owed", ascending=False).round(2))
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    out = head | {"total_owed": round(float(ar.Balance.sum()), 2), "total_overdue": round(float(ar.overdue.sum()), 2),
                  "customers_total": len(g), "customers": _records(g.head(limit))}
    if customer:  # one customer: its invoices too
        inv = ar.sort_values("DocDueDate")[["DocNum", "DocDate", "DocDueDate", "DocTotal", "Balance", "days_late"]]
        out["invoices"] = _records(inv.head(limit).round(2))
    return out


def finance_analysis(topic: str = "") -> dict:
    path = DATA_DIR / "finance.json"
    if not path.exists():
        return {"note": "Finance analysis has not run yet."}
    f = json.loads(path.read_text())
    head = {"generated_at": f["generated_at"], "currency": f["currency"]}
    if topic in FINANCE_TOPICS:
        return head | {topic: f.get(topic) or {"note": f"No {topic} results yet (not enough history)."}}
    # Overview: the headline of each topic; ask again with a topic for the details.
    c, lp, m = f["cash_forecast"], f["late_payers"], f["margins"]
    return head | {
        "cash_forecast": {k: v for k, v in c.items() if k != "weekly"},
        "late_payers": {"company_avg_days_paid_after_due": lp["company_avg_days_paid_after_due"],
                        "open_invoices_likely_late": {k: lp["open_invoices_likely_late"][k] for k in ("count", "value")}},
        "supplier_payments": {"company_avg_days_paid_after_due": f["supplier_payments"]["company_avg_days_paid_after_due"]},
        "margins": {k: m.get(k) for k in ("last_12_months", "margin_pct_last_90_days", "margin_pct_previous_275_days")},
        "churn": {k: f["churn"].get(k) for k in ("count", "monthly_revenue_at_risk")},
        "cross_sell_pairs": len(f["cross_sell"]["pairs"]),
        "topics_for_details": FINANCE_TOPICS,
    }


def daily_briefing() -> dict:
    path = DATA_DIR / "briefing.md"
    return {"briefing": path.read_text()} if path.exists() else {"note": "No briefing generated yet."}


EXPORT_MAX_ROWS = 50000
# Tools whose data can be exported (not charts, internet pages or the export itself).
# Ready-made master data lists, so the model never has to guess B1 column names for them.
LISTS = {
    "customers": "SELECT CardCode AS [Code], CardName AS [Name], Phone1 AS [Phone], E_Mail AS [Email], City, Country, "
                 "Currency, Balance AS [Balance still owed], OrdersBal AS [Open orders], CreditLine AS [Credit limit], "
                 "CASE WHEN frozenFor = 'Y' THEN 'Inactive' ELSE 'Active' END AS [Status], CreateDate AS [Created] "
                 "FROM OCRD WHERE CardType = 'C' ORDER BY CardCode",
    "suppliers": "SELECT CardCode AS [Code], CardName AS [Name], Phone1 AS [Phone], E_Mail AS [Email], City, Country, "
                 "Currency, Balance AS [Balance we owe], OrdersBal AS [Open orders], "
                 "CASE WHEN frozenFor = 'Y' THEN 'Inactive' ELSE 'Active' END AS [Status], CreateDate AS [Created] "
                 "FROM OCRD WHERE CardType = 'S' ORDER BY CardCode",
    "items": "SELECT I.ItemCode AS [Code], I.ItemName AS [Name], G.ItmsGrpNam AS [Group], I.InvntryUom AS [Unit], "
             "I.OnHand AS [On hand], I.IsCommited AS [Committed], I.OnOrder AS [On order], "
             "I.OnHand - I.IsCommited AS [Available], I.AvgPrice AS [Average cost], I.LastPurPrc AS [Last purchase price], "
             "I.CardCode AS [Preferred supplier], CASE WHEN I.frozenFor = 'Y' THEN 'Inactive' ELSE 'Active' END AS [Status] "
             "FROM OITM I LEFT JOIN OITB G ON G.ItmsGrpCod = I.ItmsGrpCod ORDER BY I.ItemCode",
}
EXPORTABLE = ("sales_analysis", "reorder_report", "critical_alerts", "stock_summary", "search_items", "item_details",
              "sales_history", "transaction_anomalies", "finance_analysis", "executive_summary", "daily_briefing",
              "market_watch", "fx_rates", "receivables", "customer_summary", "supplier_summary", "document")


def _result_sheets(result: dict) -> dict[str, pd.DataFrame]:
    """Any tool result as sheets: its figures on a Summary sheet, each list of rows on a sheet of its own."""
    summary, sheets = [], {}

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if not str(k).startswith("_"):
                    walk(v, f"{path} {k}".strip())
        elif isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            sheets[{"rows": "Lines", "items": "Items"}.get(path, path.replace("_", " "))] = pd.json_normalize(value)
        elif isinstance(value, list) and not value:
            summary.append({"Figure": path.replace("_", " "), "Value": "(none)"})
        elif isinstance(value, list):
            summary.append({"Figure": path.replace("_", " "), "Value": ", ".join(map(str, value))})
        else:
            summary.append({"Figure": path.replace("_", " "), "Value": value})

    walk(result, "")
    return ({"Summary": pd.DataFrame(summary)} if summary else {}) | sheets


def export_excel(title: str, tool: str = "", arguments: dict | None = None, sql: str = "") -> dict:
    """Excel download of a report (re-run with the same arguments) or of a read-only SELECT (up to 50,000 rows)."""
    arguments = dict(arguments or {})
    if tool in LISTS:
        sql = LISTS[tool]
    if sql:
        stmt = sql.strip().rstrip(";").strip()
        if problem := _sql_problem(stmt):
            return {"error": problem}
        try:
            conn = connect()
            try:
                cur = conn.cursor()
                cur.execute(stmt)
                cols = [c[0] for c in cur.description]
                rows = cur.fetchmany(EXPORT_MAX_ROWS + 1)
            finally:
                conn.close()
        except Exception as e:
            return {"error": str(e).split("DDBC Error: ")[-1][:500],
                    "note": "This query failed. Do not run it again unchanged: fix it once, or tell the user what you could not get."}
        df = pd.DataFrame.from_records(rows[:EXPORT_MAX_ROWS], columns=cols)
        df = df.map(lambda v: float(v) if type(v).__name__ == "Decimal" else v)
        link = excel_link(title, {title[:31] or "Data": df}, title=title,
                          subtitle=f"{len(df):,} rows" + (f" (first {EXPORT_MAX_ROWS:,} only)" if len(rows) > EXPORT_MAX_ROWS else ""))
        return {"_display": link + "\n", "rows": len(df), "columns": cols, "truncated": len(rows) > EXPORT_MAX_ROWS,
                "instruction": "The download link is already shown to the user. Say in one line what the file contains."}
    if tool not in EXPORTABLE:
        return {"error": f"Use tool = one of: {', '.join(list(LISTS) + list(EXPORTABLE))}, or give sql."}
    fn = TOOLS[tool][0]
    if "limit" in TOOLS[tool][2]:
        arguments["limit"] = MAX_ROWS  # the file gets every row the report allows, not the 20 used in chat
    result = fn(**arguments)
    if isinstance(result, dict) and result.get("error"):
        return result
    if tool in ("sales_analysis", "supplier_analytics"):  # they make their own, fuller workbook
        m = re.search(r"📥 .*", result.get("_display", ""))
        if not m:
            return {"error": "The sales Excel file could not be made this time.", "note": result.get("note")}
        return {"_display": m.group(0) + "\n", "instruction": "The download link is already shown to the user. "
                f"Say in one line that it holds the full {tool.replace('_', ' ')}."}
    sheets = _result_sheets(result if isinstance(result, dict) else {"rows": result})
    if not sheets:
        return {"error": "That report has no data to export."}
    about = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in arguments.items() if k != "limit" and v not in ("", None))
    return {"_display": excel_link(title, sheets, title=title, subtitle=tool.replace("_", " ").capitalize() + (f" ({about})" if about else "")) + "\n",
            "sheets": {k: len(v) for k, v in sheets.items()},
            "instruction": "The download link is already shown to the user. Say in one line what the file contains."}


TOOLS = {
    # Charts: pick the one that answers the question. Each draws from real B1 data; never draw an unrelated one.
    "sales_trend_chart": (sales_trend_chart, "CHART over time: net sales, gross PROFIT or gross MARGIN % per month, for the company or one item. Use for 'graph our sales/revenue/profit/margin', 'how are sales trending', 'monthly sales'.", {
        "measure": {"type": "string", "description": "sales (default), profit or margin"},
        "months": {"type": "integer", "description": "Number of months, default 12 (3-36)"},
        "item_code": {"type": "string", "description": "Optional exact item code"},
    }),
    "top_chart": (top_chart, "CHART ranking: the biggest customers, items, item groups or salespeople by net sales or gross profit, with each one's share. Use for 'top/best customers', 'best-selling items', 'who buys most', 'sales by salesperson', 'profit by customer/item'.", {
        "by": {"type": "string", "description": "customer (default), item, item_group or salesperson"},
        "measure": {"type": "string", "description": "sales (default) or profit"},
        "months": {"type": "integer", "description": "Period in months, default 12"},
        "top": {"type": "integer", "description": "How many to show, default 10"},
    }),
    "stock_levels_chart": (stock_levels_chart, "CHART of stock QUANTITY per item: units on hand, how many are free (not promised to customers) and on order. Use for 'show stock / in-stock quantity by item', 'stock of servers', 'what do we have in stock'. search filters by item name, code or group words.", {
        "search": {"type": "string", "description": "Optional words to match items, e.g. server or printer"},
        "warehouse": {"type": "string", "description": "Optional warehouse code"},
        "top": {"type": "integer", "description": "How many items, default 15"},
    }),
    "inventory_status_chart": (inventory_status_chart, "CHART of stock VALUE (money) by status: line-stop risk, stockout risk, reorder, overstock, dead stock, OK. Use for 'where is our stock money', 'stock health', 'overstock/dead stock'. Not for quantities per item.", {}),
    "stock_projection_chart": (stock_projection_chart, "CHART for ONE item: projected stock week by week, deliveries already coming, safety stock and when a new order would arrive. Use for 'when will item X run out', 'show X's stock outlook'. For several items use stock_levels_chart.", {
        "item_code": {"type": "string", "description": "Exact B1 item code"},
        "warehouse": {"type": "string", "description": "Optional warehouse code"},
    }),
    "receivables_chart": (receivables_chart, "CHART of money customers still owe: by how late (age buckets) or per customer with the overdue part. Use for 'who owes us', 'overdue invoices', 'receivables', 'debtors'.", {
        "by": {"type": "string", "description": "age (default) or customer"},
        "top": {"type": "integer", "description": "For by=customer: how many, default 10"},
    }),
    "cash_forecast_chart": (cash_forecast_chart, "CHART of the expected cash balance for the next 13 weeks with the lowest point. Use for 'cash flow / cash forecast / will we have enough cash' graphs.", {}),
    "custom_chart": (custom_chart, "CHART last resort, only if no chart above fits: one series of figures copied exactly from another tool's result in this conversation. Title must say exactly what the numbers are.", {
        "title": {"type": "string", "description": "What the chart shows, e.g. 'Overdue amount by customer'"},
        "chart_type": {"type": "string", "description": "bar (categories, default), line (over time) or pie (2-5 shares of one whole)"},
        "labels": {"type": "array", "items": {"type": "string"}, "description": "Category labels"},
        "values": {"type": "array", "items": {"type": "number"}, "description": "Values, same order as labels"},
        "unit": {"type": "string", "description": "Unit of the values: the currency (e.g. $ or RM), units, %, days"},
        "subtitle": {"type": "string", "description": "Optional: period and source of the figures"},
    }),
    "executive_summary": (executive_summary, "CEO view of the whole business in money and risk: operational risk and open customer orders affected, stock value / excess / dead stock, sales trend and top customers, order book, unpaid and overdue customer invoices, supplier on-time delivery. Use for any management or 'how is the business' question.", {}),
    "daily_briefing": (daily_briefing, "Today's morning briefing for management (risks, money, sales, market), as generated at 7 AM.", {}),
    "market_watch": (market_watch, "Latest automatic internet market watch: news per product category the company depends on, exchange rates, and the assessment of what it means for the company.", {}),
    "transaction_anomalies": (transaction_anomalies, "Nightly red-flag checks of recent transactions, high severity first: PRICE AP, PRICE AR, BELOW COST, DUPLICATE AP, 3-WAY QTY (billed more than received), 3-WAY PRICE (above PO price), NO PO, BANK CHANGE (supplier bank details changed), CREDIT SPIKE, JOURNAL (suspicious manual journal). Each row has the reason, paid status, money lost (impact) or amount at stake. Use for money leakage, overpaying, pricing errors, controls or fraud questions.", {
        "kind": {"type": "string", "description": "Optional type filter, e.g. DUPLICATE AP or BANK CHANGE"},
        "limit": {"type": "integer", "description": "Max rows, default 20"},
    }),
    "receivables": (receivables, "Who owes us money: unpaid customer invoices per customer with amount owed, amount overdue, number of invoices and oldest days late (customer given = also its invoices). Use for 'who owes us', 'receivables', 'debtors', 'unpaid/overdue invoices', 'how much does X owe'.", {
        "customer": {"type": "string", "description": "Optional customer code or part of the name"},
        "limit": {"type": "integer", "description": "Max customers, default 20"},
    }),
    "customer_summary": (customer_summary, "One-page summary of ONE customer: contact, payment terms, credit limit, what they owe and how overdue, open sales orders, sales and gross profit (last 12 months vs the year before), top items, how late they pay, and warning flags. Use for 'summarize/tell me about/how is customer X', 'customer 360', 'account review'.", {
        "customer": {"type": "string", "description": "Customer code or (part of) the name"},
        "limit": {"type": "integer", "description": "Rows per list (unpaid invoices, orders, items), default 5"},
    }),
    "supplier_summary": (supplier_summary, "One-page summary of ONE supplier: contact, payment terms, what we owe and when it is due, open purchase orders, purchases (last 12 months vs the year before), top items with price change, on-time delivery and lead time, how we pay them, and red flags. Use for 'summarize/tell me about/how is supplier X', 'vendor review'.", {
        "supplier": {"type": "string", "description": "Supplier code or (part of) the name"},
        "limit": {"type": "integer", "description": "Rows per list (unpaid invoices, orders, items), default 5"},
    }),
    "document": (document, "Look up ONE B1 document by type and number: header, every line, totals, status, balance due, what it was copied from/to and where to open it in B1. Use for 'show invoice 1049', 'what is on sales order 52', 'details of PO 310', 'payment 12'.", {
        "kind": {"type": "string", "description": "sales_quotation, sales_order, delivery, return, ar_down_payment, ar_invoice, ar_credit_memo, purchase_quotation, purchase_order, goods_receipt_po, goods_return, ap_invoice, ap_credit_memo, incoming_payment or outgoing_payment (also: invoice, quote, SO, DO, PO, GRPO, credit note, bill)"},
        "number": {"type": "integer", "description": "The document number (DocNum)"},
    }),
    "b1_howto": (b1_howto, "How to do something IN SAP Business One: the menu path, short steps and tips for a B1 task (create a credit memo, cancel an invoice, receive goods, pay a supplier, add a customer, stock transfer, journal entry, posting periods, reports, users, approvals...). If the user names a document number, it also checks that document (status, lines still open to copy). Use for 'how do I...', 'where in B1...', 'cara/macam mana nak... dalam SAP'. Answer with the path and steps exactly as returned; never invent a menu path.", {
        "question": {"type": "string", "description": "The user's question in their own words"},
        "document_kind": {"type": "string", "description": "Optional: type of the document they named (invoice, sales order, PO...)"},
        "number": {"type": "integer", "description": "Optional: number of the document they named"},
    }),
    "compare_periods": (compare_periods, "DASHBOARD comparing net sales and gross profit of a month / quarter / year with the period before or the same period last year, and which customers, items, item groups or salespeople caused the change. Use for 'how did we do this month vs last month', 'compare with last year', 'why are sales down', 'growth', 'banding bulan lepas'.", {
        "period": {"type": "string", "description": "month, quarter or year (default month)"},
        "which": {"type": "string", "description": "last (last complete one, default), this (to date), or e.g. 2026-09, 2026-Q3, 2025"},
        "against": {"type": "string", "description": "previous (default) or last_year"},
        "by": {"type": "string", "description": "What to explain the change by: customer (default), item, item_group or salesperson"},
    }),
    "quotation_pipeline": (quotation_pipeline, "DASHBOARD of quotations: open value, expired, won (turned into orders), win rate, and which open quotations to follow up first. Use for 'quotation pipeline', 'win rate', 'open quotes', 'which quotations to follow up', 'sebut harga terbuka'.", {
        "months": {"type": "integer", "description": "Quotations of the last N months (default 6)"},
    }),
    "customer_insights": (customer_insights, "DASHBOARD of customers: growing, steady, declining, at risk (gone quiet), new and lost, how often they buy, when they last bought, how late they pay, and who to call. Use for 'which customers are buying less', 'customers at risk', 'customer segments', 'who stopped buying'.", {
        "months": {"type": "integer", "description": "Compare the last N months with the N before (default 12)"},
    }),
    "product_profitability": (product_profitability, "DASHBOARD of product profitability: net sales, gross profit and margin per item or item group, ABC ranking, and items whose price or margin is slipping. Use for 'most profitable products', 'margin by item', 'ABC analysis', 'which products to push or drop'.", {
        "months": {"type": "integer", "description": "Last N months (default 12)"},
        "by": {"type": "string", "description": "item (default) or item_group"},
    }),
    "salesperson_performance": (salesperson_performance, "DASHBOARD per salesperson: net sales, change vs the period before, gross profit, margin, customers, quotation win rate and open quotations. Use for 'salesperson performance', 'sales rep ranking', 'prestasi jurujual'.", {
        "months": {"type": "integer", "description": "Last N months compared with the N before (default 3)"},
    }),
    "delivery_performance": (delivery_performance, "DASHBOARD of delivery to customers: % of sales order lines delivered on time, days late, days from order to delivery, and order lines already late and not delivered (back orders). Use for 'are we delivering on time', 'late orders', 'back orders', 'delivery performance'.", {
        "months": {"type": "integer", "description": "Deliveries of the last N months (default 6)"},
    }),
    "inventory_health": (inventory_health, "DASHBOARD of inventory health: stock value, turns a year, days of stock, overstock and items not moving, and where money is tied up. Use for 'inventory health', 'stock turnover', 'dead stock value', 'ageing', 'how much money is in slow stock'.", {}),
    "profit_and_loss": (profit_and_loss, "DASHBOARD of PROFIT AND LOSS from the accounts: revenue, cost of sales, gross profit, expenses, operating and net profit per month, biggest costs, and budget vs actual where a budget is set in B1. Use for 'P&L', 'profit and loss', 'income statement', 'are we profitable', 'expenses by month', 'untung rugi'.", {
        "months": {"type": "integer", "description": "Last N months (default 12)"},
    }),
    "sales_forecast": (sales_forecast, "FORECAST of net sales for the next months (company, one customer or one item) from invoice history, with a likely range, plus items whose expected use is more than stock + on order (likely to run short). Use for 'sales forecast', 'how much will we sell next quarter', 'demand forecast for A00001', 'ramalan jualan'. Always say it is an estimate.", {
        "months_ahead": {"type": "integer", "description": "How many months ahead (default 3, max 12)"},
        "customer": {"type": "string", "description": "Optional: one customer (code or name)"},
        "item": {"type": "string", "description": "Optional: one item (code or name)"},
    }),
    "management_pack": (management_pack, "MANAGEMENT PACK: every dashboard the user may see (comparisons, P&L, quotations, customers, products, salespeople, delivery, inventory, forecast) in one PDF ready to print or send, plus an Excel summary. Use for 'management pack', 'board pack', 'monthly report for my boss', 'laporan pengurusan'.", {}),
    "prepare_b1_draft": (prepare_b1_draft, "STEP 1 of creating a document IN B1 (as a draft): sales quotation, sales order, delivery, A/R invoice, A/R credit memo, return, return request, A/R down payment invoice, A/R reserve invoice, purchase request (someone asks for items to be bought; no supplier needed), purchase quotation, purchase order, goods receipt PO, A/P invoice, A/P credit memo, A/P down payment invoice, goods return, goods return request, goods receipt (stock in without a PO) and goods issue (stock out, e.g. write-off or own use). Checks it against B1 (partner, items, prices, warehouse, stock) and shows a preview; the user then replies yes. Either give partner + items, or copy_from an earlier document like B1's Copy From (open lines and their prices): 'deliver sales order 1186' = kind delivery, copy_from 'sales order 1186'; 'invoice delivery 512', 'credit note for invoice 77', 'receive PO 340' (goods_receipt_po), 'supplier invoice for GRPO 88'. Use when the user asks to create / enter / raise / make / deliver / invoice / receive such a document in SAP (also Malay: 'buat PO kepada Acme', 'hantar pesanan jualan 1186'). Nothing is created in B1 by this step.", {
        "kind": {"type": "string", "description": "sales_quotation, sales_order, delivery, ar_invoice, ar_credit_memo, return, return_request, ar_down_payment, ar_reserve_invoice, purchase_request, purchase_quotation, purchase_order, goods_receipt_po, ap_invoice, ap_credit_memo, ap_down_payment, goods_return, goods_return_request, goods_receipt or goods_issue"},
        "warehouse": {"type": "string", "description": "Optional warehouse for all lines (e.g. goods issue from warehouse 01); default = each item's own"},
        "partner": {"type": "string", "description": "Customer or supplier: code or (part of) the name. Not needed with copy_from"},
        "items": {"type": "string", "description": "Items and quantities, optional @price: 'A00001:5, PC Set 1:2@2500'. With copy_from: only to take part of the open lines; empty = all open lines"},
        "copy_from": {"type": "string", "description": "Optional earlier document to copy from: its type and number, e.g. 'sales order 1186', 'delivery 512', 'purchase order 340'"},
        "due_date": {"type": "string", "description": "Optional date YYYY-MM-DD: delivery date (orders), valid-until (quotations), document date otherwise"},
        "reference": {"type": "string", "description": "Optional customer/supplier reference (e.g. their PO or invoice number)"},
        "remarks": {"type": "string", "description": "Optional remarks"},
    }),
    "prepare_b1_stock_document": (prepare_b1_stock_document, "STEP 1 for STOCK documents in B1: inventory_transfer (move stock between warehouses; draft), transfer_request (ask the warehouse to move stock; moves nothing), inventory_counting (record a stock count; draft) or inventory_posting (set stock to counted quantities; draft). NOT for goods receipt, goods issue or purchase request: those are prepare_b1_draft. Shows a preview; the user then replies yes. Use for 'transfer 5 A00001 from 01 to 02', 'stock count in warehouse 01: A00001 12', 'pindahkan stok'.", {
        "kind": {"type": "string", "description": "inventory_transfer, transfer_request, inventory_counting or inventory_posting"},
        "items": {"type": "string", "description": "Items and quantities (counted quantity for counting/posting): 'A00001:5, C00010:12'"},
        "warehouse": {"type": "string", "description": "From warehouse (transfers) or the counted warehouse: code or name"},
        "to_warehouse": {"type": "string", "description": "To warehouse (transfers only)"},
        "doc_date": {"type": "string", "description": "Optional date YYYY-MM-DD (default today)"},
        "remarks": {"type": "string", "description": "Optional remarks"},
    }),
    "prepare_b1_payment": (prepare_b1_payment, "STEP 1 for a PAYMENT in B1 (as a payment draft): incoming_payment (a customer pays us) or outgoing_payment (we pay a supplier), applied to their open invoices, oldest due first. Shows a preview; the user then replies yes. Use for 'record payment from Maxi-Teq for invoice 1047', 'pay Acme their open invoices', 'customer paid 5000 by transfer to MBB', 'bayar pembekal'.", {
        "kind": {"type": "string", "description": "incoming_payment or outgoing_payment"},
        "partner": {"type": "string", "description": "Customer (incoming) or supplier (outgoing): code or (part of) the name"},
        "invoices": {"type": "string", "description": "Optional invoice numbers, e.g. '1047, 1052'. Empty = all their open invoices"},
        "amount": {"type": "number", "description": "Optional amount paid; empty = everything still open on those invoices"},
        "bank": {"type": "string", "description": "Optional bank account for the transfer: house bank code (e.g. MBB), account number or G/L account"},
        "doc_date": {"type": "string", "description": "Optional payment date YYYY-MM-DD (default today)"},
        "reference": {"type": "string", "description": "Optional transfer reference"},
        "remarks": {"type": "string", "description": "Optional remarks"},
    }),
    "prepare_b1_production_order": (prepare_b1_production_order, "STEP 1 for a PRODUCTION ORDER in B1, created with status Planned (nothing released or issued): an item with a bill of materials and the quantity to make; shows the components needed and stock. The user then replies yes. Use for 'make 10 MRP_BOM', 'production order for 5 LM4029', 'buat pesanan pengeluaran'.", {
        "item": {"type": "string", "description": "Item to produce: code or name"},
        "quantity": {"type": "number", "description": "Quantity to make"},
        "due_date": {"type": "string", "description": "Optional due date YYYY-MM-DD (default in 7 days)"},
        "warehouse": {"type": "string", "description": "Optional warehouse for the finished item (default from the bill of materials)"},
        "remarks": {"type": "string", "description": "Optional remarks"},
    }),
    "prepare_b1_journal_entry": (prepare_b1_journal_entry, "STEP 1 for a JOURNAL ENTRY in B1, created as a journal voucher (not posted until someone posts it in B1). Lines as '<account> debit|credit <amount>' separated by ';' - account = G/L account code or name, or a customer/supplier code. Debits must equal credits. The user then replies yes. Use for 'journal entry: rent expense debit 2000, bank credit 2000', 'accrue 500 electricity'.", {
        "lines": {"type": "string", "description": "e.g. '_SYS00000000100 debit 2000; Bank credit 2000' or '6100-00 debit 500; C20000 credit 500'"},
        "memo": {"type": "string", "description": "Optional short description"},
        "doc_date": {"type": "string", "description": "Optional posting date YYYY-MM-DD (default today)"},
        "reference": {"type": "string", "description": "Optional reference"},
    }),
    "create_b1_draft": (create_b1_draft, "STEP 2 for every prepare_b1_* tool: create the prepared document in B1. Only offered right after the user approved the preview (replied yes). Call it then, once.", {
        "code": {"type": "string", "description": "The approval ref shown in the preview (filled in by the chat service)"},
    }),
    "collections_plan": (collections_plan, "COLLECTIONS HELPER: today's list of customers to chase, most urgent first (money overdue x how late), with oldest days late, open orders to hold, the next step and contact, plus ready payment reminder drafts for the first few. Use for 'who should I chase today', 'collections plan/list', 'senarai kutipan hutang'. For one customer use customer_summary or payment_reminder instead.", {
        "top": {"type": "integer", "description": "How many customers, default 10"},
        "drafts": {"type": "integer", "description": "Reminder drafts for the first N, default 5 (0 = none)"},
    }),
    "purchasing_plan": (purchasing_plan, "PURCHASING HELPER: what to order today as one proposed order per supplier (urgent first), with items and quantities from the reorder report, the supplier (preferred, else suggested from purchase history), estimated value, earliest run-out, and an order email draft per supplier. Use for 'what should I order today', 'purchasing plan', 'prepare orders for suppliers', 'pelan pembelian'.", {}),
    "company_documents": (company_documents, "Search the COMPANY'S OWN DOCUMENTS (policies, procedures, price lists, terms, contracts, internal rules) and return the passages that answer, each with its file and page. Use for questions about our rules or agreed terms: 'what is our credit policy', 'warranty period', 'who approves', 'distributor price in our price list', 'polisi syarikat'. Answer only from the passages and name the source. Empty query = list the documents.", {
        "query": {"type": "string", "description": "Key words of the question; include English and Malay words when the documents may be in either language"},
    }),
    "supplier_analytics": (supplier_analytics, "Supplier ANALYTICS / DASHBOARD across all suppliers (or one supplier / item): spend per supplier and share, what we owe and what is overdue, how many days after due we pay, on-time delivery, payments per month, items purchased with the BEST PRICE per item (which supplier is cheapest) and the possible saving. Shows charts and an Excel dashboard automatically. Use for 'supplier dashboard/analytics/report', 'which supplier is cheapest', 'what we bought from suppliers', 'payments to suppliers', 'compare supplier prices'. Never write SQL for this.", {
        "months": {"type": "integer", "description": "Period in months, default 12"},
        "supplier": {"type": "string", "description": "Optional: one supplier code or (part of) the name"},
        "item": {"type": "string", "description": "Optional: one item code or (part of) the name"},
    }),
    "payment_reminder": (payment_reminder, "DRAFT a payment reminder email (polite, firm or final) or a statement of account for ONE customer, listing their real unpaid invoices and exact amounts. Never sent: shown to copy, plus an email-draft file. Use for 'write/draft a reminder to X', 'chase X for payment', 'send X a statement', 'surat peringatan bayaran'. No tone = chosen from how late the oldest invoice is.", {
        "customer": {"type": "string", "description": "Customer code or (part of) the name"},
        "tone": {"type": "string", "description": "Optional: polite, firm, final or statement"},
        "language": {"type": "string", "description": "en (default) or ms (Malay)"},
    }),
    "supplier_order_email": (supplier_order_email, "DRAFT an order request email to ONE supplier: from the reorder suggestions for that supplier, or from items and quantities the user gives; with last purchase prices and an estimated total. Never sent. No supplier = list which suppliers have suggested orders. Use for 'draft an order to supplier X', 'email Nexa for 10 laptops', 'tulis emel pesanan kepada pembekal'.", {
        "supplier": {"type": "string", "description": "Supplier code or (part of) the name"},
        "items": {"type": "string", "description": "Optional: items and quantities, e.g. 'LT-LAP-14:10, LT-MON-27:5' (item code or name)"},
        "language": {"type": "string", "description": "en (default) or ms (Malay)"},
    }),
    "cover_note": (cover_note, "DRAFT a short covering email for ONE document to send with it: quotation, order confirmation (sales order), delivery, invoice, credit note or purchase order, with its exact total and dates. Never sent. Use for 'write an email to send quotation 1186', 'cover note for invoice 2'.", {
        "kind": {"type": "string", "description": "quotation, sales_order, delivery, ar_invoice, ar_credit_memo or purchase_order"},
        "number": {"type": "integer", "description": "The document number"},
        "language": {"type": "string", "description": "en (default) or ms (Malay)"},
    }),
    "finance_analysis": (finance_analysis, "Money outlook from B1: 13-week cash forecast, customers likely to pay late, how early/late we pay suppliers, gross margins by customer/item/salesperson with trend, customers who stopped ordering (churn) and items often bought together (cross-sell). No topic = overview.", {
        "topic": {"type": "string", "description": "Optional: cash_forecast, late_payers, supplier_payments, margins, churn or cross_sell"},
    }),
    "web_search": (web_search, "Search the internet (live). Use for current market prices, supply disruptions, supplier or industry news, regulations, or anything not in B1. news=true for news; recent_days limits age. Use at most 3 searches per question. For advice (e.g. how to keep sales up), first look at the company's own figures (sales_analysis and similar) and market_watch, then search only to fill gaps.", {
        "query": {"type": "string", "description": "Search words"},
        "news": {"type": "boolean", "description": "Search news only"},
        "recent_days": {"type": "integer", "description": "Only results from the last N days (1, 7, 31, 365)"},
    }),
    "read_web_page": (read_web_page, "Read the text of a public web page found with web_search.", {
        "url": {"type": "string", "description": "Page URL"},
    }),
    "fx_rates": (fx_rates, "Live exchange rates (European Central Bank reference rates).", {
        "base": {"type": "string", "description": "Base currency code, e.g. USD"},
        "symbols": {"type": "string", "description": "Comma-separated currency codes, e.g. EUR,MYR"},
    }),
    "stock_summary": (stock_summary, "Overview of the latest reorder report: data date, line counts and suggested order value per status, and the top 5 orders by value. Start here for general questions.", {}),
    "critical_alerts": (critical_alerts, "Urgent warnings: LINE STOP RISK (critical items, e.g. production components, that will run out) and STOCKOUT RISK, most urgent first, with run-out date, blocked production orders and the action to take.", {
        "limit": {"type": "integer", "description": "Max items, default 20"},
    }),
    "reorder_report": (reorder_report, "Lines of the reorder report, highest suggested order value first. With no status it returns every line that needs ordering (STOCKOUT RISK and REORDER) - use that for 'what should I order' questions. Optional status filter: LINE STOP RISK, STOCKOUT RISK, REORDER, OVERSTOCK, DEAD STOCK, OK.", {
        "status": {"type": "string", "description": "Optional status filter; leave empty for everything that needs ordering"},
        "warehouse": {"type": "string", "description": "Optional warehouse code, e.g. 01"},
        "limit": {"type": "integer", "description": "Max rows, default 20"},
    }),
    "search_items": (search_items, "Find item codes by part of the item code or name.", {
        "text": {"type": "string", "description": "Text to search for"},
    }),
    "item_details": (item_details, "Everything about one item: master data, LIVE stock per warehouse from B1, reorder report lines with reorder point and safety stock, and the 12-week forecast.", {
        "item_code": {"type": "string", "description": "Exact B1 item code"},
    }),
    "sales_history": (sales_history, "Weekly invoiced sales (net of credit notes) for one item, per warehouse.", {
        "item_code": {"type": "string", "description": "Exact B1 item code"},
        "weeks": {"type": "integer", "description": "How many recent weeks, default 26"},
    }),
    "sales_analysis": (sales_analysis, "Finished sales analysis report by invoice, LIVE from B1: net sales after credit memos, tax, gross profit and margin, sales by month, by customer and by item, what is still to be paid and overdue, and the invoice list. The report is shown to the user directly. Use it for ANY sales report, sales analysis, revenue, invoices-issued or sales-by-customer/item/month question; one call answers it.", {
        "date_from": {"type": "string", "description": "Optional start: YYYY-MM-DD, YYYY-MM or YYYY. Empty = 1 January of the end year"},
        "date_to": {"type": "string", "description": "Optional end: YYYY-MM-DD, YYYY-MM or YYYY (YYYY-MM = whole month). Empty = today"},
        "customer": {"type": "string", "description": "Optional customer code or part of the name"},
        "charts": {"type": "boolean", "description": "true = also draw charts (a sales dashboard): by month, top customers, top items"},
    }),
    "export_excel": (export_excel, "Make an Excel (.xlsx) file the user can download. Use whenever the user asks for Excel, a spreadsheet, a download, a file, or to export data, including 'that' / 'the table above': re-create it with the tool (and arguments) or SELECT that produced it. The download link is shown to the user automatically; never write a link yourself.", {
        "title": {"type": "string", "description": "Short file name, e.g. Reorder list warehouse 01"},
        "tool": {"type": "string", "description": "What to export. Ready-made lists: customers, suppliers, items (all of them, "
                 "with balances / stock). Or a report: " + ", ".join(EXPORTABLE)},
        "arguments": {"type": "object", "description": "That report's arguments, e.g. {\"status\": \"REORDER\"}; empty for none"},
        "sql": {"type": "string", "description": "Only when no ready-made list or report fits: one read-only SELECT, up to 50,000 rows (filter documents with CANCELED = 'N')"},
    }),
    "run_sql": (run_sql, "Run one read-only T-SQL SELECT against the live B1 database (max 200 rows). Only use when the other tools cannot answer (never for sales reports: use sales_analysis). Filter documents with CANCELED = 'N'.", {
        "sql": {"type": "string", "description": "A single SELECT statement"},
    }),
}
REQUIRED = {"prepare_b1_draft": ["kind"], "prepare_b1_stock_document": ["kind", "items", "warehouse"], "prepare_b1_payment": ["kind", "partner"], "prepare_b1_production_order": ["item", "quantity"], "prepare_b1_journal_entry": ["lines"], "create_b1_draft": ["code"], "payment_reminder": ["customer"], "cover_note": ["kind", "number"], "b1_howto": ["question"], "customer_summary": ["customer"], "supplier_summary": ["supplier"], "document": ["kind", "number"], "stock_projection_chart": ["item_code"], "custom_chart": ["title", "chart_type", "labels", "values", "unit"], "web_search": ["query"], "read_web_page": ["url"], "search_items": ["text"], "item_details": ["item_code"], "sales_history": ["item_code"], "run_sql": ["sql"], "export_excel": ["title"]}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": desc,
                                      "parameters": {"type": "object", "properties": props, "required": REQUIRED.get(name, [])}}}
    for name, (_, desc, props) in TOOLS.items()
]


def _coerce(name: str, args: dict) -> dict:
    """The model sometimes sends numbers or true/false as text ("7", "true"): convert them to the schema's type."""
    props = TOOLS[name][2]
    out = dict(args)
    for k, v in args.items():
        kind = (props.get(k) or {}).get("type")
        if not isinstance(v, str):
            continue
        try:
            if kind == "integer":
                out[k] = int(float(v)) if v.strip() else 0
            elif kind == "number":
                out[k] = float(v) if v.strip() else 0.0
            elif kind == "boolean":
                out[k] = v.strip().lower() in ("true", "1", "yes")
        except ValueError:
            pass  # leave it; the tool reports the problem
    return out


def call_tool(name: str, args: dict) -> str:
    """Run a tool and return its result as JSON text for the model."""
    if name not in TOOLS:
        return json.dumps({"error": f"Unknown tool {name}"})
    from . import access
    if reason := access.allowed(name, args):  # the person's role, checked here whatever the AI asks for
        return json.dumps({"error": f"Not allowed: {reason}.", "note": "Tell the user plainly that their role does not include "
                           "this and that an administrator can change their access. Do not try other tools to get it."})
    try:
        result = TOOLS[name][0](**_coerce(name, args or {}))
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    text = json.dumps(result, default=str)
    if isinstance(result, dict) and "_display" in result:
        return text  # chart image goes to the user, not the model; the agent strips it before the model sees it
    return text if len(text) < 15000 else text[:15000] + '..."(truncated)"'


if __name__ == "__main__":
    print(datetime.now(), call_tool("stock_summary", {})[:600])
