"""Sales and demand forecast from B1's invoice history (read-only), for the company, one customer or one item.

Method (said in the answer, so nobody mistakes it for a promise): monthly net sales of up to the last 36 months, Holt-Winters
exponential smoothing with a damped trend, and yearly seasonality when there are 24+ months of history; with less history,
the recent months' weighted average. The range is the typical error of the method on the past months.
Without an item, the demand part lists the items whose forecast use over the next months is more than their stock
plus what is already on order.
"""

import datetime as dt
import warnings

import numpy as np
import pandas as pd

from .analytics import _cur, _excel, _hchart, _md, _months_ago, _result, _sales_lines
from .charts import BLUE, OTHER, RED, _figure, _image
from .db import query
from .numbers import money, qty


def _model(series: pd.Series, ahead: int) -> tuple[np.ndarray, float, str]:
    """Forecast `ahead` months; returns (values, typical monthly error, method in words)."""
    y = series.astype(float).values
    n = len(y)
    if n >= 12 and np.count_nonzero(y) >= 6:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        seasonal = 12 if n >= 24 else None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ExponentialSmoothing(y, trend="add", damped_trend=True, seasonal="add" if seasonal else None,
                                       seasonal_periods=seasonal, initialization_method="estimated").fit()
        f = fit.forecast(ahead)
        err = float(np.sqrt(np.mean(fit.resid[-12:] ** 2)))
        how = "Holt-Winters exponential smoothing (damped trend" + (", yearly seasonality)" if seasonal else ")")
    else:
        w = np.arange(1, min(n, 6) + 1, dtype=float)
        level = float(np.dot(y[-len(w):], w) / w.sum()) if n else 0.0
        f = np.full(ahead, level)
        err = float(np.std(y[-6:])) if n > 1 else 0.0
        how = "weighted average of the last months (too little history for trend or seasonality)"
    return np.clip(f, 0, None), err, how


