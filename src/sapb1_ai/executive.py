"""Executive (CEO) view: the business in money and risk, built from the synced B1 data and the reorder report."""

import json

import numpy as np
import pandas as pd

from .sync import DATA_DIR


def _read(name: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def _money(x) -> float:
    return round(float(x), 2) if pd.notna(x) else 0.0


def _pct(new: float, old: float):
    return round((new - old) / old * 100, 1) if old else None


def executive_summary() -> dict:
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    sales = _read("sales")
    open_so = _read("open_sales")
    ar = _read("ar_open")
    lead = _read("lead_times")
    today = pd.Timestamp.today().normalize()

    # ---- Inventory: where the cash sits ----
    cost = rep.unit_cost.fillna(0)
    stock_value = (rep.OnHand.clip(lower=0) * cost)
    excess_units = (rep.OnHand - rep.order_up_to).clip(lower=0)
    inventory = {
        "stock_value": _money(stock_value.sum()),
        "dead_stock_value": _money(stock_value[rep.status == "DEAD STOCK"].sum()),
        "excess_stock_value": _money((excess_units * cost)[rep.status == "OVERSTOCK"].sum()),
        "suggested_purchases_value": _money(rep.suggested_value.sum()),
        "items_without_cost": int((cost == 0).sum()),
    }

    # ---- Operational risk: what could stop the business ----
    risky = rep[rep.status.isin(["LINE STOP RISK", "STOCKOUT RISK"])]
    risky_keys = set(zip(risky.ItemCode, risky.WhsCode))
    revenue_at_risk = 0.0
    customers_affected = []
    if not open_so.empty:
        so = open_so[[k in risky_keys for k in zip(open_so.ItemCode, open_so.WhsCode)]]
        revenue_at_risk = so.OpenValue.sum()
        customers_affected = (so.groupby("CardName").OpenValue.sum().sort_values(ascending=False).head(5).round(2)
                              .reset_index().rename(columns={"CardName": "customer", "OpenValue": "open_order_value"})
                              .to_dict("records"))
    blocked = risky.production_orders_at_risk.dropna()
    risk = {
        "line_stop_risk_items": int((rep.status == "LINE STOP RISK").sum()),
        "stockout_risk_items": int((rep.status == "STOCKOUT RISK").sum()),
        "production_orders_at_risk": int(blocked.str.count("WO ").sum()) if not blocked.empty else 0,
        "open_sales_value_on_short_items": _money(revenue_at_risk),
        "customers_most_affected": customers_affected,
        "most_urgent": risky.sort_values("days_to_runout").head(5)[["status", "ItemCode", "ItemName", "WhsCode", "action"]].to_dict("records"),
    }

    # ---- Sales: trend against the previous period ----
    sales_block = {}
    if not sales.empty:
        s = sales.assign(DocDate=pd.to_datetime(sales.DocDate))
        end = s.DocDate.max().normalize()
        def period(days, back=0):
            hi = end - pd.Timedelta(days=days * back)
            return s[(s.DocDate > hi - pd.Timedelta(days=days)) & (s.DocDate <= hi)].LineTotal.sum()
        r30, p30, r90, p90 = period(30), period(30, 1), period(90), period(90, 1)
        last365 = s[s.DocDate > end - pd.Timedelta(days=365)]
        monthly = last365.groupby(last365.DocDate.dt.to_period("M")).LineTotal.sum().round(2)
        top_cust = s[s.DocDate > end - pd.Timedelta(days=90)].groupby("CardName").LineTotal.sum().sort_values(ascending=False).head(5).round(2)
        top_items = s[s.DocDate > end - pd.Timedelta(days=90)].groupby("ItemCode").LineTotal.sum().sort_values(ascending=False).head(5).round(2)
        sales_block = {
            "data_up_to": str(end.date()),
            "revenue_last_30_days": _money(r30), "change_vs_previous_30_days_pct": _pct(r30, p30),
            "revenue_last_90_days": _money(r90), "change_vs_previous_90_days_pct": _pct(r90, p90),
            "monthly_revenue_last_12_months": {str(k): float(v) for k, v in monthly.items()},
            "top_customers_90_days": top_cust.reset_index().rename(columns={"CardName": "customer", "LineTotal": "revenue"}).to_dict("records"),
            "top_items_90_days": top_items.reset_index().rename(columns={"LineTotal": "revenue"}).to_dict("records"),
        }

    # ---- Order book ----
    order_book = {}
    if not open_so.empty:
        need = pd.to_datetime(open_so.NeedDate)
        order_book = {
            "open_sales_orders_value": _money(open_so.OpenValue.sum()),
            "overdue_sales_orders_value": _money(open_so.OpenValue[need < today].sum()),
            "overdue_sales_order_lines": int((need < today).sum()),
        }

    # ---- Cash: unpaid customer invoices ----
    receivables = {}
    if not ar.empty:
        due = pd.to_datetime(ar.DocDueDate)
        overdue = ar[due < today].assign(days=(today - due[due < today]).dt.days)
        receivables = {
            "total_unpaid": _money(ar.Balance.sum()),
            "overdue": _money(overdue.Balance.sum()),
            "overdue_over_90_days": _money(overdue.Balance[overdue.days > 90].sum()),
            "top_overdue_customers": overdue.groupby("CardName").Balance.sum().sort_values(ascending=False).head(5).round(2)
                                            .reset_index().rename(columns={"CardName": "customer", "Balance": "overdue"}).to_dict("records"),
        }

    # ---- Suppliers: delivery reliability over the last 12 months ----
    suppliers = {}
    if not lead.empty and "DaysLate" in lead.columns:
        l = lead[pd.to_datetime(lead.GRPODate) > pd.to_datetime(lead.GRPODate).max() - pd.Timedelta(days=365)]
        if not l.empty:
            per = l.groupby("CardName").agg(receipts=("DaysLate", "size"), on_time_pct=("DaysLate", lambda d: round((d <= 0).mean() * 100, 1)),
                                            avg_days_late=("DaysLate", lambda d: round(d.clip(lower=0).mean(), 1)))
            suppliers = {
                "on_time_delivery_pct": round(float((l.DaysLate <= 0).mean() * 100), 1),
                "least_reliable": per[per.receipts >= 3].sort_values("on_time_pct").head(5).reset_index()
                                     .rename(columns={"CardName": "supplier"}).to_dict("records"),
            }

    return {
        "company_database": meta.get("database"), "currency": meta.get("currency"), "data_synced_at": meta.get("synced_at"),
        "operational_risk": risk, "inventory": inventory, "sales": sales_block, "order_book": order_book,
        "receivables": receivables, "suppliers": suppliers,
    }


if __name__ == "__main__":
    print(json.dumps(executive_summary(), indent=2, default=str))
