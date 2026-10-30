"""Nightly sync: copies the B1 tables the AI needs into local Parquet files.

Small tables are re-read in full each run; large document tables incrementally (see INCREMENTAL),
with a full re-read on Sundays. Read-only; nothing is written back to B1.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from .db import connect, query

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / os.getenv("SAPB1_DB", "default")

QUERIES = {
    # Stock items and their planning fields.
    "items": """
        SELECT i.ItemCode, i.ItemName, i.ItmsGrpCod, g.ItmsGrpNam,
               i.InvntryUom, i.AvgPrice, i.LastPurPrc, i.CardCode AS PrefVendor,
               i.LeadTime, i.MinOrdrQty, i.OrdrMulti,
               i.validFor, i.frozenFor
        FROM OITM i
        LEFT JOIN OITB g ON g.ItmsGrpCod = i.ItmsGrpCod
        WHERE i.InvntItem = 'Y'
    """,
    "warehouses": "SELECT WhsCode, WhsName, Inactive FROM OWHS",
    # Current stock position per item and warehouse.
    "stock": """
        SELECT w.ItemCode, w.WhsCode, w.OnHand, w.IsCommited, w.OnOrder, w.MinStock, w.MaxStock
        FROM OITW w
        JOIN OITM i ON i.ItemCode = w.ItemCode AND i.InvntItem = 'Y'
    """,
    # Demand: A/R invoices minus A/R credit notes, in inventory units.
    # CANCELED = 'N' drops both cancelled documents and their cancellation documents.
    "sales": """
        SELECT h.DocDate, l.ItemCode, l.WhsCode, l.InvQty AS Qty, l.LineTotal, 'INV' AS Src, h.CardCode, h.CardName
        FROM OINV h JOIN INV1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND h.DocType = 'I' AND l.ItemCode IS NOT NULL
        UNION ALL
        SELECT h.DocDate, l.ItemCode, l.WhsCode, -l.InvQty, -l.LineTotal, 'RIN', h.CardCode, h.CardName
        FROM ORIN h JOIN RIN1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND h.DocType = 'I' AND l.ItemCode IS NOT NULL
    """,
    # Actual supplier lead times: purchase order date to goods receipt date.
    "lead_times": """
        SELECT l.ItemCode, h.CardCode, h.CardName, po.DocDate AS PODate, po.DocDueDate AS PODueDate, h.DocDate AS GRPODate,
               DATEDIFF(day, po.DocDate, h.DocDate) AS LeadDays, DATEDIFF(day, po.DocDueDate, h.DocDate) AS DaysLate, l.Quantity AS Qty
        FROM OPDN h
        JOIN PDN1 l ON l.DocEntry = h.DocEntry
        JOIN OPOR po ON po.DocEntry = l.BaseEntry
        WHERE l.BaseType = 22 AND h.CANCELED = 'N'
    """,
    # Consumption by production: issues for production orders (BaseType 202), incl. backflush.
    "production_issues": """
        SELECT h.DocDate, l.ItemCode, l.WhsCode, l.Quantity AS Qty, l.LineTotal, 'PRD' AS Src
        FROM OIGE h JOIN IGE1 l ON l.DocEntry = h.DocEntry
        WHERE l.BaseType = 202 AND l.ItemCode IS NOT NULL
    """,
    # Component quantities still to be issued to planned (P) and released (R) production orders.
    "production_needs": """
        SELECT o.DocNum, o.Status, o.ItemCode AS ParentItem, l.ItemCode, l.wareHouse AS WhsCode,
               l.PlannedQty - l.IssuedQty AS Qty, COALESCE(o.StartDate, o.DueDate) AS NeedDate, o.DueDate
        FROM OWOR o JOIN WOR1 l ON l.DocEntry = o.DocEntry
        WHERE o.Status IN ('P', 'R') AND l.ItemType = 4 AND l.PlannedQty > l.IssuedQty
    """,
    # Finished goods still to come out of planned/released production orders.
    "production_supply": """
        SELECT o.DocNum, o.Status, o.ItemCode, o.Warehouse AS WhsCode, o.PlannedQty - o.CmpltQty AS Qty, o.DueDate
        FROM OWOR o
        WHERE o.Status IN ('P', 'R') AND o.PlannedQty > o.CmpltQty
    """,
    # Open sales order lines (dated customer demand).
    "open_sales": """
        SELECT h.DocNum, h.CardCode, h.CardName, l.ItemCode, l.WhsCode, l.OpenInvQty AS Qty, l.OpenSum AS OpenValue,
               COALESCE(l.ShipDate, h.DocDueDate) AS NeedDate
        FROM ORDR h JOIN RDR1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND l.LineStatus = 'O' AND l.OpenInvQty > 0 AND l.ItemCode IS NOT NULL
    """,
    # Open purchase order lines (dated incoming supply).
    "open_purchases": """
        SELECT h.DocNum, h.CardCode, l.ItemCode, l.WhsCode, l.OpenInvQty AS Qty, l.OpenSum AS OpenValue,
               COALESCE(l.ShipDate, h.DocDueDate) AS DueDate
        FROM OPOR h JOIN POR1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND l.LineStatus = 'O' AND l.OpenInvQty > 0 AND l.ItemCode IS NOT NULL
    """,
    # Unpaid customer invoices (receivables), in local currency.
    "ar_open": """
        SELECT DocNum, CardCode, CardName, DocDate, DocDueDate, DocTotal, PaidToDate, DocTotal - PaidToDate AS Balance
        FROM OINV
        WHERE CANCELED = 'N' AND DocStatus = 'O' AND DocTotal > PaidToDate
    """,
    # A/R invoice lines for price and margin checks. LineTotal is before the document discount; GrssProfit is in local currency.
    "ar_lines": """
        SELECT h.DocEntry, h.DocNum, h.DocDate, h.CardCode, h.CardName, h.SlpCode, h.UserSign, l.LineNum, l.ItemCode, l.Dscription,
               l.WhsCode, l.InvQty, l.DiscPrcnt AS LineDisc, h.DiscPrcnt AS DocDisc, h.DocTotal, h.PaidToDate, l.LineTotal, l.GrssProfit
        FROM OINV h JOIN INV1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND h.DocType = 'I' AND l.ItemCode IS NOT NULL
    """,
    # A/P invoice lines for purchase price checks.
    "ap_lines": """
        SELECT h.DocEntry, h.DocNum, h.DocDate, h.CardCode, h.CardName, h.UserSign, l.LineNum, l.ItemCode, l.Dscription,
               l.WhsCode, l.InvQty, l.DiscPrcnt AS LineDisc, h.DiscPrcnt AS DocDisc, h.DocTotal, h.PaidToDate, l.LineTotal
        FROM OPCH h JOIN PCH1 l ON l.DocEntry = h.DocEntry
        WHERE h.CANCELED = 'N' AND h.DocType = 'I' AND l.ItemCode IS NOT NULL
    """,
    # A/P invoice headers (item and service) for duplicate checks, in local currency.
    "ap_invoices": """
        SELECT DocEntry, DocNum, DocDate, TaxDate, DocDueDate, CardCode, CardName, NumAtCard, DocType,
               DocTotal, DocCur, DocTotalFC, PaidToDate, DocStatus, UserSign
        FROM OPCH
        WHERE CANCELED = 'N'
    """,
    # Three-way match: each A/P invoice line with the goods receipt and purchase order line it was copied from.
    "ap_match": """
        SELECT h.DocEntry, h.DocNum, h.DocDate, h.CardCode, h.CardName, h.DocTotal, h.PaidToDate, l.LineNum, l.ItemCode,
               l.Dscription, l.Quantity, l.Price, l.Currency, l.LineTotal, l.BaseType,
               g.DocEntry AS GRPOEntry, g.LineNum AS GRPOLine, gh.DocNum AS GRPONum, g.Quantity AS GRPOQty,
               po.DocNum AS PONum, p.Price AS POPrice, p.Currency AS POCurrency
        FROM OPCH h JOIN PCH1 l ON l.DocEntry = h.DocEntry
        LEFT JOIN PDN1 g ON l.BaseType = 20 AND g.DocEntry = l.BaseEntry AND g.LineNum = l.BaseLine
        LEFT JOIN OPDN gh ON gh.DocEntry = g.DocEntry
        LEFT JOIN POR1 p ON (g.BaseType = 22 AND p.DocEntry = g.BaseEntry AND p.LineNum = g.BaseLine)
                         OR (l.BaseType = 22 AND p.DocEntry = l.BaseEntry AND p.LineNum = l.BaseLine)
        LEFT JOIN OPOR po ON po.DocEntry = p.DocEntry
        WHERE h.CANCELED = 'N' AND h.DocType = 'I' AND l.ItemCode IS NOT NULL
    """,
    # Supplier bank details over time: B1's change log (ACRD) plus the current master record.
    "bp_bank_history": """
        SELECT CardCode, CardName, LogInstanc, UpdateDate, UserSign2, DflAccount, BankCode, DflBranch, DflIBAN, DflSwift
        FROM ACRD WHERE CardType = 'S'
        UNION ALL
        SELECT CardCode, CardName, 2147483647, UpdateDate, UserSign2, DflAccount, BankCode, DflBranch, DflIBAN, DflSwift
        FROM OCRD WHERE CardType = 'S'
    """,
    # Outgoing payments (headers), local currency.
    "ap_payments": """
        SELECT DocEntry, DocNum, DocDate, CardCode, CardName, DocTotal, TrsfrSum
        FROM OVPM
        WHERE Canceled = 'N' AND CardCode IS NOT NULL
    """,
    # Outgoing payments applied to A/P invoices: when we pay compared with the due date.
    "ap_payments_applied": """
        SELECT p.DocEntry AS PayEntry, p.DocDate AS PayDate, p.CardCode, p.CardName, i.DocEntry AS InvEntry,
               i.DocDate AS InvDate, i.DocDueDate, l.SumApplied
        FROM OVPM p JOIN VPM2 l ON l.DocNum = p.DocEntry JOIN OPCH i ON i.DocEntry = l.DocEntry
        WHERE p.Canceled = 'N' AND l.InvType = 18
    """,
    # A/R invoice headers: payment terms, salesperson, balance.
    "ar_invoices": """
        SELECT DocEntry, DocNum, DocDate, DocDueDate, CardCode, CardName, SlpCode, DocTotal, PaidToDate, DocStatus
        FROM OINV
        WHERE CANCELED = 'N'
    """,
    # Incoming payments applied to A/R invoices: how late each customer really pays.
    "ar_payments": """
        SELECT r.DocEntry AS PayEntry, r.DocDate AS PayDate, r.CardCode, i.DocEntry AS InvEntry, i.DocDate AS InvDate,
               i.DocDueDate, l.SumApplied
        FROM ORCT r JOIN RCT2 l ON l.DocNum = r.DocEntry JOIN OINV i ON i.DocEntry = l.DocEntry
        WHERE r.Canceled = 'N' AND l.InvType = 13
    """,
    # A/R credit notes (headers), for credit note spikes by customer and salesperson.
    "ar_credits": """
        SELECT DocEntry, DocNum, DocDate, CardCode, CardName, SlpCode, DocTotal, UserSign
        FROM ORIN
        WHERE CANCELED = 'N'
    """,
    # Manual journal entries (TransType 30) and their lines.
    "journal_lines": """
        SELECT h.TransId, h.Number, h.RefDate, h.CreateDate, h.CreateTime, h.UserSign, h.Memo, h.LocTotal, h.StornoToTr,
               l.Line_ID, l.Account, l.ShortName, l.Debit, l.Credit
        FROM OJDT h JOIN JDT1 l ON l.TransId = h.TransId
        WHERE h.TransType = 30
    """,
    "accounts": "SELECT AcctCode, AcctName, Finanse, Postable, CurrTotal FROM OACT",
    "users": "SELECT USERID, USER_CODE, U_NAME FROM OUSR",
    "salespeople": "SELECT SlpCode, SlpName FROM OSLP",
    # Production bills of materials: which components each parent needs.
    "boms": """
        SELECT b.Father AS ParentItem, b.Code AS ItemCode, b.Quantity, t.TreeType
        FROM ITT1 b JOIN OITT t ON t.Code = b.Father
        WHERE b.Type = 4
    """,
}


# Large document tables are synced incrementally: only documents whose header changed since the last sync
# (new, paid, cancelled...) are re-read and replaced. Name -> (header table, its key, query's UpdateDate column, key column).
INCREMENTAL = {
    "ar_lines": ("OINV", "DocEntry", "h.UpdateDate", "DocEntry"),
    "ap_lines": ("OPCH", "DocEntry", "h.UpdateDate", "DocEntry"),
    "ap_invoices": ("OPCH", "DocEntry", "UpdateDate", "DocEntry"),
    "ap_match": ("OPCH", "DocEntry", "h.UpdateDate", "DocEntry"),
    "ar_invoices": ("OINV", "DocEntry", "UpdateDate", "DocEntry"),
    "ar_payments": ("ORCT", "DocEntry", "r.UpdateDate", "PayEntry"),
    "ap_payments_applied": ("OVPM", "DocEntry", "p.UpdateDate", "PayEntry"),
    "journal_lines": ("OJDT", "TransId", "h.UpdateDate", "TransId"),
}
LOOKBACK_DAYS = 3  # UpdateDate has no time; re-read a few days so nothing is missed


def _sync_incremental(name: str, sql: str, since: datetime, conn) -> pd.DataFrame:
    table, hkey, upd, key = INCREMENTAL[name]
    old = pd.read_parquet(DATA_DIR / f"{name}.parquet")
    changed = query(f"SELECT {hkey} AS k FROM {table} WHERE UpdateDate >= ?", (since,), conn=conn).k
    new = query(f"{sql} AND {upd} >= ?", (since,), conn=conn)
    keep = old[~old[key].isin(changed) & ~old[key].isin(new[key])]
    return pd.concat([keep, new], ignore_index=True) if not new.empty else keep


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = DATA_DIR / "sync_meta.json"
    last = json.loads(meta_path.read_text()).get("synced_at") if meta_path.exists() else None
    # Full re-read on Sundays (or FULL_SYNC=1) as a safety net; incremental on other nights.
    full = os.getenv("FULL_SYNC") == "1" or datetime.now().weekday() == 6 or not last
    since = (pd.Timestamp(last).normalize() - pd.Timedelta(days=LOOKBACK_DAYS)).to_pydatetime() if last else None
    conn = connect()
    meta = {"database": os.getenv("SAPB1_DB"), "synced_at": datetime.now().isoformat(timespec="seconds"),
            "full_sync": full, "rows": {}}
    try:
        for name, sql in QUERIES.items():
            if not full and name in INCREMENTAL and (DATA_DIR / f"{name}.parquet").exists():
                df, how = _sync_incremental(name, sql, since, conn), "incremental"
            else:
                df, how = query(sql, conn=conn), "full"
            df.to_parquet(DATA_DIR / f"{name}.parquet", index=False)
            meta["rows"][name] = len(df)
            print(f"{name:<20}{len(df):>10,} rows  {how}")
        meta["currency"] = query("SELECT MainCurncy FROM OADM", conn=conn).MainCurncy.iloc[0]
    finally:
        conn.close()
    (DATA_DIR / "sync_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"\nSaved to {DATA_DIR}")


if __name__ == "__main__":
    main()
