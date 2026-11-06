"""The home dashboard: the company's key figures, shown on the chat site's start screen until someone starts a chat.

LIVE from B1 (read-only SQL, the same figures as the chat's analytics tools), kept for CACHE_SECONDS so opening the page
again does not query B1 every time. Each person sees only the areas of their role (access.py).

    GET /sapb1/home          the page (dashboard.html); shown by the chat site's page script (webui/loader.js)
    GET /sapb1/home/data     its figures as JSON: ?year=2026&group=<item group>
    POST /sapb1/home/refresh "Refresh data": re-read the live figures now and pull the latest B1 data (the nightly
                             refresh's steps, as sapb1-ai-refresh-now@<company>, without the stock pushes)
    GET /sapb1/home/refresh  whether that pull is still running, and when B1 data was last pulled

A SAVED COPY of every figure set is kept on this PC (data/<db>/dashboard/*.json, written after each successful read and
by `python -m sapb1_ai.dashboard` at the end of each refresh). If the SAP PC cannot be reached, the dashboard shows that
last copy, marked offline with when it was saved, instead of an error.

Only the chat site's proxy may call these: it signs the person in first and passes their email (X-Sapb1-Email, which it
removes from anything the browser sends). Requests from anywhere else (e.g. straight to this port) are refused.
"""

import datetime as dt
import hashlib
import ipaddress
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pandas as pd

from . import access
from .analytics import _sales_lines
from .company import COMPANY
from .db import query
from .numbers import label

CACHE_SECONDS = int(os.getenv("DASHBOARD_CACHE_SECONDS", "900"))
PAGE = Path(__file__).with_name("dashboard.html")
TOP_GROUPS = 4  # stacked chart: biggest item groups, the rest as "Other"
_cache: dict = {}
_lock = threading.Lock()


def trusted(client_host: str | None, company: str | None) -> bool:
    """Only the sign-in proxy (a container on this PC) for this company; never straight from the network."""
    try:
        ip = ipaddress.ip_address(client_host or "")
    except ValueError:
        return False
    local = ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12")
    return local and (company or "") == COMPANY


def _cached(key, make):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = make()
    with _lock:
        _cache[key] = (now, value)
    return value


# ---------------------------------------------------------------- saved copy, for when the SAP PC is down
DOWN_SECONDS = 60  # after a failed read, use the saved copy for this long before trying SAP again (each try waits ~15 s)
_down = {"until": 0.0}


def _save_dir() -> Path:
    from .sync import DATA_DIR
    return DATA_DIR / "dashboard"


def _file(key) -> Path:
    parts = key if isinstance(key, tuple) else (key,)
    name = "_".join(str(p) if i < 2 else hashlib.md5(str(p).encode()).hexdigest()[:10] for i, p in enumerate(parts))
    return _save_dir() / f"{name}.json"


def _save(key, value) -> None:
    try:
        f = _file(key)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "value": value}, default=str))
        tmp.replace(f)
    except Exception:
        pass  # the saved copy is a fallback; never break the live figures over it


def _kept(key, make, offline: list):
    """Live figures (cached for CACHE_SECONDS) and saved to disk; the saved copy if SAP B1 cannot be read."""
    if time.time() >= _down["until"]:
        try:
            value = _cached(key, make)
            _save(key, value)
            return value
        except Exception:
            _down["until"] = time.time() + DOWN_SECONDS
    try:
        saved = json.loads(_file(key).read_text())
    except (OSError, ValueError):
        raise RuntimeError("SAP B1 cannot be reached and there is no saved copy of these figures yet.") from None
    offline.append(saved["saved_at"])
    return saved["value"]


def _base() -> dict:
    """Things that change slowly: currency, which years have sales."""
    cur = query("SELECT MainCurncy, CompnyName FROM OADM")
    years = query("SELECT DISTINCT YEAR(DocDate) AS y FROM OINV WHERE CANCELED = 'N' ORDER BY 1 DESC").y.astype(int).tolist()
    return {"currency": label(cur.MainCurncy.iloc[0]), "company": str(cur.CompnyName.iloc[0]), "years": years}


def _period(year: int) -> tuple[dt.date, dt.date, dt.date, dt.date, bool]:
    """This year to date (or the whole year if it is over) and the same days a year earlier."""
    today = dt.date.today()
    d_from = dt.date(year, 1, 1)
    ytd = year == today.year
    d_to = today if ytd else dt.date(year, 12, 31)
    try:
        p_to = d_to.replace(year=year - 1)
    except ValueError:  # 29 February
        p_to = dt.date(year - 1, 2, 28)
    return d_from, d_to, dt.date(year - 1, 1, 1), p_to, ytd


def _sales(year: int) -> pd.DataFrame:
    return _cached(("sales", year), lambda: _sales_lines(dt.date(year, 1, 1), dt.date(year, 12, 31)))


def _round(v) -> float:
    return round(float(v), 2)


