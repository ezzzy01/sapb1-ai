"""Nightly money analysis: 13-week cash forecast, late payers, supplier payment timing, margins, churn and cross-sell.

Reads the Parquet files written by sync.py and writes finance.json (read by the chat assistant and the briefing).
All amounts in local currency. Statistical, not black-box: every number can be traced to B1 documents.

Method:
  - Late payers: for each paid customer invoice, days paid after the due date (weighted by amount), over the last
    two years. Per customer the average is shrunk towards the company average (SHRINK pseudo-invoices), so a
    customer with one late payment is not judged on it alone. Open invoices are expected at due date + that delay.
  - Cash forecast: cash accounts today + expected receipts (open A/R, then open sales orders once invoiced on their
    ship date with the customer's usual terms) - expected payments (open A/P at due date + our usual delay with that
    supplier, open purchase orders likewise). Invoices more than OLD_DAYS past their expected date are left out as
    doubtful and reported separately; so are orders more than OLD_DAYS past their ship/due date.
  - Margins: invoice lines of the last 12 months after document discounts (credit notes not included).
    Trend compares the last 90 days with the 275 days before.
  - Churn: regular customers (4+ invoices in two years) whose silence is over 3x their usual gap and 60+ days.
  - Cross-sell: item pairs bought on the same invoice at least 3 times and at least 1.5x more often than chance.
"""

import json
from datetime import datetime
from itertools import combinations

import numpy as np
import pandas as pd

from .sync import DATA_DIR

WEEKS = 13
SHRINK = 5
OLD_DAYS = 60
TOP = 10


def _read(name: str, dates: tuple = ()) -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{name}.parquet")
    for c in dates:
        df[c] = pd.to_datetime(df[c])
    return df


def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.round(2).to_json(orient="records", date_format="iso"))


def payment_delay(paid: pd.DataFrame, key: str, today: pd.Timestamp) -> tuple[pd.DataFrame, float]:
    """Per partner: invoices paid, on-time share and amount-weighted days paid after due date (shrunk to the average)."""
    p = paid[paid.PayDate >= today - pd.Timedelta(days=730)].copy()
    p["late"] = (p.PayDate - p.DocDueDate).dt.days
    p["w"] = p.SumApplied.clip(lower=0.01)
    overall = float(np.average(p.late, weights=p.w)) if len(p) else 0.0
    g = p.assign(wl=p.late * p.w, ontime=(p.late <= 0) * p.w).groupby(key).agg(
        invoices=("InvEntry", "nunique"), w=("w", "sum"), wl=("wl", "sum"), ontime=("ontime", "sum"))
    g["avg_days_late"] = (g.wl / g.w)
    g["on_time_pct"] = g.ontime / g.w * 100
    g["expected_days_late"] = (g.invoices * g.avg_days_late + SHRINK * overall) / (g.invoices + SHRINK)
    return g[["invoices", "on_time_pct", "avg_days_late", "expected_days_late"]], overall


def late_payers(today: pd.Timestamp) -> tuple[dict, pd.DataFrame]:
    inv = _read("ar_invoices", ("DocDate", "DocDueDate"))
    paid = _read("ar_payments", ("PayDate", "DocDueDate"))
    stats, overall = payment_delay(paid, "CardCode", today)
    names = inv.drop_duplicates("CardCode").set_index("CardCode").CardName
    open_ = inv[(inv.DocStatus == "O") & (inv.DocTotal > inv.PaidToDate)].copy()
    open_["balance"] = open_.DocTotal - open_.PaidToDate
    open_["expected_days_late"] = open_.CardCode.map(stats.expected_days_late).fillna(overall)
    open_["expected_pay_date"] = open_.DocDueDate + pd.to_timedelta(open_.expected_days_late.round(), unit="D")
    open_["days_overdue"] = (today - open_.DocDueDate).dt.days
    likely = open_[(open_.expected_days_late > 7) & (open_.days_overdue < OLD_DAYS)]
    cust = stats.join(open_.groupby("CardCode").balance.sum().rename("open_balance")).fillna({"open_balance": 0})
    cust["customer"] = cust.index.map(names)
    worst = cust[cust.invoices >= 2].sort_values(["avg_days_late", "open_balance"], ascending=False).head(TOP)
    return {
        "company_avg_days_paid_after_due": round(overall, 1),
        "worst_payers": _records(worst.reset_index()[["CardCode", "customer", "invoices", "on_time_pct", "avg_days_late", "open_balance"]]),
        "open_invoices_likely_late": {
            "count": len(likely), "value": round(float(likely.balance.sum()), 2),
            "top": _records(likely.sort_values("balance", ascending=False).head(TOP)[
                ["DocNum", "CardName", "balance", "DocDueDate", "expected_pay_date"]].rename(columns={"DocNum": "invoice"})),
        },
    }, open_


