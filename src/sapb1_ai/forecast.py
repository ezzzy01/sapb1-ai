"""Weekly demand forecast, time-phased shortage check and reorder suggestions per item and warehouse.

Reads the Parquet files written by sync.py and writes:
  forecast.parquet       12-week forecast per item/warehouse
  reorder_report.csv     stock position, run-out date, reorder point, suggested order qty and action

Method:
  - Demand = invoices minus credit notes plus issues for production, bucketed by week,
    so raw materials and components are forecast from what production actually consumes.
  - Each series is classified by ADI / CV² (Syntetos-Boylan): smooth and erratic
    series use AutoETS, intermittent and lumpy series use Croston SBA.
  - Service level comes from ABC class on the last 52 weeks of value; critical items get 99%.
  - Safety stock = z * sigma(weekly demand) * sqrt(lead time + review period).
  - Time-phased projection from today: on hand + open POs + released production output
    - max(forecast, dated open sales orders + production component needs) per week.
    The first week it goes negative is the run-out date; if that is before a normal order
    could arrive (lead time) the item needs expediting.

Critical items (status LINE STOP RISK when they will run out):
  - components still to be issued to planned/released production orders,
  - components in the production BOM of an item that is being sold or produced,
  - anything listed in config/critical_items.csv (any industry: packaging, spare parts, ingredients...).
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoETS, CrostonSBA

from .sync import DATA_DIR

HORIZON_WEEKS = 12
HISTORY_WEEKS = 104
PROJECTION_WEEKS = 26
REVIEW_WEEKS = float(os.getenv("REVIEW_WEEKS", "1"))
DEFAULT_LEAD_DAYS = float(os.getenv("DEFAULT_LEAD_DAYS", "14"))
SERVICE_Z = {"A": 2.05, "B": 1.65, "C": 1.28}  # 98%, 95%, 90%
CRITICAL_Z = 2.33  # 99%
CRITICAL_FILE = Path(__file__).resolve().parents[2] / "config" / "critical_items.csv"

STATUS_ORDER = {"LINE STOP RISK": 0, "STOCKOUT RISK": 1, "REORDER": 2, "OVERSTOCK": 3, "DEAD STOCK": 4, "OK": 5}


def _load(name: str, columns: list[str]) -> pd.DataFrame:
    """Optional sync outputs; empty if this database has none (e.g. no production)."""
    path = DATA_DIR / f"{name}.parquet"
    df = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=columns)
    for col in ("Qty", "LineTotal", "Quantity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col]).astype(float)
    return df


def week_end(dates) -> pd.Series:
    return pd.to_datetime(pd.Series(dates)).dt.to_period("W-SUN").dt.end_time.dt.normalize()


def weekly_demand(demand: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    s = demand.assign(ds=week_end(demand.DocDate).values)
    s = s[s.ds <= as_of]
    s["unique_id"] = s.ItemCode + "|" + s.WhsCode
    wk = s.groupby(["unique_id", "ds"], as_index=False).Qty.sum()
    wk["y"] = wk.Qty.clip(lower=0)  # a week of net returns counts as zero demand

    start = as_of - pd.Timedelta(weeks=HISTORY_WEEKS - 1)
    weeks = pd.date_range(start, as_of, freq="W-SUN")
    full = pd.MultiIndex.from_product([wk.unique_id.unique(), weeks], names=["unique_id", "ds"])
    return wk.set_index(["unique_id", "ds"])[["y"]].reindex(full, fill_value=0.0).reset_index()


def classify(y: np.ndarray) -> str:
    nz = y[y > 0]
    if len(nz) == 0:
        return "none"
    adi = len(y) / len(nz)
    cv2 = (nz.std() / nz.mean()) ** 2 if len(nz) > 1 else 0.0
    if adi < 1.32:
        return "smooth" if cv2 < 0.49 else "erratic"
    return "intermittent" if cv2 < 0.49 else "lumpy"


def abc_classes(demand: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    recent = demand[pd.to_datetime(demand.DocDate) > as_of - pd.Timedelta(weeks=52)]
    rev = recent.groupby("ItemCode").LineTotal.sum().clip(lower=0).sort_values(ascending=False)
    share = rev.cumsum() / rev.sum() if rev.sum() > 0 else rev * 0
    return pd.cut(share, [-np.inf, 0.80, 0.95, np.inf], labels=["A", "B", "C"]).astype(str)


def lead_time_days(lead: pd.DataFrame, items: pd.DataFrame) -> pd.Series:
    observed = lead[lead.LeadDays >= 0].groupby("ItemCode").LeadDays.agg(["median", "count"])
    observed = observed[observed["count"] >= 2]["median"]
    master = items.set_index("ItemCode").LeadTime.where(lambda x: x > 0)
    return observed.combine_first(master).reindex(items.ItemCode).fillna(DEFAULT_LEAD_DAYS)


def critical_items(sales: pd.DataFrame, needs: pd.DataFrame, supply: pd.DataFrame, boms: pd.DataFrame,
                   as_of: pd.Timestamp) -> pd.Series:
    """ItemCode -> reason it is critical."""
    reasons: dict[str, list[str]] = {}

    for code, grp in needs.groupby("ItemCode"):
        reasons.setdefault(code, []).append(f"needed by {grp.DocNum.nunique()} open production order(s)")

    sold = set(sales[pd.to_datetime(sales.DocDate) > as_of - pd.Timedelta(weeks=52)].ItemCode)
    active_parents = sold | set(supply.ItemCode)
    prod_boms = boms[(boms.TreeType == "P") & boms.ParentItem.isin(active_parents)]
    for code, grp in prod_boms.groupby("ItemCode"):
        parents = sorted(grp.ParentItem.unique())
        reasons.setdefault(code, []).append("BOM component of " + ", ".join(parents[:5]) + (" ..." if len(parents) > 5 else ""))

    if CRITICAL_FILE.exists():
        manual = pd.read_csv(CRITICAL_FILE, dtype=str, comment="#").fillna("")
        for code, why in zip(manual.ItemCode.str.strip(), manual.get("Reason", pd.Series([""] * len(manual)))):
            if code:
                reasons.setdefault(code, []).append(f"marked critical{': ' + why if why else ''}")

    return pd.Series({k: "; ".join(v) for k, v in reasons.items()}, dtype=str)


def project(row, today: pd.Timestamp, weeks: pd.DatetimeIndex, demand_ev: dict, supply_ev: dict, needs_by_uid: dict):
    """Week-by-week projected stock; returns run-out week, shortfall, production orders at risk,
    stock when a new order would arrive, and the weekly projection (stock, supply, demand) for charts."""
    uid = row.unique_id
    dated_demand = demand_ev.get(uid, pd.Series(dtype=float)).reindex(weeks, fill_value=0.0).to_numpy()
    supply = supply_ev.get(uid, pd.Series(dtype=float)).reindex(weeks, fill_value=0.0).to_numpy()
    demand = np.maximum(dated_demand, row.fc_weekly)  # forecast consumed by known orders
    projected = row.OnHand + np.cumsum(supply - demand)

    # Stock left at the point a new order placed today could arrive (lead time + review period).
    arrival = min(int(np.ceil(row.lead_days / 7 + REVIEW_WEEKS)), len(weeks) - 1)
    at_arrival = float(projected[arrival])
    neg = np.nonzero(projected < 0)[0]
    if len(neg) == 0:
        return pd.NaT, 0.0, "", at_arrival, (projected, supply, demand)
    first = neg[0]
    runout = today if first == 0 else weeks[first]
    # Shortage before a new order can arrive: only expediting or transfers can fix this part.
    shortfall = float(max(0.0, -projected[: arrival + 1].min()))

    blocked = ""
    needs = needs_by_uid.get(uid)
    if needs is not None:
        at_risk = needs[needs.week >= weeks[first]] if first > 0 else needs
        if not at_risk.empty:
            blocked = ", ".join(f"WO {d} ({p})" for d, p in at_risk[["DocNum", "ParentItem"]].drop_duplicates().head(8).itertuples(index=False))
    return runout, shortfall, blocked, at_arrival, (projected, supply, demand)


def round_order(qty: float, min_qty: float, multiple: float) -> float:
    if qty <= 0:
        return 0.0
    qty = max(qty, min_qty or 0)
    if multiple and multiple > 0:
        qty = np.ceil(qty / multiple) * multiple
    return float(np.ceil(qty))


def main() -> None:
    items = pd.read_parquet(DATA_DIR / "items.parquet")
    stock = pd.read_parquet(DATA_DIR / "stock.parquet")
    sales = pd.read_parquet(DATA_DIR / "sales.parquet")
    lead = pd.read_parquet(DATA_DIR / "lead_times.parquet")
    issues = _load("production_issues", ["DocDate", "ItemCode", "WhsCode", "Qty", "LineTotal", "Src"])
    needs = _load("production_needs", ["DocNum", "Status", "ParentItem", "ItemCode", "WhsCode", "Qty", "NeedDate", "DueDate"])
    psupply = _load("production_supply", ["DocNum", "Status", "ItemCode", "WhsCode", "Qty", "DueDate"])
    open_so = _load("open_sales", ["DocNum", "CardCode", "ItemCode", "WhsCode", "Qty", "NeedDate"])
    open_po = _load("open_purchases", ["DocNum", "CardCode", "ItemCode", "WhsCode", "Qty", "DueDate"])
    boms = _load("boms", ["ParentItem", "ItemCode", "Quantity", "TreeType"])
    items = items[items.frozenFor != "Y"]

    demand_hist = pd.concat([sales, issues], ignore_index=True)
    # Demo databases end years ago, so default the forecast date to the last recorded demand.
    as_of = pd.Timestamp(os.getenv("AS_OF") or pd.to_datetime(demand_hist.DocDate).max())
    as_of = as_of.to_period("W-SUN").end_time.normalize()
    # The shortage projection always starts today: open documents are current.
    today = pd.Timestamp.today().normalize()
    print(f"Forecast as of week ending {as_of:%Y-%m-%d}; shortage projection from {today:%Y-%m-%d}")

    wk = weekly_demand(demand_hist, as_of)
    last52 = wk[wk.ds > as_of - pd.Timedelta(weeks=52)]
    stats = last52.groupby("unique_id").y.agg(mean52="mean", std52="std", weeks_with_sales=lambda y: (y > 0).sum())
    stats["pattern"] = last52.groupby("unique_id").y.apply(lambda y: classify(y.to_numpy()))
    last_sale = wk[wk.y > 0].groupby("unique_id").ds.max()

    active = stats.index[stats.pattern != "none"]
    sf = StatsForecast(models=[AutoETS(season_length=1), CrostonSBA()], freq="W-SUN", n_jobs=-1)
    fc = sf.forecast(df=wk[wk.unique_id.isin(active)], h=HORIZON_WEEKS)
    fc = fc.reset_index() if "unique_id" not in fc.columns else fc
    fc["pattern"] = fc.unique_id.map(stats.pattern)
    fc["yhat"] = np.where(fc.pattern.isin(["smooth", "erratic"]), fc.AutoETS, fc.CrostonSBA).clip(min=0)
    fc[["unique_id", "ds", "pattern", "yhat"]].to_parquet(DATA_DIR / "forecast.parquet", index=False)

    weekly_fc = fc.groupby("unique_id").yhat.mean().rename("fc_weekly")

    rep = stock.merge(items, on="ItemCode")
    rep["unique_id"] = rep.ItemCode + "|" + rep.WhsCode
    rep = rep.join(stats, on="unique_id").join(weekly_fc, on="unique_id").join(last_sale.rename("last_sale"), on="unique_id")
    rep = rep[(rep.OnHand != 0) | (rep.OnOrder != 0) | (rep.IsCommited != 0) | rep.fc_weekly.notna()]

    crit = critical_items(sales, needs, psupply[psupply.Status == "R"], boms, as_of)
    rep["critical_reason"] = rep.ItemCode.map(crit).fillna("")
    rep["critical"] = rep.critical_reason != ""
    rep["abc"] = rep.ItemCode.map(abc_classes(demand_hist, as_of)).fillna("C")
    rep["lead_days"] = rep.ItemCode.map(lead_time_days(lead, items))
    rep["fc_weekly"] = rep.fc_weekly.fillna(0)
    rep["std52"] = rep.std52.fillna(0)

    lt_w = rep.lead_days / 7
    z = np.where(rep.critical, CRITICAL_Z, rep.abc.map(SERVICE_Z))
    rep["safety_stock"] = np.ceil(z * rep.std52 * np.sqrt(lt_w + REVIEW_WEEKS))
    rep["reorder_point"] = np.ceil(rep.fc_weekly * lt_w + rep.safety_stock)
    rep["order_up_to"] = np.ceil(rep.fc_weekly * (lt_w + REVIEW_WEEKS) + rep.safety_stock)
    rep["available"] = rep.OnHand - rep.IsCommited
    rep["position"] = rep.available + rep.OnOrder
    rep["weeks_cover"] = np.where(rep.fc_weekly > 0, rep.available / rep.fc_weekly, np.inf).round(1)

    # ---- Time-phased projection from dated open documents ----
    weeks = pd.date_range(today.to_period("W-SUN").end_time.normalize(), periods=PROJECTION_WEEKS, freq="W-SUN")

    def bucket(df: pd.DataFrame, date_col: str, overdue_week: int = 0) -> dict:
        if df.empty:
            return {}
        dates = pd.to_datetime(df[date_col])
        w = week_end(dates).clip(lower=weeks[0])
        w = w.where(dates.values >= today, weeks[overdue_week]).values  # overdue: demand due now, supply not before next week
        g = df.assign(uid=df.ItemCode + "|" + df.WhsCode, week=w).groupby(["uid", "week"]).Qty.sum()
        return {uid: s.droplevel(0) for uid, s in g.groupby(level=0)}

    demand_ev = bucket(pd.concat([open_so[["ItemCode", "WhsCode", "Qty", "NeedDate"]],
                                  needs[["ItemCode", "WhsCode", "Qty", "NeedDate"]]], ignore_index=True), "NeedDate")
    # Only released production orders count as incoming supply; planned ones may never be released.
    supply_ev = bucket(pd.concat([open_po[["ItemCode", "WhsCode", "Qty", "DueDate"]],
                                  psupply[psupply.Status == "R"][["ItemCode", "WhsCode", "Qty", "DueDate"]]], ignore_index=True),
                       "DueDate", overdue_week=1)
    needs_by_uid = {}
    if not needs.empty:
        n = needs.assign(uid=needs.ItemCode + "|" + needs.WhsCode, week=week_end(needs.NeedDate).clip(lower=weeks[0]).values)
        needs_by_uid = {uid: g for uid, g in n.groupby("uid")}

    proj = [project(r, today, weeks, demand_ev, supply_ev, needs_by_uid) for r in rep.itertuples()]
    rep["runout_date"] = [p[0] for p in proj]
    rep["shortfall_in_lead_time"] = np.ceil([p[1] for p in proj])
    rep["production_orders_at_risk"] = [p[2] for p in proj]
    rep["stock_at_arrival"] = np.round([p[3] for p in proj], 1)
    pd.DataFrame([
        {"ItemCode": r.ItemCode, "WhsCode": r.WhsCode, "week": w, "projected": round(float(s), 2),
         "supply": round(float(sup), 2), "demand": round(float(d), 2)}
        for r, p in zip(rep.itertuples(), proj) for w, s, sup, d in zip(weeks, *p[4])
    ]).to_parquet(DATA_DIR / "projection.parquet", index=False)
    rep["days_to_runout"] = (rep.runout_date - today).dt.days
    lt_deadline = today + pd.to_timedelta(rep.lead_days, unit="D")
    runs_out_in_lt = rep.runout_date.notna() & (rep.runout_date <= lt_deadline)

    # Order what is missing when a new order would arrive: safety stock plus one review period of demand,
    # minus the projected stock at that point (which already counts open POs and known orders).
    order_qty = rep.safety_stock + rep.fc_weekly * REVIEW_WEEKS - rep.stock_at_arrival
    need = order_qty > 0
    rep["suggested_qty"] = [
        round_order(q, m, mult) if n else 0.0
        for q, m, mult, n in zip(order_qty, rep.MinOrdrQty, rep.OrdrMulti, need)
    ]
    # Moving-average cost can be 0 (e.g. standard costing); fall back to last purchase price.
    rep["unit_cost"] = rep.AvgPrice.where(rep.AvgPrice > 0, rep.LastPurPrc.where(rep.LastPurPrc > 0))
    rep["suggested_value"] = (rep.suggested_qty * rep.unit_cost).round(2)

    # Other warehouses with spare stock of the same item: the fastest fix for a shortage.
    spare = rep.assign(spare=rep.available - rep.safety_stock)
    spare = spare[spare.spare > 0]
    spare_by_item = {code: g for code, g in spare.groupby("ItemCode")}
    rep["transfer_from"] = [
        ", ".join(f"{w} ({s:.0f} spare)" for w, s in spare_by_item[c][spare_by_item[c].WhsCode != wh][["WhsCode", "spare"]].itertuples(index=False))
        if c in spare_by_item else ""
        for c, wh in zip(rep.ItemCode, rep.WhsCode)
    ]

    no_recent = rep.last_sale.isna() | (rep.last_sale < as_of - pd.Timedelta(weeks=26))
    rep["status"] = np.select(
        [
            rep.critical & runs_out_in_lt,  # will stop production / operations
            runs_out_in_lt,  # runs out before a new order could arrive
            need,  # stock when a new order would arrive is below safety stock
            no_recent & (rep.OnHand > 0) & (rep.IsCommited <= 0) & ~rep.critical,
            rep.weeks_cover > 26,
        ],
        ["LINE STOP RISK", "STOCKOUT RISK", "REORDER", "DEAD STOCK", "OVERSTOCK"],
        default="OK",
    )

    def action(r) -> str:
        if r.status in ("LINE STOP RISK", "STOCKOUT RISK"):
            when = "now" if r.days_to_runout <= 0 else f"on {r.runout_date:%Y-%m-%d}"
            parts = [f"EXPEDITE: runs out {when}, {r.shortfall_in_lead_time:.0f} short before a new order "
                     f"({r.lead_days:.0f} d lead time) can arrive"]
            parts.append(f"transfer from whs {r.transfer_from}" if r.transfer_from else "chase open POs / find another supplier")
            if r.suggested_qty > 0:
                parts.append(f"also order {r.suggested_qty:.0f}")
            if r.production_orders_at_risk:
                parts.append(f"blocks {r.production_orders_at_risk}")
            return "; ".join(parts)
        if r.status == "REORDER":
            return f"Order {r.suggested_qty:.0f} this week" + (f"; runs out {r.runout_date:%Y-%m-%d} otherwise" if pd.notna(r.runout_date) else "")
        if r.status == "DEAD STOCK":
            return "No demand for 26+ weeks: review for sale, transfer or write-off"
        if r.status == "OVERSTOCK":
            return "Hold purchases"
        return ""

    rep["action"] = [action(r) for r in rep.itertuples()]

    cols = [
        "status", "critical", "ItemCode", "ItemName", "WhsCode", "ItmsGrpNam", "abc", "pattern", "PrefVendor",
        "OnHand", "IsCommited", "OnOrder", "available", "position", "fc_weekly", "weeks_cover", "lead_days",
        "runout_date", "days_to_runout", "shortfall_in_lead_time", "stock_at_arrival", "transfer_from", "production_orders_at_risk",
        "safety_stock", "reorder_point", "order_up_to", "suggested_qty", "unit_cost", "suggested_value",
        "last_sale", "critical_reason", "action",
    ]
    rep = rep.sort_values(["status", "days_to_runout", "suggested_value"],
                          key=lambda c: c.map(STATUS_ORDER) if c.name == "status" else (c if c.name == "days_to_runout" else -c),
                          na_position="last")[cols]
    rep["fc_weekly"] = rep.fc_weekly.round(2)
    rep.to_csv(DATA_DIR / "reorder_report.csv", index=False)

    print(f"\n{rep.status.value_counts().to_string()}\n")
    print(f"Critical items: {rep.critical.sum()}  (production: {len(needs.ItemCode.unique())} items in open production orders, "
          f"{len(boms)} BOM lines, manual list: {'yes' if CRITICAL_FILE.exists() else 'no'})")
    print("Most urgent:")
    top = rep[rep.status.isin(["LINE STOP RISK", "STOCKOUT RISK", "REORDER"])].head(15)
    print(top[["status", "ItemCode", "WhsCode", "critical", "available", "runout_date", "lead_days", "suggested_qty"]].to_string(index=False))
    print(f"\nTotal suggested order value: {rep.suggested_value.sum():,.2f}")
    print(f"Report: {DATA_DIR / 'reorder_report.csv'}")


if __name__ == "__main__":
    main()
