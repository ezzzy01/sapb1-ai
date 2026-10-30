"""Puts SAP B1 AI on the shared chat site (Open WebUI), one entry per company in the model menu.

The model menu at the top of the chat is the company selector. Each company stays separate behind it: its own chat
service, SQL login and data. On the site:
  register  (./company.sh, site level) one connection per started company to its own chat service, one group per
            company, and the company's menu entry readable only by that group. Open WebUI hides the entry from
            everyone else and refuses chats with it. The site-wide pop-up and suggestions carry no company data.
  publish   (nightly, per company) the company's warning line and suggested questions, shown on the start page
            while that company is selected.
Changes are written with Open WebUI's own code inside its container, so they follow its rules and schema.
"""

import json
import os
import subprocess
import sys

from dotenv import dotenv_values

from .company import ROOT
from .sync import DATA_DIR

CONTAINER = os.getenv("WEBUI_CONTAINER", "sapb1-ai-webui")
MODEL_ID = os.getenv("AGENT_MODEL_ID", "sapb1-stock-assistant")

# Site-wide start-page suggestions: generic, because everyone sees them whatever company they work in.
GENERIC_SUGGESTIONS = [
    {"title": ["How is the business doing?", "What needs a decision today"],
     "content": "How is the business doing and what needs a decision today? Keep it short."},
    {"title": ["Cash outlook", "Will we have enough cash?"],
     "content": "Will we have enough cash over the next 13 weeks? Show the lowest point and what drives it."},
    {"title": ["Who owes us money?", "Overdue customers"],
     "content": "Who owes us money? Show overdue customer invoices, biggest amounts first, and who is likely to pay late."},
    {"title": ["Invoices to check", "Duplicates, overcharges, red flags"],
     "content": "Which recent invoices or payments should finance check? Show possible duplicates, overcharges and other red flags, with the money involved."},
]

# Runs inside the Open WebUI container. Input on stdin: {"action": ..., ...}. Prints one JSON line.
_SITE_SCRIPT = r"""
import asyncio, json, logging, sqlite3, sys, time
logging.disable(logging.CRITICAL)
from open_webui.models.groups import GroupForm, Groups
from open_webui.models.models import ModelForm, Models
from open_webui.models.users import Users

req = json.loads(sys.stdin.read())
DB = "/app/backend/data/webui.db"


def set_config(values):
    c = sqlite3.connect(DB, timeout=30)
    changed = []
    for k, v in values.items():
        row = c.execute("select value from config where key=?", (k,)).fetchone()
        if row is None or json.loads(row[0]) != v:
            changed.append(k)
            c.execute("insert into config(key,value,updated_at) values(?,?,?) on conflict(key) do update set "
                      "value=excluded.value, updated_at=excluded.updated_at", (k, json.dumps(v), int(time.time())))
    c.commit()
    return changed


def form(model, **changes):
    current = {"id": model.id, "base_model_id": model.base_model_id, "name": model.name,
               "meta": model.meta.model_dump(), "params": model.params.model_dump(),
               "access_grants": [{"principal_type": g.principal_type, "principal_id": g.principal_id,
                                  "permission": g.permission} for g in model.access_grants]}
    return ModelForm(**(current | changes))


async def register():
    cos = req["companies"]
    changed = set_config({
        "openai.enable": True, "ollama.enable": False,
        "openai.api_base_urls": [c["url"] for c in cos], "openai.api_keys": [c["key"] for c in cos],
        "openai.api_configs": {str(i): {"enable": True, "connection_type": "external", "model_ids": [c["model_id"]]}
                               for i, c in enumerate(cos)},
        "ui.banners": [], "ui.prompt_suggestions": req["suggestions"], "ui.default_models": "",
    })
    admin = next(u for u in (await Users.get_users())["users"] if u.role == "admin")
    out = []
    for c in cos:
        group = await Groups.get_group_by_name(c["name"])
        if group is None:
            group = await Groups.insert_new_group(admin.id, GroupForm(
                name=c["name"], description=f"People who may use SAP B1 AI for {c['name']} ({c['database']})"))
        grants = [{"principal_type": "group", "principal_id": group.id, "permission": "read"}]
        model = await Models.get_model_by_id(c["model_id"])
        if model is None:
            await Models.insert_new_model(ModelForm(
                id=c["model_id"], base_model_id=None, name=c["name"], params={}, access_grants=grants,
                meta={"description": f"SAP B1 AI for {c['name']}"}), admin.id)
        else:
            await Models.update_model_by_id(c["model_id"], form(model, name=c["name"], access_grants=grants))
        out.append(c["name"])
    print(json.dumps({"connections_changed": any(k.startswith("openai.") or k.startswith("ollama.") for k in changed),
                      "companies": out}))


async def publish():
    model = await Models.get_model_by_id(req["model_id"])
    if model is None:
        print(json.dumps({"published": False}))
        return
    meta = model.meta.model_dump() | {"description": req["description"], "suggestion_prompts": req["suggestions"]}
    await Models.update_model_by_id(model.id, form(model, meta=meta))
    print(json.dumps({"published": True}))


asyncio.run({"register": register, "publish": publish}[req["action"]]())
"""


