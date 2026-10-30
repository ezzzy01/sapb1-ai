"""Daily CEO briefing: risks, money, order book, cash and market signals in one short report.

Facts are assembled in code (so numbers are always exact); the local model only writes the
three-line headline on top. Saved as briefing.md and pushed with the same channels as alerts.
"""

import json
import os
from datetime import datetime

import httpx

from .executive import executive_summary
from .llm import ask
from .sync import DATA_DIR

SYSTEM = """You write the top of a CEO's morning briefing. From the facts given, write exactly three short lines:
1) the single most important risk and its money impact, 2) the most important business trend, 3) the one decision
needed today. Plain language, no item codes unless essential, use the currency given, no invented numbers."""


def _m(cur: str, v) -> str:
    return f"{cur}{v:,.0f}" if isinstance(v, (int, float)) else "n/a"


def build() -> str:
    ex = executive_summary()
    cur = ex.get("currency") or ""
    risk, inv, sales, book, ar, sup = (ex["operational_risk"], ex["inventory"], ex.get("sales", {}),
                                       ex.get("order_book", {}), ex.get("receivables", {}), ex.get("suppliers", {}))
    mw_path = DATA_DIR / "market_watch.json"
    market = json.loads(mw_path.read_text()) if mw_path.exists() else {}

    facts = []
    facts.append("## 🛑 Operational risk")
    facts.append(f"- **{risk['line_stop_risk_items']}** item(s) could stop production, **{risk['stockout_risk_items']}** more at stockout risk; "
                 f"**{risk['production_orders_at_risk']}** production order(s) blocked.")
    facts.append(f"- {_m(cur, risk['open_sales_value_on_short_items'])} of open customer orders depend on these items"
                 + (": " + ", ".join(f"{c['customer']} ({_m(cur, c['open_order_value'])})" for c in risk["customers_most_affected"][:3]) if risk["customers_most_affected"] else "") + ".")
    for u in risk["most_urgent"][:3]:
        facts.append(f"- {'🛑' if u['status'] == 'LINE STOP RISK' else '⚠️'} **{u['ItemName']}** (whs {u['WhsCode']}): {u['action']}")

    facts.append("\n## 💰 Money")
    facts.append(f"- Stock on hand {_m(cur, inv['stock_value'])}; excess {_m(cur, inv['excess_stock_value'])}; dead stock {_m(cur, inv['dead_stock_value'])}.")
    facts.append(f"- Purchases suggested now: {_m(cur, inv['suggested_purchases_value'])}.")
    if ar:
        facts.append(f"- Unpaid customer invoices {_m(cur, ar['total_unpaid'])}, overdue {_m(cur, ar['overdue'])} "
                     f"({_m(cur, ar['overdue_over_90_days'])} over 90 days).")
    an_path = DATA_DIR / "anomalies.json"
    if an_path.exists() and (found := json.loads(an_path.read_text())["by_type"]):
        names = {"DUPLICATE AP": "possible duplicate supplier invoices", "BELOW COST": "sales below cost",
                 "PRICE AP": "unusual purchase prices", "PRICE AR": "unusual sales prices"}
        facts.append("- Transaction checks: " + "; ".join(f"{t['count']} {names.get(t['type'], t['type'])} ({_m(cur, t['money_lost'])})"
                                                           for t in found) + ".")

    if sales:
        def chg(p):
            return "n/a" if p is None else f"{p:+.0f}%"
        facts.append("\n## 📈 Sales")
        facts.append(f"- Last 30 days {_m(cur, sales['revenue_last_30_days'])} ({chg(sales['change_vs_previous_30_days_pct'])}); "
                     f"last 90 days {_m(cur, sales['revenue_last_90_days'])} ({chg(sales['change_vs_previous_90_days_pct'])}). Data up to {sales['data_up_to']}.")
    if book:
        facts.append(f"- Open orders {_m(cur, book['open_sales_orders_value'])}, of which {_m(cur, book['overdue_sales_orders_value'])} overdue "
                     f"({book['overdue_sales_order_lines']} lines).")
    if sup:
        worst = sup.get("least_reliable", [])[:2]
        facts.append(f"- Suppliers on time: {sup['on_time_delivery_pct']}%"
                     + ("; least reliable: " + ", ".join(f"{s['supplier']} ({s['on_time_pct']}%)" for s in worst) if worst else "") + ".")

    if market.get("assessment"):
        facts.append(f"\n## 🌍 Market (internet, {market['generated_at'][:10]})")
        facts.append(market["assessment"])
        if market.get("fx", {}).get("rates"):
            chg_fx = market["fx"].get("change_pct_since_last_run", {})
            facts.append("- FX vs " + market["fx"]["base"] + ": " + ", ".join(
                f"{k} {v}" + (f" ({chg_fx[k]:+.2f}%)" if k in chg_fx else "") for k, v in market["fx"]["rates"].items()))

    body = "\n".join(facts)
    try:
        headline = ask(SYSTEM, body, max_tokens=250)
    except Exception:
        headline = ""
    date = datetime.now().strftime("%A %d %B %Y")
    return (f"# Morning briefing — {date}\n*{ex['company_database']}, data synced {ex['data_synced_at'][:16].replace('T', ' ')}*\n\n"
            + (f"{headline}\n\n" if headline else "") + body + "\n")


def push(text: str) -> None:
    title = text.splitlines()[0].lstrip("# ").strip()
    if url := os.getenv("NTFY_URL"):
        httpx.post(url, content=text.encode(), timeout=20,
                   headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Markdown": "yes", "Tags": "newspaper"})
        print("Pushed to ntfy")
    if url := os.getenv("ALERT_WEBHOOK_URL"):
        httpx.post(url, json={"text": text}, timeout=20)
        print("Pushed to webhook")


def main() -> None:
    text = build()
    (DATA_DIR / "briefing.md").write_text(text)
    print(text)
    try:
        push(text)
    except httpx.HTTPError as e:
        print(f"Push failed: {e}")


if __name__ == "__main__":
    main()
