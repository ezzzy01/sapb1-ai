"""Connection check: confirms access and summarises the B1 company database."""

import os

from .db import connect, query


def main() -> None:
    conn = connect()
    try:
        print(query("SELECT @@VERSION AS v", conn=conn).v[0].splitlines()[0])

        if not os.getenv("SAPB1_DB"):
            # B1 company databases are the ones that contain the OADM table.
            dbs = query("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name", conn=conn)
            print("\nSAPB1_DB is empty. Databases visible to this login:")
            print("\n".join(f"  - {n}" for n in dbs.name) or "  (none)")
            print("\nSet SAPB1_DB in .env to the company database and run again.")
            return

        info = query(
            """
            SELECT a.CompnyName, c.Version AS B1Version
            FROM OADM a CROSS JOIN CINF c
            """,
            conn=conn,
        ).iloc[0]
        stats = query(
            """
            SELECT
                (SELECT COUNT(*) FROM OITM)                          AS Items,
                (SELECT COUNT(*) FROM OITM WHERE InvntItem = 'Y')    AS StockItems,
                (SELECT COUNT(*) FROM OWHS)                          AS Warehouses,
                (SELECT COUNT(*) FROM OINM)                          AS StockMovements,
                (SELECT MIN(DocDate) FROM OINM)                      AS FirstMovement,
                (SELECT MAX(DocDate) FROM OINM)                      AS LastMovement,
                (SELECT COUNT(*) FROM OINV)                          AS Invoices
            """,
            conn=conn,
        ).iloc[0]

        print(f"\nCompany:          {info.CompnyName}")
        print(f"B1 version:       {info.B1Version}")
        for k, v in stats.items():
            print(f"{k + ':':<18}{v}")

        # Confirm the login cannot write.
        perms = query(
            "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE') "
            "WHERE permission_name IN ('INSERT','UPDATE','DELETE','ALTER')",
            conn=conn,
        )
        print("\nRead-only login: " + ("YES" if perms.empty else f"NO, has {', '.join(perms.permission_name)}"))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
