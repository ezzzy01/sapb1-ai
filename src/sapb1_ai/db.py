"""Read-only access to the SAP B1 SQL Server database."""

import os
from decimal import Decimal
from pathlib import Path

import mssql_python
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def connect(database: str | None = None) -> mssql_python.Connection:
    """Open a connection using the settings in .env."""
    missing = [k for k in ("SAPB1_SQL_HOST", "SAPB1_SQL_USER", "SAPB1_SQL_PASSWORD") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (copy .env.example to .env)")

    db = database if database is not None else os.getenv("SAPB1_DB", "")
    parts = [
        f"Server={os.environ['SAPB1_SQL_HOST']},{os.getenv('SAPB1_SQL_PORT', '1433')}",
        f"UID={os.environ['SAPB1_SQL_USER']}",
        f"PWD={os.environ['SAPB1_SQL_PASSWORD']}",
        # Traffic already runs over Tailscale; the laptop uses a self-signed SQL certificate.
        "Encrypt=yes",
        "TrustServerCertificate=yes",
        "ApplicationIntent=ReadOnly",
    ]
    if db:
        parts.append(f"Database={db}")
    return mssql_python.connect(";".join(parts))


def query(sql: str, params: tuple = (), conn: mssql_python.Connection | None = None) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame."""
    own = conn is None
    conn = conn or connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        df = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
        # B1 stores quantities and amounts as DECIMAL; use floats so pandas and the models can work with them.
        for col in df.columns:
            first = df[col].dropna().head(1)
            if not first.empty and isinstance(first.iloc[0], Decimal):
                df[col] = df[col].astype(float)
        return df
    finally:
        if own:
            conn.close()
