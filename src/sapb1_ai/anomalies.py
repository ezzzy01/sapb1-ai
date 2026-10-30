"""Nightly transaction checks: purchasing, sales and ledger red flags, each with money lost or amount to review.

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
  - 3-WAY QTY / 3-WAY PRICE: A/P invoices billing more than the goods receipt received, or a price more than
    PO_PRICE_TOL_PCT above the purchase order. NO PO: item invoices with no purchase order or goods receipt behind them.
  - BANK CHANGE: supplier bank details changed (B1 change log), with payments made in the 60 days after.
  - CREDIT SPIKE: customer or salesperson whose credit notes are at least CREDIT_RATIO of sales and double the year before.
  - JOURNAL: manual journal entries with two or more red flags (weekend/late, backdated, round amount, new user/account
    combination) or unusually large (above the 99th percentile).
"money_lost" sums measured losses; "amount" is the value at stake for findings that are risks to review, not losses.
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
PO_PRICE_TOL_PCT = float(os.getenv("PO_PRICE_TOL_PCT", "2"))
CREDIT_RATIO = 0.10
COLS = ["type", "severity", "doc", "DocDate", "partner", "ItemCode", "ItemName", "qty", "unit_price", "usual_price",
        "deviation_pct", "impact", "amount", "paid", "reason"]


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


def _paid(df: pd.DataFrame) -> np.ndarray:
    return np.select([df.PaidToDate >= df.DocTotal - 0.005, df.PaidToDate > 0], ["paid", "part paid"], "unpaid")


def three_way_match(since: pd.Timestamp) -> pd.DataFrame:
    """A/P invoice lines compared with the goods receipt and purchase order they were copied from."""
    m = pd.read_parquet(DATA_DIR / "ap_match.parquet")
    m["DocDate"] = pd.to_datetime(m.DocDate)
    m["paid"] = _paid(m)
    m["unit_lc"] = m.LineTotal / m.Quantity.where(m.Quantity > 0)
    out = []

    # Billed for more than was received: all invoices on one goods receipt line together exceed its quantity.
    based = m[m.GRPOEntry.notna()].sort_values(["DocDate", "DocEntry"])
    based = based.assign(billed=based.groupby(["GRPOEntry", "GRPOLine"]).Quantity.cumsum())
    over = based[(based.billed > based.GRPOQty * 1.001 + 1e-6) & (based.DocDate >= since)].copy()
    over["qty"] = np.minimum(over.billed - over.GRPOQty, over.Quantity)
    over["impact"] = over.qty * over.unit_lc
    over["type"], over["severity"] = "3-WAY QTY", "high"
    over["reason"] = [f"billed {b:,.0f} in total but goods receipt {g:.0f} received only {r:,.0f}; invoice {p}"
                      for b, g, r, p in zip(over.billed, over.GRPONum, over.GRPOQty, over.paid)]
    out.append(over)

    # Invoice price above the agreed purchase order price (same currency only).
    po = m[m.DocDate >= since].dropna(subset=["POPrice"])
    po = po[(po.POPrice > 0) & (po.Currency == po.POCurrency)].copy()
    po["deviation_pct"] = (po.Price / po.POPrice - 1) * 100
    po = po[po.deviation_pct >= PO_PRICE_TOL_PCT].copy()
    po["impact"] = po.LineTotal * (1 - po.POPrice / po.Price)
    po["type"] = "3-WAY PRICE"
    po["severity"] = np.where(po.deviation_pct >= 10, "high", "medium")
    po["unit_price"], po["usual_price"], po["qty"] = po.Price, po.POPrice, po.Quantity
    po["reason"] = [f"invoice price {p:,.2f} is {d:.0f}% above purchase order {n:.0f} price {o:,.2f}; invoice {s}"
                    for p, d, n, o, s in zip(po.Price, po.deviation_pct, po.PONum, po.POPrice, po.paid)]
    out.append(po)

    # Item invoices with no purchase order or goods receipt behind any line.
    heads = m[m.DocDate >= since].groupby("DocEntry").agg(
        DocNum=("DocNum", "first"), DocDate=("DocDate", "first"), CardCode=("CardCode", "first"), CardName=("CardName", "first"),
        DocTotal=("DocTotal", "first"), paid=("paid", "first"), no_base=("BaseType", lambda b: (b == -1).all()))
    nopo = heads[heads.no_base].reset_index()
    nopo["type"], nopo["severity"], nopo["amount"] = "NO PO", "medium", nopo.DocTotal
    nopo["reason"] = [f"item invoice not based on a purchase order or goods receipt; invoice {p}" for p in nopo.paid]
    out.append(nopo)

    df = pd.concat(out, ignore_index=True)
    df["doc"] = "A/P invoice " + df.DocNum.astype(str)
    df["partner"] = df.CardName.fillna(df.CardCode)
    df["ItemName"] = df.get("Dscription")
    return df


def bank_changes(since: pd.Timestamp, users: pd.Series) -> pd.DataFrame:
    """Supplier bank details changed recently, with the payments made to that supplier afterwards."""
    h = pd.read_parquet(DATA_DIR / "bp_bank_history.parquet")
    h["UpdateDate"] = pd.to_datetime(h.UpdateDate)
    fields = ["DflAccount", "BankCode", "DflBranch", "DflIBAN", "DflSwift"]
    h[fields] = h[fields].fillna("").astype(str).replace({"-1": ""})
    # One edit can be logged as several versions; keep the last version per supplier and day.
    h = h.sort_values(["CardCode", "LogInstanc"]).groupby(["CardCode", h.UpdateDate.dt.normalize()]).last().reset_index(level=0)
    h = h.reset_index(drop=True)
    prev = h.groupby("CardCode")[fields].shift()
    changed = prev.notna().all(axis=1) & (h[fields] != prev).any(axis=1)
    ch = h[changed & (h.UpdateDate >= since)].copy()
    if ch.empty:
        return pd.DataFrame(columns=COLS)
    prev = prev.loc[ch.index]
    ch["added"] = (prev == "").all(axis=1)
    pay = pd.read_parquet(DATA_DIR / "ap_payments.parquet")
    pay["DocDate"] = pd.to_datetime(pay.DocDate)
    ch["amount"] = [pay[(pay.CardCode == c) & (pay.DocDate >= d) & (pay.DocDate <= d + pd.Timedelta(days=60))].DocTotal.sum()
                    for c, d in zip(ch.CardCode, ch.UpdateDate)]
    ch["type"] = "BANK CHANGE"
    ch["severity"] = np.where(~ch.added & (ch.amount > 0), "high", "medium")
    ch["doc"] = "supplier " + ch.CardCode
    ch["DocDate"], ch["partner"] = ch.UpdateDate, ch.CardName.fillna(ch.CardCode)
    ch["reason"] = [
        ("bank details added" if a else "bank details changed (" + ", ".join(f"{f}: '{o}' -> '{n}'" for f, o, n in
                                                                       zip(fields, po, pn) if o != n) + ")")
        + f" by {users.get(u, u)}; " + (f"{amt:,.2f} paid to this supplier in the 60 days after" if amt else "no payment since")
        for a, po, pn, u, amt in zip(ch.added, prev.itertuples(index=False), ch[fields].itertuples(index=False), ch.UserSign2, ch.amount)
    ]
    return ch


def credit_spikes(since: pd.Timestamp, today: pd.Timestamp, salespeople: pd.Series) -> pd.DataFrame:
    """Customers or salespeople whose credit notes jumped compared with the year before."""
    inv = pd.read_parquet(DATA_DIR / "ar_invoices.parquet")
    cr = pd.read_parquet(DATA_DIR / "ar_credits.parquet")
    base_from = since - pd.Timedelta(days=365)
    out = []
    for dim, label in (("CardCode", "customer"), ("SlpCode", "salesperson")):
        def window(df, a, b):
            d = pd.to_datetime(df.DocDate)
            return df[(d >= a) & (d < b)].groupby(dim).DocTotal.agg(["sum", "size"])
        r_inv, r_cr = window(inv, since, today + pd.Timedelta(days=1)), window(cr, since, today + pd.Timedelta(days=1))
        b_inv, b_cr = window(inv, base_from, since), window(cr, base_from, since)
        s = pd.DataFrame({"inv": r_inv["sum"], "cr": r_cr["sum"], "n": r_cr["size"],
                          "b_ratio": (b_cr["sum"] / b_inv["sum"]).reindex(r_cr.index)}).fillna(0)
        s["ratio"] = s.cr / s.inv.where(s.inv > 0)
        hit = s[(s.n >= 2) & (s.ratio.isna() | ((s.ratio >= CREDIT_RATIO) & (s.ratio >= 2 * s.b_ratio)))].copy()
        if hit.empty:
            continue
        hit["impact"] = hit.cr - hit.b_ratio * hit.inv
        names = (cr.drop_duplicates(dim).set_index(dim).CardName if dim == "CardCode" else salespeople)
        hit["partner"] = [names.get(k, k) for k in hit.index]
        hit["doc"] = label + " " + hit.index.astype(str)
        hit["amount"] = hit.cr
        hit["reason"] = [f"{n:.0f} credit notes worth {c:,.2f} = {('%.0f%%' % (r * 100)) if r == r else 'no'} of invoiced sales "
                         f"(was {b * 100:.0f}% the year before)" for n, c, r, b in zip(hit.n, hit.cr, hit.ratio, hit.b_ratio)]
        out.append(hit)
    if not out:
        return pd.DataFrame(columns=COLS)
    df = pd.concat(out)
    df["type"], df["severity"], df["DocDate"] = "CREDIT SPIKE", "medium", today
    return df


def journal_checks(since: pd.Timestamp, users: pd.Series) -> pd.DataFrame:
    """Manual journal entries with several red flags: weekend/late, backdated, round, unusually large, new user/account."""
    j = pd.read_parquet(DATA_DIR / "journal_lines.parquet")
    if j.empty:
        return pd.DataFrame(columns=COLS)
    for c in ("RefDate", "CreateDate"):
        j[c] = pd.to_datetime(j[c])
    heads = j.groupby("TransId").first()
    heads = heads[heads.StornoToTr.isna()]  # reversals of earlier entries are not new postings
    hist, recent = heads[heads.RefDate < since], heads[heads.RefDate >= since]
    if recent.empty:
        return pd.DataFrame(columns=COLS)
    seen = set(zip(j[j.RefDate < since].UserSign, j[j.RefDate < since].Account))
    experienced = hist.UserSign.value_counts()  # only users with a posting history can break their own pattern
    big = hist.LocTotal.quantile(0.99) if len(hist) >= 20 else np.inf
    t = recent.CreateTime.fillna(-1).astype(int)
    t = np.where(t > 2400, t // 100, t)  # HHMMSS -> HHMM
    flags = pd.DataFrame({
        "posted on a weekend": recent.CreateDate.dt.weekday >= 5,
        "posted outside 07:00-20:00": (t >= 0) & ((t < 700) | (t > 2000)),
        "dated over 30 days before it was entered": recent.RefDate < recent.CreateDate - pd.Timedelta(days=30),
        # Recurring postings (payroll, rent) repeat the same round amount; only a new round amount counts.
        "round amount": (recent.LocTotal >= 1000) & (recent.LocTotal % 1000 == 0) & ~recent.LocTotal.isin(hist.LocTotal),
        "unusually large": recent.LocTotal > big,
        "user never posted to this account before": [
            experienced.get(u, 0) >= 10 and any((u, a) not in seen for a in j[j.TransId == tid].Account)
            for tid, u in zip(recent.index, recent.UserSign)],
    }, index=recent.index)
    n = flags.sum(axis=1)
    hit = recent[(n >= 2) | flags["unusually large"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=COLS)
    hit["type"] = "JOURNAL"
    hit["severity"] = np.where(n.loc[hit.index] >= 3, "high", "medium")
    hit["doc"] = "journal entry " + hit.Number.astype(str)
    hit["DocDate"], hit["amount"] = hit.RefDate, hit.LocTotal
    hit["partner"] = [users.get(u, u) for u in hit.UserSign]
    hit["reason"] = ["; ".join(flags.columns[flags.loc[i]]) + (f" ('{m}')" if m else "") for i, m in zip(hit.index, hit.Memo.fillna(""))]
    return hit


def build() -> dict:
    today = pd.Timestamp.today().normalize()
    since = today - pd.Timedelta(days=ANOMALY_DAYS)
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    items = pd.read_parquet(DATA_DIR / "items.parquet", columns=["ItemCode", "ItemName"]).set_index("ItemCode").ItemName
    users = pd.read_parquet(DATA_DIR / "users.parquet").set_index("USERID").U_NAME
    salespeople = pd.read_parquet(DATA_DIR / "salespeople.parquet").set_index("SlpCode").SlpName
    ar, ap = _lines("ar_lines"), _lines("ap_lines")
    parts = []
    for df in (price_deviations(ap, "AP", since, today), price_deviations(ar, "AR", since, today), below_cost(ar, since)):
        paid = _paid(df)
        df = df.assign(partner=df.CardName.fillna(df.CardCode), qty=df.InvQty,
                       ItemName=df.ItemCode.map(items).fillna(df.Dscription), paid=paid,
                       reason=[f"{r}; invoice {p}" for r, p in zip(df.reason, paid)])
        parts.append(df.reindex(columns=COLS))
    for df in (duplicate_ap(since), three_way_match(since), bank_changes(since, users), credit_spikes(since, today, salespeople),
               journal_checks(since, users)):
        parts.append(df.reindex(columns=COLS))
    parts = [p for p in parts if not p.empty]
    found = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    found["ItemName"] = found.ItemName.fillna(found.ItemCode.map(items))
    found = (found.assign(_rank=found.severity.map({"high": 0}).fillna(1), _size=found.impact.abs().fillna(found.amount))
             .sort_values(["_rank", "_size"], ascending=[True, False]).drop(columns=["_rank", "_size"]).round(2))
    found.to_csv(DATA_DIR / "anomalies.csv", index=False)
    # money_lost counts only losses (a cheaper-than-usual purchase is listed as a possible keying error but saves money);
    # amount_to_review is the value of findings that are risks rather than measured losses (bank changes, journals, no PO).
    summary = (found.assign(loss=found.impact.clip(lower=0))
               .groupby("type").agg(count=("doc", "size"), high=("severity", lambda s: int((s == "high").sum())),
                                    money_lost=("loss", "sum"), amount_to_review=("amount", "sum")).round(2))
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
    print(f"  {'type':<14}{'count':>6}{'high':>6}{'money lost':>16}{'to review':>16}")
    for t in result["by_type"] or [{"type": "none found", "count": 0, "high": 0, "money_lost": 0, "amount_to_review": 0}]:
        print(f"  {t['type']:<14}{t['count']:>6}{t['high']:>6}{t['money_lost']:>16,.2f}{t['amount_to_review']:>16,.2f}")


if __name__ == "__main__":
    main()
