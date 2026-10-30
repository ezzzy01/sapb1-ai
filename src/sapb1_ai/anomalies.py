"""Nightly transaction checks: unusual purchase/sales prices, sales below cost and duplicate supplier invoices.

Reads the Parquet files written by sync.py and writes:
  anomalies.json   counts, money totals and the flagged documents, biggest money impact first (read by the chat assistant)
  anomalies.csv    the same list as a table

Checks (all amounts in local currency, after line and document discounts):
  - PRICE AP / PRICE AR: unit price per inventory unit compared with the item's median price over the last two years.
    Flagged when it is at least PRICE_DEV_PCT away and a robust z-score (median/MAD) of at least 3.
    Impact: AP = paid above the usual price, AR = revenue below the usual price (negative if above).
  - BELOW COST: A/R invoice line sold for less than its cost (B1 gross profit after document discount is negative).
  - DUPLICATE AP: two A/P invoices from the same supplier with the same supplier reference (NumAtCard),
    or with the same total within DUPLICATE_DAYS of each other. Impact: the total of the later invoice.
Only documents dated in the last ANOMALY_DAYS are flagged; history is used as the baseline.
"""

import json
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd

from .sync import DATA_DIR

ANOMALY_DAYS = int(os.getenv("ANOMALY_DAYS", "90"))
BASELINE_DAYS = 730
PRICE_DEV_PCT = float(os.getenv("PRICE_DEV_PCT", "25"))
MIN_PRICE_HISTORY = 5
DUPLICATE_DAYS = 14
COLS = ["type", "severity", "doc", "DocDate", "partner", "ItemCode", "ItemName", "qty", "unit_price", "usual_price",
        "deviation_pct", "impact", "paid", "reason"]