def supplier_payments(today: pd.Timestamp) -> tuple[dict, pd.DataFrame, float]:
    paid = _read("ap_payments_applied", ("PayDate", "DocDueDate"))
    stats, overall = payment_delay(paid, "CardCode", today)
    stats["supplier"] = stats.index.map(paid.drop_duplicates("CardCode").set_index("CardCode").CardName)
    cols = ["CardCode", "supplier", "invoices", "on_time_pct", "avg_days_late"]
    early = stats[stats.avg_days_late < -3].sort_values("avg_days_late").head(5).reset_index()
    late = stats[stats.avg_days_late > 3].sort_values("avg_days_late", ascending=False).head(5).reset_index()
    return {
        "company_avg_days_paid_after_due": round(overall, 1),
        "note": "negative = we pay before the due date (cash leaves early); positive = we pay late (supplier risk)",
        "paid_early_most": _records(early[cols]),
        "paid_late_most": _records(late[cols]),
    }, stats, overall


def _terms(df: pd.DataFrame) -> tuple[pd.Series, float]:
    days = (df.DocDueDate - df.DocDate).dt.days
    return days.groupby(df.CardCode).median(), float(days.median()) if len(days) else 30.0


def cash_forecast(today: pd.Timestamp, ar_open: pd.DataFrame, ap_stats: pd.DataFrame, ap_overall: float) -> dict:
    weeks = pd.date_range(today.to_period("W-SUN").end_time.normalize(), periods=WEEKS, freq="W-SUN")
    acct = _read("accounts")
    cash = float(acct[(acct.Finanse == "Y") & (acct.Postable == "Y")].CurrTotal.sum())

    def bucket(dates: pd.Series, amounts: pd.Series, ref: pd.Series | None = None) -> tuple[pd.Series, float]:
        """Sum amounts by forecast week; past dates up to OLD_DAYS late go in week 1, older ones are 'doubtful'.
        ref is the document's own date (order ship date) when that, not the payment date, decides staleness."""
        late = (today - (dates if ref is None else ref)).dt.days
        old = late > OLD_DAYS
        d = dates.where(dates >= today, today)
        wk = d.dt.to_period("W-SUN").dt.end_time.dt.normalize()
        return amounts[~old].groupby(wk[~old]).sum().reindex(weeks, fill_value=0), float(amounts[old].sum())

    ar_in, ar_doubtful = bucket(ar_open.expected_pay_date, ar_open.balance)

    inv = _read("ar_invoices", ("DocDate", "DocDueDate"))
    terms, terms_all = _terms(inv)
    so = _read("open_sales", ("NeedDate",))
    so = so[so.NeedDate.notna()]
    cust_late = ar_open.drop_duplicates("CardCode").set_index("CardCode").expected_days_late
    so_days = so.CardCode.map(terms).fillna(terms_all) + so.CardCode.map(cust_late).fillna(0)
    so_in, so_old = bucket(so.NeedDate.where(so.NeedDate >= today, today) + pd.to_timedelta(so_days.round(), unit="D"),
                           so.OpenValue, so.NeedDate)

    ap = _read("ap_invoices", ("DocDate", "DocDueDate"))
    ap_open = ap[(ap.DocStatus == "O") & (ap.DocTotal > ap.PaidToDate)]
    ap_late = ap_open.CardCode.map(ap_stats.expected_days_late).fillna(ap_overall)
    ap_out, ap_old = bucket(ap_open.DocDueDate + pd.to_timedelta(ap_late.round(), unit="D"), ap_open.DocTotal - ap_open.PaidToDate)
    sterms, sterms_all = _terms(ap)
    po = _read("open_purchases", ("DueDate",))
    po = po[po.DueDate.notna()]
    po_days = po.CardCode.map(sterms).fillna(sterms_all) + po.CardCode.map(ap_stats.expected_days_late).fillna(ap_overall)
    po_out, po_old = bucket(po.DueDate.where(po.DueDate >= today, today) + pd.to_timedelta(po_days.round(), unit="D"),
                            po.OpenValue, po.DueDate)

    w = pd.DataFrame({"week_ending": weeks, "in_customer_invoices": ar_in.values, "in_sales_orders": so_in.values,
                      "out_supplier_invoices": ap_out.values, "out_purchase_orders": po_out.values})
    w["net"] = w.in_customer_invoices + w.in_sales_orders - w.out_supplier_invoices - w.out_purchase_orders
    w["balance"] = cash + w.net.cumsum()
    low = w.loc[w.balance.idxmin()]
    return {
        "cash_today": round(cash, 2),
        "cash_in_13_weeks": round(float(w.balance.iloc[-1]), 2),
        "total_in_13_weeks": round(float(w.in_customer_invoices.sum() + w.in_sales_orders.sum()), 2),
        "total_out_13_weeks": round(float(w.out_supplier_invoices.sum() + w.out_purchase_orders.sum()), 2),
        "lowest_balance": round(float(low.balance), 2), "lowest_week_ending": str(low.week_ending.date()),
        "weeks_below_zero": int((w.balance < 0).sum()),
        "left_out_as_doubtful": {"customer_invoices_over_60_days_late": round(ar_doubtful, 2),
                                 "supplier_invoices_over_60_days_late": round(ap_old, 2),
                                 "sales_orders_over_60_days_past_ship_date": round(so_old, 2),
                                 "purchase_orders_over_60_days_past_due_date": round(po_old, 2)},
        "weekly": _records(w),
        "note": "cash today = B1 cash/bank accounts; sales and purchase order values exclude tax",
    }


