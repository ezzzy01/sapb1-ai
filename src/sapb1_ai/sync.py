"""Nightly sync: copies the B1 tables the AI needs into local Parquet files.

Full extract each run. Read-only; nothing is written back to B1.
"""

import json
import os
from datetime import datetime
from pathlib import Path

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
        SELECT h.DocNum, h.CardCode, l.ItemCode, l.WhsCode, l.OpenInvQty AS Qty, COALESCE(l.ShipDate, h.DocDueDate) AS DueDate
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
        SELECT h.DocEntry, h.DocNum, h.DocDate, h.CardCode, h.CardName, h.UserSign, l.LineNum, l.ItemCode, l.Dscription,
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
    # Production bills of materials: which components each parent needs.
    "boms": """
        SELECT b.Father AS ParentItem, b.Code AS ItemCode, b.Quantity, t.TreeType
        FROM ITT1 b JOIN OITT t ON t.Code = b.Father
        WHERE b.Type = 4
    """,
}


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    meta = {"database": os.getenv("SAPB1_DB"), "synced_at": datetime.now().isoformat(timespec="seconds"), "rows": {}}
    try:
        for name, sql in QUERIES.items():
            df = query(sql, conn=conn)
            df.to_parquet(DATA_DIR / f"{name}.parquet", index=False)
            meta["rows"][name] = len(df)
            print(f"{name:<12}{len(df):>10,} rows")
        meta["currency"] = query("SELECT MainCurncy FROM OADM", conn=conn).MainCurncy.iloc[0]
    finally:
        conn.close()
    (DATA_DIR / "sync_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"\nSaved to {DATA_DIR}")


if __name__ == "__main__":
    main()
