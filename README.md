# SAP B1 AI

A local AI assistant for **SAP Business One 10.0 on SQL Server**: stock planning, transaction red-flag checks and a
cash/margin outlook for management. It reads B1 through a read-only SQL login and never writes back to B1.
Everything runs on one PC: the analysis on the CPU overnight, a small local LLM (Ollama) to answer questions in chat.

## What it does

**Stock**
- Weekly demand forecast per item and warehouse (AutoETS / Croston SBA), including production consumption
- Time-phased shortage projection, reorder point and suggested order quantity; safety stock covers demand and
  supplier lead-time variation
- Warnings for items that will stop production (line-stop risk) or run out

**Transactions** (nightly red-flag checks, each with money lost or amount to review)
- Unusual purchase and sales prices, sales below cost
- Duplicate supplier invoices
- Three-way match: billed more than received, price above the purchase order, invoices without a PO
- Supplier bank detail changes, credit note spikes, suspicious manual journal entries

**Money**
- 13-week cash forecast from open invoices and orders, using each partner's real payment behaviour
- Customers likely to pay late, supplier payment timing
- Gross margins by customer, item and salesperson with trend; customers who stopped ordering; cross-sell pairs

**For people**
- Chat assistant (OpenAI-compatible tool-calling API for Open WebUI) with live B1 lookups and charts
- Morning briefing for management, internet market watch for the product categories the company depends on

## How it runs

```
02:00  sync -> forecast -> alerts -> anomalies -> finance -> webui -> market     (systemd user timer)
07:00  briefing
any    chat: Open WebUI -> agent (:8100) -> tools over the nightly results + read-only live B1 queries
```

Code is in `src/sapb1_ai/`: `sync.py` (B1 extract, incremental), `forecast.py`, `alerts.py`, `anomalies.py`,
`finance.py`, `tools.py` + `agent.py` (chat), `briefing.py`, `market.py`.

## Setup (outline)

1. Create a read-only SQL login on the B1 company database (`db_datareader` only).
2. `cp .env.example .env` and fill in the host, database, login and an API key.
3. `uv sync`, then `uv run sapb1-check` to test the connection and `uv run sapb1-sync` for the first extract.
4. Build the Ollama model from `ollama/Modelfile`; start Open WebUI with `docker compose up -d`.

## Copyright

Copyright (c) 2026 ez. **All rights reserved.**

This repository is public for viewing only. No permission is granted to copy, modify, distribute or use this code,
in whole or in part, without the author's written permission. SAP and SAP Business One are trademarks of SAP SE;
this project is not affiliated with or endorsed by SAP.