def _sales_part(year: int, group: str) -> dict:
    d_from, d_to, p_from, p_to, ytd = _period(year)
    cur, prev = _sales(year), _sales(year - 1)
    groups = sorted(set(cur.ItemGroup) | set(prev.ItemGroup))
    if group:
        cur, prev = cur[cur.ItemGroup == group], prev[prev.ItemGroup == group]
    now = cur[cur.DocDate.dt.date <= d_to]
    before = prev[prev.DocDate.dt.date <= p_to]

    def month_sum(df):
        s = df.groupby(df.DocDate.dt.month).Net.sum()
        return [_round(s.get(m, 0)) for m in range(1, 13)]

    sales, gp = now.Net.sum(), now.GP.sum()
    kpi = {"sales": _round(sales), "sales_prev": _round(before.Net.sum()), "gp": _round(gp),
           "margin_pct": round(float(gp / sales * 100), 1) if sales else None,
           "margin_prev": round(float(before.GP.sum() / before.Net.sum() * 100), 1) if before.Net.sum() else None,
           "invoices": int(now[now.Net > 0].DocEntry.nunique()), "invoices_prev": int(before[before.Net > 0].DocEntry.nunique()),
           "customers": int(now.CardCode.nunique()), "customers_prev": int(before.CardCode.nunique())}

    # Item groups by month: the biggest few, the rest as Other (fixed order by this year's sales, so colours stay put).
    by_group = now.groupby("ItemGroup").Net.sum().sort_values(ascending=False)
    top = [g for g in by_group.index[:TOP_GROUPS]]
    stack = [{"name": g, "values": month_sum(now[now.ItemGroup == g])} for g in top]
    rest = now[~now.ItemGroup.isin(top)]
    if len(rest):
        stack.append({"name": "Other", "values": month_sum(rest)})

    cust = (now.groupby(["CardCode", "CardName"]).agg(sales=("Net", "sum"), gp=("GP", "sum")).reset_index()
            .sort_values("sales", ascending=False).head(10))
    items = (now.groupby(["ItemCode", "ItemName"]).agg(sales=("Net", "sum"), gp=("GP", "sum"), qty=("Qty", "sum")).reset_index()
             .sort_values("sales", ascending=False).head(15))
    items = items[items.sales > 0]
    heat_groups = by_group.index[:8].tolist()
    heat = [month_sum(now[now.ItemGroup == g]) for g in heat_groups]
    last_month = d_to.month if ytd else 12
    return {
        "kpi": kpi, "groups": groups, "last_month": last_month,
        "monthly": {"this": month_sum(now), "last": month_sum(prev)},  # last year: the whole year, for the shape
        "stack": stack,
        "customers": [{"code": r.CardCode, "name": r.CardName, "sales": _round(r.sales), "gp": _round(r.gp)} for r in cust.itertuples()],
        "items": [{"code": r.ItemCode, "name": r.ItemName, "sales": _round(r.sales), "qty": _round(r.qty),
                   "margin_pct": round(float(r.gp / r.sales * 100), 1)} for r in items.itertuples()],
        "heat": {"groups": heat_groups, "values": heat},
    }


def _aging() -> dict:
    df = query("""SELECT CardCode, CardName, DocDueDate, DocTotal - PaidToDate AS Balance
                  FROM OINV WHERE DocStatus = 'O' AND CANCELED = 'N'""")
    if df.empty:
        return {"owed": 0, "overdue": 0, "buckets": [], "customers": 0}
    df["Balance"] = df.Balance.astype(float)
    late = (pd.Timestamp(dt.date.today()) - pd.to_datetime(df.DocDueDate)).dt.days
    names = ["Not due", "1–30 days", "31–60 days", "61–90 days", "Over 90 days"]
    cut = pd.cut(late, [-10**6, 0, 30, 60, 90, 10**6], labels=names)
    b = df.groupby(cut, observed=False).Balance.sum()
    return {"owed": _round(df.Balance.sum()), "overdue": _round(df.Balance[late > 0].sum()),
            "customers": int(df.CardCode.nunique()), "buckets": [{"name": n, "amount": _round(b.get(n, 0))} for n in names]}


def _open_orders(head: str, lines: str) -> dict:
    """Open orders and what is still open on them (open line amounts before tax: delivered or received parts excluded)."""
    r = query(f"""SELECT COUNT(DISTINCT h.DocEntry) AS n, ISNULL(SUM(l.OpenSum), 0) AS v FROM {lines} l
                   JOIN {head} h ON h.DocEntry = l.DocEntry
                   WHERE h.DocStatus = 'O' AND h.CANCELED = 'N' AND l.LineStatus = 'O'""")
    return {"count": int(r.n.iloc[0]), "value": _round(r.v.iloc[0])}


