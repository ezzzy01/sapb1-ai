"""Which company this process serves, and where its settings and data live.

Each company is separate: its own settings file, SQL login, nightly data and chat service. They share one chat site,
where the model menu is the company selector and each person sees only the companies their group allows.
  SAPB1_COMPANY=<id>  ->  settings from companies/<id>/company.env only (the shared .env is NOT read, so no
                          login, key or push channel can fall through from another company);
                          lists (critical_items.csv, market_watch.csv) from companies/<id>/.
  not set             ->  the single-company setup: .env and config/.
"""

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
COMPANY = os.getenv("SAPB1_COMPANY", "").strip()
REQUIRED = ("COMPANY_NAME", "SAPB1_DB", "SAPB1_SQL_HOST", "SAPB1_SQL_USER", "SAPB1_SQL_PASSWORD",
            "AGENT_API_KEY", "AGENT_PORT", "AGENT_MODEL_ID")

if COMPANY:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", COMPANY):
        raise SystemExit(f"Invalid company id {COMPANY!r}: use lowercase letters, digits and dashes")
    CONFIG_DIR = ROOT / "companies" / COMPANY
    _env = CONFIG_DIR / "company.env"
    if not _env.exists():
        raise SystemExit(f"Unknown company {COMPANY!r}: {_env} not found")
    _values = dotenv_values(_env)
    if missing := [k for k in REQUIRED if not _values.get(k)]:
        raise SystemExit(f"Missing in {_env}: {', '.join(missing)}")
    load_dotenv(_env, override=True)
else:
    CONFIG_DIR = ROOT / "config"
    load_dotenv(ROOT / ".env")


def config_file(name: str) -> Path:
    """A company list file; falls back to the empty template in config/ when the company has none yet."""
    own = CONFIG_DIR / name
    return own if own.exists() else ROOT / "config" / name