def _lines(name: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{name}.parquet")
    df["DocDate"] = pd.to_datetime(df.DocDate)
    df = df[df.InvQty > 0].copy()
    df["net"] = df.LineTotal * (1 - df.DocDisc.fillna(0) / 100)
    df["unit_price"] = df.net / df.InvQty
    return df


def price_deviations(df: pd.DataFrame, kind: str, since: pd.Timestamp, today: pd.Timestamp) -> pd.DataFrame:
    base = df[df.DocDate >= today - pd.Timedelta(days=BASELINE_DAYS)]
    stats = base.groupby("ItemCode").unit_price.agg(usual_price="median", n="size")
    stats["mad"] = base.groupby("ItemCode").unit_price.apply(lambda p: (p - p.median()).abs().median()) * 1.4826
    recent = df[df.DocDate >= since].join(stats, on="ItemCode")
    recent = recent[(recent.n >= MIN_PRICE_HISTORY) & (recent.usual_price > 0)]
    recent["deviation_pct"] = (recent.unit_price / recent.usual_price - 1) * 100
    z = (recent.unit_price - recent.usual_price).abs() / recent.mad.replace(0, np.nan)
    hit = recent[(recent.deviation_pct.abs() >= PRICE_DEV_PCT) & (z.isna() | (z >= 3))].copy()
    sign = 1 if kind == "AP" else -1  # AP: paying more costs money; AR: charging less costs money
    hit["impact"] = sign * (hit.unit_price - hit.usual_price) * hit.InvQty
    hit["type"] = f"PRICE {kind}"
    hit["severity"] = np.where(hit.deviation_pct.abs() >= 50, "high", "medium")
    hit["doc"] = ("A/P invoice " if kind == "AP" else "A/R invoice ") + hit.DocNum.astype(str)
    word = np.where(hit.deviation_pct > 0, "above", "below")
    hit["reason"] = [f"{abs(d):.0f}% {w} the usual price ({p:,.2f} vs {u:,.2f} per unit, median of {n} invoice lines)"
                     for d, w, p, u, n in zip(hit.deviation_pct, word, hit.unit_price, hit.usual_price, hit.n)]
    return hit


def below_cost(ar: pd.DataFrame, since: pd.Timestamp) -> pd.DataFrame:
    df = ar[ar.DocDate >= since].copy()
    df["cost"] = df.LineTotal - df.GrssProfit
    df["impact"] = df.cost - df.net
    hit = df[(df.cost > 0) & (df.impact > 0.005)].copy()
    hit["type"] = "BELOW COST"
    hit["severity"] = np.where(hit.impact / hit.cost >= 0.2, "high", "medium")
    hit["doc"] = "A/R invoice " + hit.DocNum.astype(str)
    hit["usual_price"] = hit.cost / hit.InvQty
    hit["deviation_pct"] = (hit.unit_price / hit.usual_price - 1) * 100
    disc = hit.LineDisc.fillna(0) + hit.DocDisc.fillna(0)
    hit["reason"] = [f"sold at {p:,.2f} per unit, cost {c:,.2f}" + (f" ({d:.0f}% discount given)" if d else "")
                     for p, c, d in zip(hit.unit_price, hit.usual_price, disc)]
    return hit


def _ref(text) -> str:
    """Supplier reference without spaces, punctuation or leading zeros in numbers: 'INV-0042' and 'inv 42' match."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", re.sub(r"[^0-9A-Z]", "", str(text or "").upper()))


def duplicate_ap(since: pd.Timestamp) -> pd.DataFrame:
    inv = pd.read_parquet(DATA_DIR / "ap_invoices.parquet")
    inv["DocDate"] = pd.to_datetime(inv.DocDate)
    inv = inv[inv.DocTotal > 0].copy()
    inv["ref"] = inv.NumAtCard.map(_ref)
    inv["amount"] = inv.DocTotal.round(2)
    keep = ["DocEntry", "DocNum", "DocDate", "CardCode", "CardName", "NumAtCard", "ref", "amount", "DocTotal", "PaidToDate"]
    by_ref = inv[inv.ref != ""][keep].merge(inv[inv.ref != ""][keep], on=["CardCode", "ref"], suffixes=("_a", "_b"))
    by_amt = inv[keep].merge(inv[keep], on=["CardCode", "amount"], suffixes=("_a", "_b"))
    by_amt = by_amt[(by_amt.DocDate_b - by_amt.DocDate_a).dt.days.abs() <= DUPLICATE_DAYS]
    by_ref["match"], by_amt["match"] = "ref", "amount"
    pairs = pd.concat([by_ref, by_amt], ignore_index=True)
    pairs = pairs[(pairs.DocEntry_a < pairs.DocEntry_b) & (pairs.DocDate_b >= since)]
    pairs = pairs.drop_duplicates(["DocEntry_a", "DocEntry_b"])  # same ref listed first, it is the stronger match
    if pairs.empty:
        return pd.DataFrame(columns=COLS)
    paid_a, paid_b = pairs.PaidToDate_a >= pairs.DocTotal_a - 0.005, pairs.PaidToDate_b >= pairs.DocTotal_b - 0.005
    out = pd.DataFrame({
        "type": "DUPLICATE AP",
        "severity": np.where((pairs.match == "ref") | (paid_a & paid_b), "high", "medium"),
        "doc": "A/P invoice " + pairs.DocNum_b.astype(str) + " vs " + pairs.DocNum_a.astype(str),
        "DocDate": pairs.DocDate_b,
        "partner": pairs.CardName_b.fillna(pairs.CardCode),
        "impact": pairs.DocTotal_b,
        "paid": np.select([paid_a & paid_b, paid_a | paid_b], ["both paid", "one paid"], "neither paid"),
    })
    out["reason"] = [
        (f"same supplier reference '{r}'" if m == "ref" else f"same total {t:,.2f} {abs((b - a).days)} day(s) apart")
        + f" as A/P invoice {n} ({p})"
        for m, r, t, a, b, n, p in zip(pairs.match, pairs.NumAtCard_b, pairs.DocTotal_b, pairs.DocDate_a, pairs.DocDate_b,
                                      pairs.DocNum_a, out.paid)
    ]
    return out


def build() -> dict:
    today = pd.Timestamp.today().normalize()
    since = today - pd.Timedelta(days=ANOMALY_DAYS)
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    items = pd.read_parquet(DATA_DIR / "items.parquet", columns=["ItemCode", "ItemName"]).set_index("ItemCode").ItemName
    ar, ap = _lines("ar_lines"), _lines("ap_lines")
    parts = []
    for df in (price_deviations(ap, "AP", since, today), price_deviations(ar, "AR", since, today), below_cost(ar, since)):
        paid = np.select([df.PaidToDate >= df.DocTotal - 0.005, df.PaidToDate > 0], ["paid", "part paid"], "unpaid")
        df = df.assign(partner=df.CardName.fillna(df.CardCode), qty=df.InvQty,
                       ItemName=df.ItemCode.map(items).fillna(df.Dscription), paid=paid,
                       reason=[f"{r}; invoice {p}" for r, p in zip(df.reason, paid)])
        parts.append(df.reindex(columns=COLS))
    parts.append(duplicate_ap(since).reindex(columns=COLS))
    found = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame(columns=COLS)
    found = found.sort_values("impact", ascending=False, key=lambda s: s.abs()).round(2)
    found.to_csv(DATA_DIR / "anomalies.csv", index=False)
    # Money impact counts only losses; a cheaper-than-usual purchase is still listed (possible keying error) but saves money.
    summary = (found.assign(loss=found.impact.clip(lower=0))
               .groupby("type").agg(count=("doc", "size"), money_lost=("loss", "sum")).round(2))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": meta.get("database"),
        "data_synced_at": meta.get("synced_at"),
        "currency": meta.get("currency"),
        "documents_since": str(since.date()),
        "by_type": json.loads(summary.reset_index().to_json(orient="records")),
        "items": json.loads(found.to_json(orient="records", date_format="iso")),
    }


def main() -> None:
    result = build()
    (DATA_DIR / "anomalies.json").write_text(json.dumps(result, indent=2))
    print(f"Transaction checks since {result['documents_since']}:")
    for t in result["by_type"] or [{"type": "none found", "count": 0, "money_lost": 0}]:
        print(f"  {t['type']:<14}{t['count']:>6}  {result['currency']}{t['money_lost']:>14,.2f}")


if __name__ == "__main__":
    main()
