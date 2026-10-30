/* SAP B1 alert: components that cannot cover open production orders and sales orders.
   Works inside B1 on its own (no AI needed), so it still warns if the AI desktop is off.

   Set up in SAP B1 (once):
     1. Tools > Queries > Query Generator > Execute this query, then Save as "Line stop risk".
     2. Administration > Alerts Management > new alert "Line stop risk", Priority High,
        add recipients, Type: User-Defined, select the saved query, frequency e.g. every 1 hour.
   B1 then pops up an alert (and sends a B1 message) whenever this query returns rows.

   Shortage = on hand + on order - committed (released production + sales orders) - still-planned production needs.
*/
SELECT
    w.ItemCode                                         AS "Item",
    i.ItemName                                         AS "Description",
    w.WhsCode                                          AS "Whs",
    w.OnHand                                           AS "On hand",
    w.IsCommited                                       AS "Committed",
    w.OnOrder                                          AS "On order",
    ISNULL(p.PlannedNeed, 0)                           AS "Planned WO need",
    w.OnHand + w.OnOrder - w.IsCommited - ISNULL(p.PlannedNeed, 0) AS "Projected",
    p.FirstNeedDate                                    AS "First WO needs it",
    p.OpenOrders                                       AS "Open WOs",
    CASE WHEN w.OnHand - w.IsCommited < 0 THEN 'SHORT NOW' ELSE 'SHORT LATER' END AS "Severity"
FROM OITW w
JOIN OITM i ON i.ItemCode = w.ItemCode AND i.InvntItem = 'Y' AND i.frozenFor = 'N'
LEFT JOIN (
    SELECT l.ItemCode, l.wareHouse,
           SUM(CASE WHEN o.Status = 'P' THEN l.PlannedQty - l.IssuedQty ELSE 0 END) AS PlannedNeed,
           MIN(COALESCE(o.StartDate, o.DueDate)) AS FirstNeedDate,
           COUNT(DISTINCT o.DocNum) AS OpenOrders
    FROM WOR1 l
    JOIN OWOR o ON o.DocEntry = l.DocEntry
    WHERE o.Status IN ('P', 'R') AND l.ItemType = 4 AND l.PlannedQty > l.IssuedQty
    GROUP BY l.ItemCode, l.wareHouse
) p ON p.ItemCode = w.ItemCode AND p.wareHouse = w.WhsCode
WHERE w.OnHand + w.OnOrder - w.IsCommited - ISNULL(p.PlannedNeed, 0) < 0
  AND (p.OpenOrders > 0 OR w.IsCommited > 0)
ORDER BY CASE WHEN w.OnHand - w.IsCommited < 0 THEN 0 ELSE 1 END, p.FirstNeedDate