def margins(today: pd.Timestamp) -> dict:
    ar = _read("ar_lines", ("DocDate",))
    ar = ar[ar.DocDate > today - pd.Timedelta(days=365)].copy()
    if ar.empty:
        return {"note": "No sales invoices in the last 12 months."}
    disc = ar.DocDisc.fillna(0) / 100
    ar["revenue"] = ar.LineTotal * (1 - disc)
    ar["gross_profit"] = ar.GrssProfit - ar.LineTotal * disc
    ar["recent"] = ar.DocDate > today - pd.Timedelta(days=90)
    slp = _read("salespeople").set_index("SlpCode").SlpName
    items = _read("items").set_index("ItemCode").ItemName
    ar["salesperson"] = ar.SlpCode.map(slp).fillna("none")
    ar["item"] = ar.ItemCode.map(items).fillna(ar.Dscription)
    total = ar.revenue.sum()

    def view(by: str) -> dict:
        g = ar.groupby(by).agg(revenue=("revenue", "sum"), gross_profit=("gross_profit", "sum"))
        g["margin_pct"] = g.gross_profit / g.revenue.where(g.revenue != 0) * 100
        r = ar[ar.recent].groupby(by).agg(rr=("revenue", "sum"), rg=("gross_profit", "sum"))
        b = ar[~ar.recent].groupby(by).agg(br=("revenue", "sum"), bg=("gross_profit", "sum"))
        g = g.join(r).join(b)
        g["margin_change_pts"] = (g.rg / g.rr.where(g.rr > 0) - g.bg / g.br.where(g.br > 0)) * 100
        g = g.reset_index()[[by, "revenue", "gross_profit", "margin_pct", "margin_change_pts"]]
        material = g[g.revenue >= total * 0.01]
        return {"top_profit": _records(g.nlargest(5, "gross_profit")),
                "lowest_margin": _records(material.nsmallest(5, "margin_pct")),
                "biggest_margin_drop": _records(material.dropna(subset=["margin_change_pts"]).nsmallest(5, "margin_change_pts"))}

    def pct(df):
        return round(float(df.gross_profit.sum() / df.revenue.sum() * 100), 1) if df.revenue.sum() else None
    return {
        "last_12_months": {"revenue": round(float(total), 2), "gross_profit": round(float(ar.gross_profit.sum()), 2),
                           "margin_pct": pct(ar)},
        "margin_pct_last_90_days": pct(ar[ar.recent]), "margin_pct_previous_275_days": pct(ar[~ar.recent]),
        "by_customer": view("CardName"), "by_item": view("item"), "by_salesperson": view("salesperson"),
        "note": "after document discounts; credit notes not deducted",
    }


