"""Tools the stock assistant can call. All read-only.

Report-based tools read the files written by sync.py / forecast.py; item_details and
run_sql also query B1 live through the read-only ai_reader login.
"""

import json
import re
from datetime import datetime

import pandas as pd

from .charts import custom_chart, inventory_status_chart, sales_trend_chart, stock_projection_chart
from .db import connect, query
from .executive import executive_summary
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


def run_sql(sql: str) -> dict:
    stmt = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", stmt, re.I) or ";" in stmt or _FORBIDDEN.search(stmt):
        return {"error": "Only a single read-only SELECT statement is allowed."}
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(stmt)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:
        return {"error": str(e).split("DDBC Error: ")[-1][:500]}
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
            "by_type": an["by_type"],
            "total_money_lost": round(sum(t["money_lost"] for t in an["by_type"]), 2),
            "total_amount_to_review": round(sum(t.get("amount_to_review", 0) for t in an["by_type"]), 2), "rows_total": len(items), "rows_by_payment_status": paid, "rows": items[:limit]}


FINANCE_TOPICS = ["cash_forecast", "late_payers", "supplier_payments", "margins", "churn", "cross_sell"]


def finance_analysis(topic: str = "") -> dict:
    path = DATA_DIR / "finance.json"
    if not path.exists():
        return {"note": "Finance analysis has not run yet."}
    f = json.loads(path.read_text())
    head = {"generated_at": f["generated_at"], "currency": f["currency"]}
    if topic in FINANCE_TOPICS:
        return head | {topic: f[topic]}
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


TOOLS = {
    "stock_projection_chart": (stock_projection_chart, "CHART: week-by-week projected stock for one item with incoming deliveries, safety stock, reorder point, lead time and run-out date. Use when asked to show/graph/visualise an item's stock or why it runs out.", {
        "item_code": {"type": "string", "description": "Exact B1 item code"},
        "warehouse": {"type": "string", "description": "Optional warehouse code"},
    }),
    "sales_trend_chart": (sales_trend_chart, "CHART: monthly sales revenue as bars, for the company or one item.", {
        "months": {"type": "integer", "description": "Number of months, default 12"},
        "item_code": {"type": "string", "description": "Optional item code"},
    }),
    "inventory_status_chart": (inventory_status_chart, "CHART: stock value by status (line-stop risk, stockout risk, overstock, dead stock, OK).", {}),
    "custom_chart": (custom_chart, "CHART: any other comparison. Only use numbers taken from another tool's result in this conversation.", {
        "title": {"type": "string", "description": "Chart title"},
        "chart_type": {"type": "string", "description": "bar, barh, line or pie"},
        "labels": {"type": "array", "items": {"type": "string"}, "description": "Category labels"},
        "values": {"type": "array", "items": {"type": "number"}, "description": "Values, same order as labels"},
        "unit": {"type": "string", "description": "Optional unit, e.g. $ or units"},
    }),
    "executive_summary": (executive_summary, "CEO view of the whole business in money and risk: operational risk and open customer orders affected, stock value / excess / dead stock, sales trend and top customers, order book, unpaid and overdue customer invoices, supplier on-time delivery. Use for any management or 'how is the business' question.", {}),
    "daily_briefing": (daily_briefing, "Today's morning briefing for management (risks, money, sales, market), as generated at 7 AM.", {}),
    "market_watch": (market_watch, "Latest automatic internet market watch: news per product category the company depends on, exchange rates, and the assessment of what it means for the company.", {}),
    "transaction_anomalies": (transaction_anomalies, "Nightly red-flag checks of recent transactions, high severity first: PRICE AP, PRICE AR, BELOW COST, DUPLICATE AP, 3-WAY QTY (billed more than received), 3-WAY PRICE (above PO price), NO PO, BANK CHANGE (supplier bank details changed), CREDIT SPIKE, JOURNAL (suspicious manual journal). Each row has the reason, paid status, money lost (impact) or amount at stake. Use for money leakage, overpaying, pricing errors, controls or fraud questions.", {
        "kind": {"type": "string", "description": "Optional type filter, e.g. DUPLICATE AP or BANK CHANGE"},
        "limit": {"type": "integer", "description": "Max rows, default 20"},
    }),
    "finance_analysis": (finance_analysis, "Money outlook from B1: 13-week cash forecast, customers likely to pay late, how early/late we pay suppliers, gross margins by customer/item/salesperson with trend, customers who stopped ordering (churn) and items often bought together (cross-sell). No topic = overview.", {
        "topic": {"type": "string", "description": "Optional: cash_forecast, late_payers, supplier_payments, margins, churn or cross_sell"},
    }),
    "web_search": (web_search, "Search the internet (live). Use for current market prices, supply disruptions, supplier or industry news, regulations, or anything not in B1. news=true for news; recent_days limits age.", {
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
    "run_sql": (run_sql, "Run one read-only T-SQL SELECT against the live B1 database (max 200 rows). Only use when the other tools cannot answer. Filter documents with CANCELED = 'N'.", {
        "sql": {"type": "string", "description": "A single SELECT statement"},
    }),
}
REQUIRED = {"stock_projection_chart": ["item_code"], "custom_chart": ["title", "chart_type", "labels", "values"], "web_search": ["query"], "read_web_page": ["url"], "search_items": ["text"], "item_details": ["item_code"], "sales_history": ["item_code"], "run_sql": ["sql"]}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": desc,
                                      "parameters": {"type": "object", "properties": props, "required": REQUIRED.get(name, [])}}}
    for name, (_, desc, props) in TOOLS.items()
]


def call_tool(name: str, args: dict) -> str:
    """Run a tool and return its result as JSON text for the model."""
    if name not in TOOLS:
        return json.dumps({"error": f"Unknown tool {name}"})
    try:
        result = TOOLS[name][0](**(args or {}))
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    text = json.dumps(result, default=str)
    if isinstance(result, dict) and "_display" in result:
        return text  # chart image goes to the user, not the model; the agent strips it before the model sees it
    return text if len(text) < 15000 else text[:15000] + '..."(truncated)"'


if __name__ == "__main__":
    print(datetime.now(), call_tool("stock_summary", {})[:600])