def sales_forecast(months_ahead: int = 3, customer: str = "", item: str = "") -> dict:
    """Net sales forecast for the next months (company, one customer or one item), plus items likely to run short."""
    ahead = max(1, min(int(months_ahead or 3), 12))
    t = dt.date.today()
    this_month = t.replace(day=1)
    start = _months_ago(this_month, 36)
    cur = _cur()
    s = _sales_lines(start, this_month - dt.timedelta(days=1))  # complete months only
    what = "the company"
    if customer:
        hit = s[(s.CardCode.str.lower() == customer.lower()) | s.CardName.str.lower().str.contains(customer.lower(), regex=False)]
        if hit.empty:
            return {"error": f"No sales to a customer matching '{customer}' in the last 36 months."}
        s, what = hit, hit.CardName.iloc[0]
    if item:
        hit = s[(s.ItemCode.str.lower() == item.lower()) | s.ItemName.fillna("").str.lower().str.contains(item.lower(), regex=False)]
        if hit.empty:
            return {"error": f"No sales of an item matching '{item}' in the last 36 months."}
        s, what = hit, (f"{what}, " if customer else "") + hit.ItemName.iloc[0]
    if s.empty:
        return {"note": "No sales history to forecast from. Say so plainly."}
    idx = pd.period_range(s.DocDate.min().to_period("M"), (this_month - dt.timedelta(days=1)), freq="M")
    hist = s.groupby(s.DocDate.dt.to_period("M")).Net.sum().reindex(idx, fill_value=0.0)
    f, err, how = _model(hist, ahead)
    fidx = pd.period_range(this_month, periods=ahead, freq="M")
    qty_hist = s.groupby(s.DocDate.dt.to_period("M")).Qty.sum().reindex(idx, fill_value=0.0) if item else None

    shown_hist = hist.iloc[-12:]
    fig, ax = _figure(3.7, f"Net sales: last {len(shown_hist)} months and forecast", f"{cur} · {what} · lighter bars = forecast, line = likely range" + (" · figures in the table" if ahead > 3 else ""))
    xs = list(range(len(shown_hist) + ahead))
    ax.bar(xs[:len(shown_hist)], shown_hist.values, color=BLUE, width=0.62)
    ax.bar(xs[len(shown_hist):], f, color="#9cc6fb", width=0.62)
    ax.errorbar(xs[len(shown_hist):], f, yerr=err, fmt="none", ecolor="#556b82", capsize=4, linewidth=1)
    for i, (x, v) in enumerate(zip(xs[len(shown_hist):], f) if ahead <= 3 else []):  # more months: the figures are in the table
        ax.annotate(money(v, cur), (x, v + err), xytext=(0, 4 + 12 * (i % 2)), textcoords="offset points", ha="center",
                    fontsize=8 if ahead > 4 else 9, color="#1d2d3e")
    months_all = list(shown_hist.index) + list(fidx)
    labels = [f"{p:%b}\n{p:%Y}" if i == 0 or p.month == 1 else f"{p:%b}" for i, p in enumerate(months_all)]
    ax.set_xticks(xs, labels)
    ax.tick_params(axis="x", colors="#556b82", labelsize=8)
    ax.yaxis.set_visible(False)
    top = max(list(shown_hist.values) + list(f + err) + [1])
    ax.set_ylim(0, top * 1.3)
    chart = _image(fig, "Sales forecast")

    out = [f"## Sales forecast: {what}, next {ahead} month{'s' if ahead > 1 else ''}",
           f"{cur}, excluding tax · method: {how}, from {len(hist)} months of history. A forecast is an estimate, not a promise.", "",
           "| Month | Forecast | Likely range | Same month last year |", "|---|---:|---:|---:|"]
    for p, v in zip(fidx, f):
        ly = hist.get(p - 12)
        out.append(f"| {p:%b %Y} | {money(v, cur)} | {money(max(v - err, 0), cur)} to {money(v + err, cur)} | "
                   f"{money(float(ly), cur) if ly is not None else '-'} |")
    out += [f"| **Total** | **{money(float(f.sum()), cur)}** | | |", ""]
    recent, year = float(hist.iloc[-3:].mean()), float(hist.iloc[-15:-3].mean()) if len(hist) >= 15 else None
    if year and recent < 0.4 * year:
        out += [f"⚠️ Sales of the last 3 months ({money(recent, cur)} a month) are far below the 12 months before "
                f"({money(year, cur)} a month), so the forecast is low. If that drop is temporary, expect more.", ""]
    elif year and recent > 1.6 * year:
        out += [f"ℹ️ Sales of the last 3 months ({money(recent, cur)} a month) are well above the 12 months before "
                f"({money(year, cur)} a month), so the forecast follows that rise.", ""]
    out.append(chart + "\n")

    # Demand: items whose expected use is more than stock + on order (company-wide or for the chosen item)
    short = pd.DataFrame()
    try:
        lines = _sales_lines(_months_ago(this_month, 12), this_month - dt.timedelta(days=1))
        if item:
            lines = lines[lines.ItemCode.isin(s.ItemCode.unique())]
        use = lines.groupby(["ItemCode", "ItemName"]).Qty.sum().reset_index()
        use = use[use.Qty > 0]
        if len(use):
            st = query("SELECT ItemCode, SUM(OnHand) AS OnHand, SUM(IsCommited) AS Committed, SUM(OnOrder) AS OnOrder FROM OITW "
                       "GROUP BY ItemCode").set_index("ItemCode")
            use = use.join(st, on="ItemCode")
            for c in ("OnHand", "Committed", "OnOrder"):
                use[c] = use[c].astype(float).fillna(0)
            use["Expected use"] = (use.Qty / 12 * ahead).round(0)
            use["Available"] = use.OnHand - use.Committed + use.OnOrder
            use["Short by"] = (use["Expected use"] - use.Available).clip(lower=0)
            short = use[use["Short by"] > 0].sort_values("Short by", ascending=False)
    except Exception:
        pass
    if len(short):
        out += [f"### Items likely to run short in the next {ahead} month{'s' if ahead > 1 else ''}",
                "Expected use = average monthly sales of the last 12 months; available = on hand - promised to customers + on order."]
        out += _md(short, {"ItemName": "Item", "Expected use": "Expected use", "OnHand": "On hand", "Committed": "Promised",
                           "OnOrder": "On order", "Short by": "Short by"})
    elif item:
        out.append("Stock plus what is on order covers the expected use of this item.")
    sheets = {"Forecast": pd.DataFrame({"Month": [f"{p:%b %Y}" for p in fidx], "Forecast": f.round(2),
                                        "Low": np.clip(f - err, 0, None).round(2), "High": (f + err).round(2)}),
              "History": pd.DataFrame({"Month": [f"{p:%b %Y}" for p in hist.index], "Net sales": hist.values.round(2)})}
    if len(short):
        sheets["Likely to run short"] = short.rename(columns={"ItemCode": "Item code", "ItemName": "Item", "Qty": "Sold in 12 months"})
    link = _excel(f"Sales_forecast_{t:%Y%m%d}", sheets, ("Forecast", "Low", "High", "Net sales"), f"Sales forecast: {what}",
                  f"Next {ahead} months · {how}",
                  [{"label": f"Forecast next {ahead} months ({cur})", "value": round(float(f.sum()), 2), "note": "estimate"}],
                  [{"sheet": "History", "category": "Month", "value": "Net sales", "wide": True, "title": "Net sales history", "subtitle": cur}])
    out[1:1] = ["", link]
    res = _result(out, {"for": what, "method": how, "months_of_history": len(hist),
                        "forecast": [{"month": f"{p:%Y-%m}", "net_sales": round(float(v), 2), "low": round(max(float(v) - err, 0), 2),
                                      "high": round(float(v) + err, 2)} for p, v in zip(fidx, f)],
                        "forecast_total": round(float(f.sum()), 2),
                        "likely_short": [{"item": r.ItemName, "short_by": float(r["Short by"])} for _, r in short.head(5).iterrows()]},
                  "forecast")
    res["instruction"] += " Say clearly it is an estimate, with the range."
    return res
