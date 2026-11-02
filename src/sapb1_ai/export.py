"""Excel downloads for chat answers, in one house report template.

A report writes a workbook here and puts a "Download Excel" link in its answer. Each company's own chat site serves
only that company's folder (site.py mounts exports/<company> at /static/exports/<company>), behind the sign-in. Each
file sits in its own long random folder that cannot be guessed or listed, and is deleted after EXPORT_KEEP_HOURS.

The template, on every sheet: a title block (company, report, period, when and where the data came from), a styled
Excel table (filter buttons, banded rows, frozen header), number formats for money / quantities / percentages /
dates, a totals row for money columns, readable column names, and print setup (landscape, fit to page width, header
and footer with page numbers). A report can add native Excel charts (e.g. sales by month).
"""

import os
import re
import secrets
import shutil
import time
from datetime import datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .company import COMPANY, ROOT

_SLOT = COMPANY or "default"
EXPORT_DIR = ROOT / "exports" / _SLOT
URL_PREFIX = f"/static/exports/{_SLOT}"
KEEP_HOURS = float(os.getenv("EXPORT_KEEP_HOURS", "24"))
COMPANY_NAME = os.getenv("COMPANY_NAME", "SAP B1 AI")

# House style (SAP Fiori Horizon colours, like the chat site).
BRAND, INK, MUTED, TAB = "0064D9", "1D2D3E", "556B82", "0A6ED1"
TITLE_ROWS = 4  # title, subtitle, source line, blank; the table header is the next row
FIRST_ROW = TITLE_ROWS + 1
_thin = Side(style="thin", color="D5DADD")

# B1 / report field names in plain words.
NAMES = {
    "ItemCode": "Item code", "ItemName": "Item", "ItmsGrpNam": "Item group", "WhsCode": "Warehouse", "OnHand": "On hand",
    "IsCommited": "Committed to customers", "OnOrder": "Ordered from suppliers", "available": "Available",
    "position": "Stock position", "fc_weekly": "Forecast per week", "weeks_cover": "Weeks of cover",
    "lead_days": "Lead time (days)", "runout_date": "Run-out date", "days_to_runout": "Days to run-out",
    "production_orders_at_risk": "Production orders at risk", "reorder_point": "Reorder point",
    "suggested_qty": "Suggested order qty", "unit_cost": "Unit cost", "suggested_value": "Suggested order value",
    "PrefVendor": "Preferred supplier", "abc": "ABC class", "critical_reason": "Why critical", "CardCode": "Code",
    "CardName": "Name", "DocNum": "Document no.", "DocDate": "Date", "DocDueDate": "Due date", "DocTotal": "Total",
}
MONEY_WORDS = re.compile(r"(value|amount|total|net|tax|sales|revenue|profit|cost|price|balance|paid|owed|owe|"
                         r"limit|lost|impact|outstanding|cash)", re.I)


def cleanup() -> None:
    """Delete downloads older than KEEP_HOURS."""
    if not EXPORT_DIR.exists():
        return
    cutoff = time.time() - KEEP_HOURS * 3600
    for d in EXPORT_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _plain(col: str) -> str:
    if col in NAMES:
        return NAMES[col]
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(col)).replace("_", " ")
    text = re.sub(r"\.(?=\w)", " · ", text).strip()  # nested figures (late payers.count), not "No."
    return text[:1].upper() + text[1:]