def _reorder() -> dict | None:
    """Stock status from the nightly stock planner (data/<db>/reorder_report.csv, forecast.py), item-warehouse rows."""
    from .sync import DATA_DIR
    path = DATA_DIR / "reorder_report.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    order = ["LINE STOP RISK", "STOCKOUT RISK", "REORDER", "OVERSTOCK", "DEAD STOCK", "OK"]
    counts = df.status.value_counts()
    return {"to_order": int(df.status.isin(order[:3]).sum()), "statuses": [{"name": s, "count": int(counts.get(s, 0))} for s in order],
            "as_of": time.strftime("%d %b %H:%M", time.localtime(path.stat().st_mtime))}


def data(year: int | None, group: str, user: dict) -> dict:
    offline: list[float] = []
    base = _kept("base", _base, offline)
    areas = access.areas(user)
    years = base["years"] or [dt.date.today().year]
    year = year if year in years else years[0]
    out = {"company": base["company"], "currency": base["currency"], "years": years, "year": year, "group": group,
           "role": user["role"], "name": user["name"], "areas": sorted(areas), "as_of": time.strftime("%H:%M"),
           "ytd": _period(year)[4], "cache_minutes": CACHE_SECONDS // 60}
    if "sales" in areas:
        out["sales"] = _kept(("sales_part", year, group), lambda: _sales_part(year, group), offline)
        out["open_sales"] = _kept("open_sales", lambda: _open_orders("ORDR", "RDR1"), offline)
    if "collections" in areas:
        out["aging"] = _kept("aging", _aging, offline)
    if "purchasing" in areas:
        out["open_purchases"] = _kept("open_purchases", lambda: _open_orders("OPOR", "POR1"), offline)
    if "stock" in areas:
        out["reorder"] = _cached("reorder", _reorder)  # already a file on this PC (the nightly stock plan)
    if offline:  # SAP PC not reachable: some or all figures come from the saved copy
        out["offline"] = True
        out["saved_at"] = time.strftime("%d %b %H:%M", time.localtime(min(offline)))
    return out


def warm() -> int:
    """Read and save every year and item group, so the saved copy is complete for when the SAP PC is down."""
    offline: list[float] = []
    n = 0
    for key, make in (("base", _base), ("open_sales", lambda: _open_orders("ORDR", "RDR1")), ("aging", _aging),
                      ("open_purchases", lambda: _open_orders("OPOR", "POR1"))):
        _kept(key, make, offline); n += 1
    for year in _kept("base", _base, offline)["years"]:
        part = _kept(("sales_part", year, ""), lambda: _sales_part(year, ""), offline); n += 1
        for g in part["groups"]:
            _kept(("sales_part", year, g), lambda: _sales_part(year, g), offline); n += 1
    if offline:
        raise SystemExit("SAP B1 could not be reached; the saved copy was not updated.")
    return n


# ---------------------------------------------------------------- "Refresh data"
REFRESH_UNIT = f"sapb1-ai-refresh-now@{COMPANY}.service"
NIGHTLY_UNIT = f"sapb1-ai-refresh@{COMPANY}.service"
MIN_GAP_SECONDS = 300  # a pull this recent is not repeated; the live figures are still re-read
_pull = {"started": 0.0, "result": ""}


def _active(unit: str) -> bool:
    r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() in ("activating", "active", "reloading")


def _synced_at() -> float:
    """When B1 data was last pulled (the sync step writes sync_meta.json)."""
    from .sync import DATA_DIR
    try:
        return dt.datetime.fromisoformat(json.loads((DATA_DIR / "sync_meta.json").read_text())["synced_at"]).timestamp()
    except Exception:
        return 0.0


def _clear_cache() -> None:
    with _lock:
        _cache.clear()


def refresh_status() -> dict:
    running = _active(REFRESH_UNIT) or _active(NIGHTLY_UNIT)
    started = _pull["started"]
    if started and not running and time.time() - started > 5:  # ours has finished
        ok = _synced_at() >= started - 1
        _pull.update(started=0.0, result="ok" if ok else "failed")
        _clear_cache()
    elif started and not running:
        running = True  # just asked to start; systemd has not picked it up yet
    last = _synced_at()
    return {"running": running, "result": _pull["result"],
            "last_pull": time.strftime("%d %b %H:%M", time.localtime(last)) if last else ""}


def start_refresh() -> dict:
    """Re-read the live figures now; pull the latest B1 data unless a pull is running or has just been done."""
    _clear_cache()
    status = refresh_status()
    if status["running"]:
        return {**status, "started": False, "message": "B1 data is already being pulled; the dashboard will update when it is done."}
    if time.time() - _synced_at() < MIN_GAP_SECONDS:
        return {**status, "started": False, "message": f"Figures re-read now. B1 data was pulled at {status['last_pull']}, a few minutes ago."}
    subprocess.run(["systemctl", "--user", "start", "--no-block", REFRESH_UNIT], check=True, capture_output=True, timeout=15)
    _pull.update(started=time.time(), result="")
    return {**status, "running": True, "started": True, "message": "Pulling the latest data from SAP B1…"}


def page() -> str:
    return PAGE.read_text()


if __name__ == "__main__":
    print(f"{COMPANY}: saved {warm()} dashboard figure sets to {_save_dir()}")
