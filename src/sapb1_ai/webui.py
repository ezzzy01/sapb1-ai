"""Publishes the current warnings and suggested prompts to the SAP B1 chat page (Open WebUI).

The banner is shown at the top of the page as soon as a user logs in, before they type anything.
Suggested prompts follow the current situation (e.g. today's most urgent item) plus the main uses:
management overview, morning briefing, market, cash. Open WebUI reads both from its config table on
every page load, so updating the rows is enough - no restart.
"""

import html
import json
import os
import subprocess
import time

from .sync import DATA_DIR

CONTAINER = os.getenv("WEBUI_CONTAINER", "sapb1-ai-webui")


def _esc(s) -> str:
    return html.escape(str(s or ""))


def banner(alerts: dict) -> list[dict]:
    """Login pop-up (styled in webui/fiori.css). Open WebUI turns newlines into <br> before rendering,
    so the content is written as one line of simple HTML (kept by its sanitizer)."""
    now = int(time.time())
    items = alerts.get("items", [])
    if not items:
        return [{"id": f"sapb1-ok-{now}", "type": "success", "title": "", "dismissible": True, "timestamp": now,
                 "content": '<div class="sapb1-h">All clear</div><p>No stock items are at line-stop or stockout risk in the latest data.</p>'}]
    c = alerts["counts"]
    rows = []
    for a in items[:5]:
        days = a.get("days_to_runout")
        when = "runs out <b>now</b>" if days is not None and days <= 0 else f"runs out <b>{_esc(a.get('runout_date'))}</b>"
        icon = "🛑" if a["status"] == "LINE STOP RISK" else "⚠️"
        blocks = f'<span class="sapb1-sub">Blocks {_esc(a["production_orders_at_risk"])}</span>' if a.get("production_orders_at_risk") else ""
        rows.append(f'<li>{icon} <b>{_esc(a["ItemName"])}</b> <span class="sapb1-whs">whs {_esc(a["WhsCode"])}</span> · {when}{blocks}</li>')
    if len(items) > 5:
        rows.append(f'<li class="sapb1-more">…and {len(items) - 5} more</li>')
    content = (
        '<div class="sapb1-h">Stock needs attention today</div>'
        f'<p><b>{c["LINE STOP RISK"]}</b> item(s) could <b>stop production</b> · <b>{c["STOCKOUT RISK"]}</b> more at stockout risk</p>'
        f'<ul>{"".join(rows)}</ul>'
        '<p class="sapb1-foot">Ask <b>"show all critical alerts"</b> in the chat for actions and order quantities.</p>'
    )
    return [{"id": f"sapb1-alert-{now}", "type": "error" if c["LINE STOP RISK"] else "warning",
             "title": "", "content": content, "dismissible": True, "timestamp": now}]


def suggestions(alerts: dict) -> list[dict]:
    out = []
    items = alerts.get("items", [])
    if items:
        top = items[0]
        out.append({"title": [f"Why is {top['ItemName']}", "at risk and what do we do?"],
                    "content": f"Why is {top['ItemName']} ({top['ItemCode']}) at {top['status'].lower()}, what does it block, and what should we do today?"})
        out.append({"title": ["Show all critical alerts", "and production at risk"],
                    "content": "Show all critical alerts: what could stop production or operations, run-out dates, blocked production orders and actions."})
    out += [
        {"title": ["How is the business doing?", "Decisions needed today"],
         "content": "I'm the CEO. How is the business doing and what do I need to decide today? Keep it short."},
        {"title": ["Today's morning briefing", "Risks, money, sales, market"],
         "content": "Show me today's morning briefing."},
        {"title": ["Market check", "Prices and supply news that affect us"],
         "content": "Check the internet: which market price or supply changes affect our critical items? Give sources."},
        {"title": ["What should we order", "this week, and why?"],
         "content": "What should we order this week? Show item, warehouse, quantity, value and why."},
        {"title": ["Cash check", "Who owes us money?"],
         "content": "Who owes us money? Show overdue customer invoices and the biggest amounts."},
        {"title": ["Supplier check", "Who delivers late?"],
         "content": "Which suppliers deliver late, and do any of them supply our critical items?"},
    ]
    return out[:8]


def publish() -> None:
    alerts = json.loads((DATA_DIR / "alerts.json").read_text())
    values = {"ui.banners": banner(alerts), "ui.prompt_suggestions": suggestions(alerts)}
    script = (
        "import sqlite3,json,sys,time\n"
        "vals=json.loads(sys.stdin.read()); c=sqlite3.connect('/app/backend/data/webui.db', timeout=30)\n"
        "for k,v in vals.items():\n"
        "    c.execute('insert into config(key,value,updated_at) values(?,?,?) on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at',(k,json.dumps(v),int(time.time())))\n"
        "c.commit()\n"
    )
    subprocess.run(["docker", "exec", "-i", CONTAINER, "python3", "-c", script], input=json.dumps(values), text=True, check=True)
    print(f"Chat page updated: banner ({values['ui.banners'][0]['type']}) and {len(values['ui.prompt_suggestions'])} suggestions")


if __name__ == "__main__":
    publish()
