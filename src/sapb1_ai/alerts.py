"""Builds stock warnings from the reorder report and pushes them, after each nightly refresh.

Writes alerts.json (read by the chat assistant) and, when configured in .env, sends a push:
  NTFY_URL=https://ntfy.example.com/your-private-topic   phone push via the ntfy app
  ALERT_WEBHOOK_URL=https://...                           Teams / Slack / Google Chat style webhook ({"text": ...})
Both are off when empty.
"""

import json
import os
from datetime import datetime

import httpx
import pandas as pd

from .sync import DATA_DIR

URGENT = ["LINE STOP RISK", "STOCKOUT RISK"]
MAX_LISTED = 10


def build() -> dict:
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    urgent = rep[rep.status.isin(URGENT)].copy()
    urgent["rank"] = urgent.status.map({s: i for i, s in enumerate(URGENT)})
    urgent = urgent.sort_values(["rank", "days_to_runout"], na_position="last")
    cols = ["status", "critical", "ItemCode", "ItemName", "WhsCode", "available", "runout_date", "days_to_runout",
            "lead_days", "suggested_qty", "production_orders_at_risk", "critical_reason", "action"]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": meta.get("database"),
        "data_synced_at": meta.get("synced_at"),
        "counts": {s: int((rep.status == s).sum()) for s in URGENT},
        "items": json.loads(urgent[cols].to_json(orient="records")),
    }


def message(alerts: dict) -> tuple[str, str]:
    c = alerts["counts"]
    title = f"SAP B1 stock: {c['LINE STOP RISK']} line-stop risk, {c['STOCKOUT RISK']} stockout risk"
    lines = []
    for a in alerts["items"][:MAX_LISTED]:
        icon = "🛑" if a["status"] == "LINE STOP RISK" else "⚠️"
        lines.append(f"{icon} {a['ItemCode']} ({a['WhsCode']}) {a['ItemName']}: {a['action']}")
    more = len(alerts["items"]) - MAX_LISTED
    if more > 0:
        lines.append(f"...and {more} more. Ask the SAP B1 Stock Assistant for the full list.")
    return title, "\n".join(lines)


def push(alerts: dict) -> None:
    if not alerts["items"]:
        return
    title, body = message(alerts)
    line_stop = alerts["counts"]["LINE STOP RISK"] > 0
    if url := os.getenv("NTFY_URL"):
        httpx.post(url, content=body.encode(), timeout=20,
                   headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                            "Priority": "urgent" if line_stop else "high", "Tags": "rotating_light" if line_stop else "warning"})
        print(f"Pushed to ntfy ({len(alerts['items'])} items)")
    if url := os.getenv("ALERT_WEBHOOK_URL"):
        httpx.post(url, json={"text": f"**{title}**\n\n{body}"}, timeout=20)
        print(f"Pushed to webhook ({len(alerts['items'])} items)")


def main() -> None:
    alerts = build()
    (DATA_DIR / "alerts.json").write_text(json.dumps(alerts, indent=2))
    print(message(alerts)[0])
    try:
        push(alerts)
    except httpx.HTTPError as e:
        print(f"Push failed: {e}")


if __name__ == "__main__":
    main()