def _site(request: dict) -> dict:
    run = subprocess.run(["docker", "exec", "-i", "-w", "/app/backend", CONTAINER, "sh", "-c",
                          "WEBUI_SECRET_KEY=$(cat .webui_secret_key) python3 -c \"$0\"", _SITE_SCRIPT],
                         input=json.dumps(request), capture_output=True, text=True, check=True)
    return json.loads(run.stdout.strip().splitlines()[-1])


def description(alerts: dict) -> str:
    """One line under the company name on the start page: what needs attention today."""
    items = alerts.get("items", [])
    if not items:
        return "✅ No stock items at risk in the latest data."
    c = alerts["counts"]
    top = items[0]
    return (f"🛑 {c['LINE STOP RISK']} item(s) could stop production, {c['STOCKOUT RISK']} more at stockout risk "
            f"(most urgent: {top['ItemName']}). Ask \"show all critical alerts\".")


def suggestions(alerts: dict) -> list[dict]:
    """Result-first questions in everyday words: finance and management first, then today's stock risk."""
    out = [
        {"title": ["How is the business doing?", "What needs a decision today"],
         "content": "How is the business doing and what needs a decision today? Keep it short."},
        {"title": ["Cash outlook", "Will we have enough cash?"],
         "content": "Will we have enough cash over the next 13 weeks? Show the lowest point and what drives it."},
        {"title": ["Who owes us money?", "Overdue customers"],
         "content": "Who owes us money? Show overdue customer invoices, biggest amounts first, and who is likely to pay late."},
        {"title": ["Invoices to check", "Duplicates, overcharges, red flags"],
         "content": "Which recent invoices or payments should finance check? Show possible duplicates, overcharges and other red flags, with the money involved."},
        {"title": ["Margins", "Which customers and items earn the least?"],
         "content": "How are our margins? Show the customers and items with the lowest margin and whether it is getting better or worse."},
        {"title": ["Today's morning briefing", "Risks, money, sales, market"],
         "content": "Show me today's morning briefing."},
    ]
    items = alerts.get("items", [])
    if items:
        top = items[0]
        out.append({"title": [f"Why is {top['ItemName']}", "at risk and what do we do?"],
                    "content": f"Why is {top['ItemName']} ({top['ItemCode']}) at risk of running out, what does it affect, and what should we do today?"})
    out.append({"title": ["What should we order", "this week, and why?"],
                "content": "What should we order this week? Show item, quantity, value and why."})
    return out[:8]


def publish() -> None:
    alerts = json.loads((DATA_DIR / "alerts.json").read_text())
    result = _site({"action": "publish", "model_id": MODEL_ID, "description": description(alerts),
                    "suggestions": suggestions(alerts)})
    print("Start page updated for this company" if result["published"]
          else f"{MODEL_ID} is not on the chat site yet; run ./company.sh start for this company")


def register(company_ids: list[str]) -> None:
    """Put the given (started) companies on the chat site; companies left out disappear from its menu."""
    companies = []
    for cid in company_ids:
        env = dotenv_values(ROOT / "companies" / cid / "company.env")
        companies.append({"id": cid, "name": env["COMPANY_NAME"], "database": env["SAPB1_DB"],
                          "model_id": env["AGENT_MODEL_ID"], "key": env["AGENT_API_KEY"],
                          "url": f"http://host.docker.internal:{env['AGENT_PORT']}/v1"})
    result = _site({"action": "register", "companies": companies, "suggestions": GENERIC_SUGGESTIONS})
    print("On the chat site: " + (", ".join(result["companies"]) or "no companies"))
    if result["connections_changed"]:
        subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True)
        print("Chat site restarted to load the company list")


if __name__ == "__main__":
    if sys.argv[1:2] == ["register"]:
        register(sys.argv[2:])
    else:
        publish()