def _typed(df: pd.DataFrame) -> pd.DataFrame:
    """Dates sent as text (ISO, from JSON results) become real Excel dates; columns get plain names."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            vals = df[col].dropna()
            if len(vals) and vals.map(lambda v: isinstance(v, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+)?", v))).all():
                df[col] = pd.to_datetime(df[col], errors="coerce")
        if pd.api.types.is_datetime64_any_dtype(df[col]) and getattr(df[col].dt, "tz", None) is not None:
            df[col] = df[col].dt.tz_localize(None)
    names, seen = [], set()
    for c in df.columns:  # Excel table headers must be unique, non-empty text
        base = _plain(c) or "Column"
        name, k = base, 2
        while name.lower() in seen:
            name, k = f"{base} {k}", k + 1
        seen.add(name.lower())
        names.append(name)
    df.columns = names
    return df


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80] or "report"


def _kind(name: str, s: pd.Series, money: set[str]) -> str:
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        return "text"
    if "%" in name or name.lower().endswith(("pct", "share")):
        return "pct"
    if name in money or (not money and MONEY_WORDS.search(name) and pd.api.types.is_float_dtype(s)):
        return "money"
    if pd.api.types.is_float_dtype(s) and not (s.dropna() % 1 == 0).all():
        return "decimal"
    return "int"


FORMATS = {"money": "#,##0.00;[Red]-#,##0.00", "int": "#,##0", "decimal": "#,##0.00", "pct": '0.0"%"', "date": "dd mmm yyyy"}


def _sheet(wb: Workbook, title: str, df: pd.DataFrame, report: str, subtitle: str, source: str, money: set[str], n: int) -> dict:
    ws = wb.create_sheet(title)
    ws.sheet_properties.tabColor = TAB if n == 0 else "A9B4BE"
    ws.sheet_view.showGridLines = False
    cols = list(df.columns)
    width = max(len(cols), 2)

    ws.cell(1, 1, report if title.lower() == report[:31].lower() else f"{report} · {title}").font = Font(size=16, bold=True, color=BRAND)
    ws.cell(2, 1, f"{COMPANY_NAME} · {subtitle}" if subtitle else COMPANY_NAME).font = Font(size=11, color=INK)
    ws.cell(3, 1, source).font = Font(size=9, italic=True, color=MUTED)
    ws.row_dimensions[1].height = 24

    header = FIRST_ROW
    kinds = {c: _kind(c, df[c], money) for c in cols}
    for j, c in enumerate(cols, start=1):
        cell = ws.cell(header, j, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=BRAND)
        cell.alignment = Alignment(horizontal="right" if kinds[c] in ("money", "int", "decimal", "pct") else "left",
                                   vertical="center", wrap_text=True)
    ws.row_dimensions[header].height = 30

    for i, row in enumerate(df.itertuples(index=False), start=header + 1):
        for j, (c, v) in enumerate(zip(cols, row), start=1):
            if v is None or (not isinstance(v, (list, dict, str)) and pd.isna(v)):
                v = None
            elif isinstance(v, (list, dict)):
                v = str(v)
            elif isinstance(v, pd.Timestamp):
                v = v.to_pydatetime()
            cell = ws.cell(i, j, v)
            if kinds[c] in FORMATS:
                cell.number_format = FORMATS[kinds[c]]
            elif isinstance(v, float):  # figures in a mixed column (e.g. Key figures)
                cell.number_format = FORMATS["decimal"]
                cell.alignment = Alignment(horizontal="right")
            elif isinstance(v, int) and not isinstance(v, bool):
                cell.number_format = FORMATS["int"]
                cell.alignment = Alignment(horizontal="right")
    last = header + max(len(df), 1)
    if df.empty:
        ws.cell(header + 1, 1, "(no rows)").font = Font(italic=True, color=MUTED)

    # A real Excel table: filter buttons and banded rows that keep working when people sort or filter.
    if len(df):
        ref = f"A{header}:{get_column_letter(len(cols))}{last}"
        name = "T_" + re.sub(r"\W", "_", title)[:20] + f"_{n}"
        table = Table(displayName=name, ref=ref)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)

    # Totals for money columns (formulas, so they stay right if someone edits a figure).
    totals = [j for j, c in enumerate(cols, start=1) if kinds[c] == "money"] if len(df) > 1 else []
    if totals:
        t = last + 1
        ws.cell(t, 1, "Total").font = Font(bold=True)
        for j in totals:
            col = get_column_letter(j)
            cell = ws.cell(t, j, f"=SUBTOTAL(109,{col}{header + 1}:{col}{last})")
            cell.number_format = FORMATS["money"]
            cell.font = Font(bold=True)
        for j in range(1, len(cols) + 1):
            ws.cell(t, j).border = Border(top=Side(style="medium", color=INK))

    for j, c in enumerate(cols, start=1):
        sample = [str(c)] + [("{:,.2f}".format(v) if isinstance(v, float) else str(v)) for v in df[c].head(300).tolist()]
        ws.column_dimensions[get_column_letter(j)].width = max(9, min(max(len(x) for x in sample) + 3, 48))
    ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width, 14)
    for r in (1, 2, 3):
        ws.cell(r, 1).alignment = Alignment(vertical="center")
    if len(cols) < 4:  # keep the title block readable on narrow sheets
        ws.column_dimensions[get_column_letter(width)].width = max(ws.column_dimensions[get_column_letter(width)].width, 22)

    ws.freeze_panes = ws.cell(header + 1, 1)
    ws.print_title_rows = f"{header}:{header}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddHeader.left.text = f"{COMPANY_NAME} · {title}"
    ws.oddFooter.left.text = "Confidential · " + COMPANY_NAME
    ws.oddFooter.right.text = "Page &P of &N"
    return {"ws": ws, "header": header, "last": last, "cols": cols}


def _chart(info: dict, spec: dict) -> None:
    ws, cols = info["ws"], info["cols"]
    if spec["category"] not in cols or spec["value"] not in cols or info["last"] <= info["header"]:
        return
    rows = min(info["last"], info["header"] + spec.get("top", 1000))
    ch = BarChart()
    ch.type = spec.get("type", "col")
    ch.title = spec["title"]
    ch.style = 10
    ch.legend = None
    ch.height, ch.width = 8, 16
    vcol, ccol = cols.index(spec["value"]) + 1, cols.index(spec["category"]) + 1
    ch.add_data(Reference(ws, min_col=vcol, min_row=info["header"], max_row=rows), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=ccol, min_row=info["header"] + 1, max_row=rows))
    if ch.type == "bar":
        ch.y_axis.scaling.orientation = "minMax"
        ch.x_axis.scaling.orientation = "maxMin"  # biggest at the top
    ch.series[0].graphicalProperties.solidFill = BRAND
    ws.add_chart(ch, f"{get_column_letter(len(cols) + 2)}{info['header']}")


def excel_link(filename: str, sheets: dict[str, pd.DataFrame], money_cols: tuple[str, ...] = (), *,
               title: str = "", subtitle: str = "", charts: list[dict] | None = None) -> str:
    """Write one workbook in the house template (one sheet per table); return a markdown download link."""
    cleanup()
    folder = EXPORT_DIR / secrets.token_urlsafe(24)
    folder.mkdir(parents=True)
    name = _safe_name(filename) + ".xlsx"
    report = title or filename.replace("_", " ")
    source = f"Generated {datetime.now():%d %b %Y %H:%M} from SAP Business One (read-only) · SAP B1 AI"
    money = {_plain(c) for c in money_cols}

    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title, wb.properties.creator, wb.properties.company = report, "SAP B1 AI", COMPANY_NAME
    used: set[str] = set()
    infos = {}
    for n, (sheet_title, df) in enumerate(sheets.items()):
        base = re.sub(r"[\[\]:*?/\\]", " ", sheet_title).strip()[:31] or "Sheet"
        base = base[:1].upper() + base[1:]
        sheet, k = base, 2
        while sheet.lower() in used:  # Excel sheet names must be unique (ignoring case) and at most 31 characters
            sheet, k = f"{base[:28]} {k}", k + 1
        used.add(sheet.lower())
        infos[sheet_title] = _sheet(wb, sheet, _typed(df), report, subtitle, source, money, n)
    for spec in charts or []:
        if spec["sheet"] in infos:
            _chart(infos[spec["sheet"]], spec)
    wb.save(folder / name)
    return f"📥 **[Download Excel: {name}]({URL_PREFIX}/{folder.name}/{name})** (link works for {KEEP_HOURS:g} hours)"