def churn(today: pd.Timestamp) -> dict:
    inv = _read("ar_invoices", ("DocDate",))
    inv = inv[inv.DocDate > today - pd.Timedelta(days=3 * 365)].sort_values("DocDate")
    rows = []
    for code, g in inv.groupby("CardCode"):
        dates = g.DocDate.drop_duplicates()
        if (dates > dates.max() - pd.Timedelta(days=730)).sum() < 4:
            continue
        gap = dates.diff().dt.days.median()
        silent = (today - dates.max()).days
        if silent > max(3 * gap, 60):
            year = g[g.DocDate > dates.max() - pd.Timedelta(days=365)]
            rows.append({"CardCode": code, "customer": g.CardName.iloc[0], "last_invoice": dates.max(), "days_silent": silent,
                         "usual_days_between_orders": gap, "monthly_revenue_before": year.DocTotal.sum() / 12})
    df = pd.DataFrame(rows)
    if df.empty:
        return {"count": 0, "customers": []}
    df = df.sort_values("monthly_revenue_before", ascending=False)
    return {"count": len(df), "monthly_revenue_at_risk": round(float(df.monthly_revenue_before.sum()), 2),
            "customers": _records(df.head(TOP))}


def cross_sell(today: pd.Timestamp) -> dict:
    ar = _read("ar_lines", ("DocDate",))
    ar = ar[ar.DocDate > today - pd.Timedelta(days=730)]
    baskets = ar.groupby("DocEntry").ItemCode.agg(lambda s: sorted(set(s)))
    baskets = baskets[baskets.str.len().between(2, 30)]  # very large invoices say little about what goes together
    n_docs = ar.DocEntry.nunique()
    count = ar.drop_duplicates(["DocEntry", "ItemCode"]).ItemCode.value_counts()
    pairs = pd.Series([p for b in baskets for p in combinations(b, 2)]).value_counts()
    if pairs.empty:
        return {"pairs": []}
    df = pd.DataFrame({"item_a": [p[0] for p in pairs.index], "item_b": [p[1] for p in pairs.index], "invoices_together": pairs.values})
    df["lift"] = df.invoices_together * n_docs / (df.item_a.map(count) * df.item_b.map(count))
    df = df[(df.invoices_together >= 3) & (df.lift >= 1.5)].sort_values(["invoices_together", "lift"], ascending=False).head(TOP)
    recent = ar[ar.DocDate > today - pd.Timedelta(days=365)]
    buyers = recent.groupby("ItemCode").CardCode.agg(set)
    df["customers_buying_one_not_other"] = [len(buyers.get(a, set()) ^ buyers.get(b, set())) for a, b in zip(df.item_a, df.item_b)]
    names = _read("items").set_index("ItemCode").ItemName
    df["name_a"], df["name_b"] = df.item_a.map(names), df.item_b.map(names)
    return {"pairs": _records(df)}


def build() -> dict:
    today = pd.Timestamp.today().normalize()
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    lp, ar_open = late_payers(today)
    sp, ap_stats, ap_overall = supplier_payments(today)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": meta.get("database"), "data_synced_at": meta.get("synced_at"), "currency": meta.get("currency"),
        "cash_forecast": cash_forecast(today, ar_open, ap_stats, ap_overall),
        "late_payers": lp,
        "supplier_payments": sp,
        "margins": margins(today),
        "churn": churn(today),
        "cross_sell": cross_sell(today),
    }


def main() -> None:
    f = build()
    (DATA_DIR / "finance.json").write_text(json.dumps(f, indent=2))
    c, cur = f["cash_forecast"], f["currency"]
    print(f"Cash today {cur}{c['cash_today']:,.0f}, in 13 weeks {cur}{c['cash_in_13_weeks']:,.0f}, "
          f"lowest {cur}{c['lowest_balance']:,.0f} (week ending {c['lowest_week_ending']})")
    lp = f["late_payers"]["open_invoices_likely_late"]
    print(f"Open customer invoices likely late: {lp['count']} ({cur}{lp['value']:,.0f})")
    m = f["margins"].get("last_12_months")
    if m:
        print(f"Margin last 12 months {m['margin_pct']}%, last 90 days {f['margins']['margin_pct_last_90_days']}%")
    print(f"Customers gone quiet: {f['churn']['count']}; cross-sell pairs: {len(f['cross_sell']['pairs'])}")


if __name__ == "__main__":
    main()
