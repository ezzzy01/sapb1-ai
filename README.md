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
02:00  sync -> forecast -> alerts -> anomalies -> finance -> webui -> market     (each company in turn)
07:00  briefing                                                                  (each company in turn)
any    chat: company's Open WebUI -> company's agent -> its nightly results + read-only live B1 queries
```

Code is in `src/sapb1_ai/`: `sync.py` (B1 extract, incremental), `forecast.py`, `alerts.py`, `anomalies.py`,
`finance.py`, `tools.py` + `agent.py` (chat), `briefing.py`, `market.py`, `company.py` (which company a process serves).

## Several companies

Each company gets its own SAP B1 AI, and nothing is shared between them except the AI model, which keeps nothing:

| Per company | Where |
|---|---|
| Settings, SQL login, chat API key, push channels | `companies/<id>/company.env` (git-ignored) |
| Critical items, market topics | `companies/<id>/*.csv` |
| Nightly data | `data/<B1 database>/` |
| Chat service | `sapb1-ai-agent@<id>` on its own port |
| Chat site, accounts and chats | its own Open WebUI container and volume, on its own port |

A process started for one company reads only that company's file, never the shared `.env`. The chat can only query its
own database, and admins can't read users' chats. For full separation give each company a SQL login that can open only
its own database.

```
./company.sh add <id> <B1 database> "<name shown>"   # then fill in its SQL login in companies/<id>/company.env
./company.sh start <id>                              # checks the login, starts its chat service and site
./company.sh list
```

## Setup (outline)

1. Create a read-only SQL login per B1 company database (`db_datareader` on that database only).
2. `cp .env.example .env` and fill in the SQL host (used as the default for new companies).
3. `uv sync`; build the Ollama model from `ollama/Modelfile`; `./company.sh install` for the background services.
4. `./company.sh add ...` and `./company.sh start ...` for each company.

## Copyright

Copyright (c) 2026 ez. **All rights reserved.**

This repository is public for viewing only. No permission is granted to copy, modify, distribute or use this code,
in whole or in part, without the author's written permission. SAP and SAP Business One are trademarks of SAP SE;
this project is not affiliated with or endorsed by SAP.
