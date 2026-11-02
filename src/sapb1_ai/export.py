"""Excel downloads for chat answers, in one house report template.

A report writes a workbook here and puts a "Download Excel" link in its answer. Each company's own chat site serves
only that company's folder (site.py mounts exports/<company> at /static/exports/<company>), behind the sign-in. Each
file sits in its own long random folder that cannot be guessed or listed, and is deleted after EXPORT_KEEP_HOURS.

The template, on every sheet: a title block (company, report, period, when and where the data came from), a styled
Excel table (filter buttons, banded rows, frozen header), number formats for money / quantities / percentages /
dates, a totals row for money columns, readable column names, and print setup (landscape, fit to page width, header
and footer with page numbers). A report can add a Summary sheet in front: headline figures as
large tiles and native Excel charts on a tidy grid (the data sheets stay plain tables).
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
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.label import DataLabel, DataLabelList
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.text import CharacterProperties, Font as DrawingFont, Paragraph, ParagraphProperties
from openpyxl.xml.functions import Element
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.formatting.rule import CellIsRule, DataBarRule
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
    if re.search(r"(\bno\.?|number|\bcode|\bid)$", name, re.I):  # document numbers and codes: 1049, not 1,049
        return "id"
    if "%" in name or name.lower().endswith(("pct", "share")):
        return "pct"
    if name in money or (not money and MONEY_WORDS.search(name) and pd.api.types.is_float_dtype(s)):
        return "money"
    if pd.api.types.is_float_dtype(s) and not (s.dropna() % 1 == 0).all():
        return "decimal"
    return "int"


FORMATS = {"id": "0", "money": "#,##0.00;[Red]-#,##0.00", "int": "#,##0", "decimal": "#,##0.00", "pct": '0.0"%"', "date": "dd mmm yyyy"}


TILE_FILL, GRID, AXIS = "F2F4F7", "E4E8EC", "C5CCD3"
MARGIN = 2  # tables start in column B, after a narrow margin, like the Summary sheet
STATUS = {"Overdue": ("B42318", "FDECEC"), "Open": ("8A5300", "FFF4E0"), "Closed": (MUTED, None)}


def _tiles(ws, row: int, items: list[tuple[str, object, str]], spans: list[tuple[int, int]]) -> None:
    """Headline figures as tiles (label, large value, note), in the Summary sheet's style."""
    fill = PatternFill("solid", fgColor=TILE_FILL)
    for (label, value, note), (c1, c2) in zip(items, spans):
        for r in range(row, row + 4):
            for j in range(c1, c2 + 1):
                ws.cell(r, j).fill = fill
                if j == c2 and c2 != spans[-1][1]:  # a white gap between tiles
                    ws.cell(r, j).border = Border(right=Side(style="thick", color="FFFFFF"))
        for j in range(c1, c2 + 1):
            ws.cell(row, j).border = Border(top=Side(style="thick", color=BRAND),
                                            right=ws.cell(row, j).border.right)
        for r, v, f in ((row + 1, label, Font(name="Calibri", size=10, color=MUTED)),
                        (row + 2, value, Font(name="Calibri", size=20, bold=True, color=INK)),
                        (row + 3, note, Font(name="Calibri", size=9, color=MUTED))):
            if c2 > c1:
                ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
            c = ws.cell(r, c1, v)
            c.font, c.alignment = f, Alignment(indent=1, vertical="center", horizontal="left")
            if isinstance(v, (int, float)):
                c.number_format = "#,##0"
    ws.row_dimensions[row].height = 6
    ws.row_dimensions[row + 2].height = 34
    ws.row_dimensions[row + 3].height = 18


def _sheet(wb: Workbook, title: str, df: pd.DataFrame, report: str, subtitle: str, source: str, money: set[str], n: int,
           bars: tuple[str, ...] = ()) -> dict:
    """One data sheet, in the Summary sheet's style: big title, headline tiles, then a clean, readable table."""
    ws = wb.create_sheet(title)
    ws.sheet_properties.tabColor = TAB if n == 0 else "A9B4BE"
    ws.sheet_view.showGridLines = False
    cols = list(df.columns)
    c0 = MARGIN
    col = lambda name: c0 + cols.index(name)  # sheet column of a table column
    ws.column_dimensions["A"].width = 2.5

    same = title.lower() == report[:31].lower()
    ws.cell(1, c0, report if same else title).font = Font(name="Calibri", size=18, bold=True, color=INK)
    sub = " · ".join(x for x in ([] if same else [report]) + [COMPANY_NAME, subtitle] if x)
    ws.cell(2, c0, sub).font = Font(name="Calibri", size=11, color=MUTED)
    ws.cell(3, c0, source).font = Font(name="Calibri", size=9, italic=True, color=MUTED)
    ws.row_dimensions[1].height = 28

    kinds = {c: _kind(c, df[c], money) for c in cols}
    for c in cols:  # a column of figures stored as mixed values (Key figures) reads like a number column
        vals = df[c].dropna()
        if kinds[c] == "text" and len(vals) and vals.map(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)).mean() > 0.5:
            kinds[c] = "figures"
    money_cols = [c for c in cols if kinds[c] == "money"]

    # Column widths first: the tiles are laid over the table's own columns.
    for c in cols:
        sample = [str(c)] + (["dd mmm yyyy"] if kinds[c] == "date" else
                             [("{:,.2f}".format(v) if isinstance(v, float) else str(v)) for v in df[c].head(300).tolist()])
        ws.column_dimensions[get_column_letter(col(c))].width = max(11, min(max(len(x) for x in sample) + 4, 48))

    header = FIRST_ROW
    if money_cols and len(df) > 1:
        count = (title[3:4].upper() + title[4:] + "s") if title.lower().startswith("by ") else (
            title if title.lower().endswith("s") else "Rows")
        items = [(count, len(df), "listed below")] + [
            (c if c.lower().startswith("total") else f"Total {c[:1].lower() + c[1:]}", float(pd.to_numeric(df[c], errors="coerce").sum()), "sum of the column")
            for c in money_cols[:3]]
        # Each tile takes table columns until it is wide enough (about 24 characters); as many tiles as fit.
        widths = [ws.column_dimensions[get_column_letter(col(c))].width for c in cols]
        spans, j = [], 0
        while j < len(cols) and len(spans) < len(items):
            k, w = j, widths[j]
            while w < 24 and k + 1 < len(cols):
                k, w = k + 1, w + widths[k + 1]
            if w < 22:  # too narrow for a large figure: widen the tile's last column
                L = get_column_letter(c0 + k)
                ws.column_dimensions[L].width += 22 - w
            spans.append((c0 + j, c0 + k))
            j = k + 1
        if len(spans) >= 2:
            _tiles(ws, 5, items[:len(spans)], spans)
            header = 10

    head_font = Font(name="Calibri", size=10, bold=True, color=INK)
    head_fill = PatternFill("solid", fgColor=TILE_FILL)
    numeric = ("money", "int", "decimal", "pct", "figures", "id")
    for c in cols:
        cell = ws.cell(header, col(c), c)
        cell.font, cell.fill = head_font, head_fill
        cell.border = Border(bottom=Side(style="medium", color=BRAND))
        cell.alignment = Alignment(horizontal="right" if kinds[c] in numeric else "left", vertical="center",
                                   wrap_text=True, indent=0 if kinds[c] in numeric else 1)
    ws.row_dimensions[header].height = 32

    line = Border(bottom=Side(style="thin", color=GRID))
    body = Font(name="Calibri", size=10, color=INK)
    tall = len(df) <= 3000  # a little air per row, unless the sheet is very long
    for i, row in enumerate(df.itertuples(index=False), start=header + 1):
        if tall:
            ws.row_dimensions[i].height = 19
        for c, v in zip(cols, row):
            if v is None or (not isinstance(v, (list, dict, str)) and pd.isna(v)):
                v = None
            elif isinstance(v, (list, dict)):
                v = str(v)
            elif isinstance(v, pd.Timestamp):
                v = v.to_pydatetime()
            cell = ws.cell(i, col(c), v)
            cell.font, cell.border = body, line
            cell.alignment = (Alignment(vertical="center") if kinds[c] in numeric
                              else Alignment(horizontal="left", vertical="center", indent=1))
            if kinds[c] in FORMATS:
                cell.number_format = FORMATS[kinds[c]]
            elif isinstance(v, float):  # figures in a mixed column (e.g. Key figures)
                cell.number_format = FORMATS["decimal"]
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif isinstance(v, int) and not isinstance(v, bool):
                cell.number_format = FORMATS["int"]
                cell.alignment = Alignment(horizontal="right", vertical="center")
    last = header + max(len(df), 1)
    if df.empty:
        ws.cell(header + 1, c0, "(no rows)").font = Font(name="Calibri", italic=True, color=MUTED)

    first, end = get_column_letter(c0), get_column_letter(c0 + len(cols) - 1)
    if len(df):
        # A real Excel table (filter buttons, sorting); our own lines and fills replace its colours.
        table = Table(displayName="T_" + re.sub(r"\W", "_", title)[:20] + f"_{n}", ref=f"{first}{header}:{end}{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
        ws.add_table(table)
        # A soft in-cell bar for the main amount, so the big ones stand out at a glance.
        for c in bars:
            if c in cols and len(df) > 1:
                L = get_column_letter(col(c))
                ws.conditional_formatting.add(f"{L}{header + 1}:{L}{last}", DataBarRule(
                    start_type="num", start_value=0, end_type="max", color="BBD5F5", showValue=True))
        # Status words in colour (with the word itself, never colour alone), and they follow when people sort.
        if "Status" in cols:
            L = get_column_letter(col("Status"))
            for word, (ink, bg) in STATUS.items():
                ws.conditional_formatting.add(f"{L}{header + 1}:{L}{last}", CellIsRule(
                    operator="equal", formula=[f'"{word}"'], font=Font(color=ink, bold=word == "Overdue"),
                    fill=PatternFill("solid", fgColor=bg, bgColor=bg) if bg else None))

    # Totals for money columns (formulas, so they stay right if someone edits a figure).
    totals = money_cols if len(df) > 1 else []
    if totals:
        t = last + 1
        ws.row_dimensions[t].height = 22
        for j in range(c0, c0 + len(cols)):
            cell = ws.cell(t, j)
            cell.fill = head_fill
            cell.border = Border(top=Side(style="thin", color=INK))
            cell.font = Font(name="Calibri", size=10, bold=True, color=INK)
            cell.alignment = Alignment(vertical="center", indent=1)
        ws.cell(t, c0, "Total")
        for c in totals:
            L = get_column_letter(col(c))
            cell = ws.cell(t, col(c), f"=SUBTOTAL(109,{L}{header + 1}:{L}{last})")
            cell.number_format = FORMATS["money"]
            cell.alignment = Alignment(vertical="center")

    for r in (1, 2, 3):
        ws.cell(r, c0).alignment = Alignment(vertical="center")
    ws.freeze_panes = ws.cell(header + 1, 1)
    ws.print_title_rows = f"{header}:{header}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddHeader.left.text = f"{COMPANY_NAME} · {title}"
    ws.oddFooter.left.text = "Confidential · " + COMPANY_NAME
    ws.oddFooter.right.text = "Page &P of &N"
    return {"ws": ws, "header": header, "last": last, "cols": cols, "c0": c0}


_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
COMPACT = '[>=1000000]#,##0.00,,"M";[>=1000]#,##0.0,"K";#,##0'  # 12.9K, 1.23M: easy to read on a chart
AXIS_FMT = '[>=1000000]#,##0.0,,"M";[>=1000]#,##0,"K";#,##0'  # 0, 100K, 200K


def _no_auto_title(ch: BarChart) -> BarChart:
    """Stop Excel's automatic title (a one-series chart otherwise repeats its series name on top).

    Done on the written XML: openpyxl has no setting for it, and a BarChart subclass loses its series when saved."""
    write = ch._write

    def _write():
        tree = write()
        for tag in ("chart", _C + "chart"):
            chart = tree.find(tag)
            if chart is not None:
                chart.insert(0, Element(tag.replace("chart", "autoTitleDeleted"), val="1"))
                break
        for el in tree.iter():  # our label formats, not the cells' (openpyxl leaves this out, so 1,228,841.75 shows)
            if el.tag.endswith("numFmt") and el.get("sourceLinked") is None:
                el.set("sourceLinked", "0")
        return tree

    ch._write = _write
    return ch


def _text(size: int = 9, color: str = MUTED, bold: bool = False) -> RichText:
    cp = CharacterProperties(sz=size * 100, b=bold, solidFill=color, latin=DrawingFont(typeface="Calibri"))
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def _line(color: str | None = None) -> GraphicalProperties:
    return GraphicalProperties(ln=LineProperties(solidFill=color, w=9525) if color else LineProperties(noFill=True))


def _chart(info: dict, spec: dict, width: float, height: float) -> BarChart | None:
    """One quiet single-series chart: one colour, no border, faint grid, only the labels people need."""
    ws, cols = info["ws"], info["cols"]
    if spec["category"] not in cols or spec["value"] not in cols or info["last"] <= info["header"]:
        return None
    rows = min(info["last"], info["header"] + spec.get("top", 1000))
    bar = spec.get("type") == "bar"
    ch = _no_auto_title(BarChart())
    ch.type = "bar" if bar else "col"
    ch.legend, ch.roundedCorners, ch.width, ch.height = None, False, width, height
    ch.graphical_properties = _line()  # no box around the chart
    vcol, ccol = info["c0"] + cols.index(spec["value"]), info["c0"] + cols.index(spec["category"])
    ch.add_data(Reference(ws, min_col=vcol, min_row=info["header"], max_row=rows), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=ccol, min_row=info["header"] + 1, max_row=rows))
    s = ch.series[0]
    s.graphicalProperties = GraphicalProperties(solidFill=BRAND, ln=LineProperties(noFill=True))
    s.invertIfNegative = False
    ch.gapWidth = 70 if bar else 60

    x, y = ch.x_axis, ch.y_axis  # x = the names (months, customers, items), y = the amounts
    x.delete = False
    x.majorTickMark = y.majorTickMark = "none"
    x.tickLblPos = "low"  # names stay clear of bars below zero
    x.txPr = _text(9, INK if bar else MUTED)
    x.graphicalProperties = _line(AXIS)
    x.title = y.title = None
    labels = DataLabelList(showVal=False, showSerName=False, showCatName=False, showLegendKey=False, showPercent=False)
    if bar:
        # Few bars, each with its amount at the tip: the amount axis and grid would only repeat them.
        x.scaling.orientation = "maxMin"  # biggest at the top
        y.delete = True
        y.majorGridlines = None
        labels.showVal, labels.numFmt, labels.position, labels.txPr = True, COMPACT, "outEnd", _text(9, INK)
    else:
        # Many columns: a light amount axis, and only the best month labelled.
        y.delete = False
        y.numFmt = AXIS_FMT
        y.txPr = _text(9, MUTED)
        y.graphicalProperties = _line()
        y.majorGridlines = ChartLines(spPr=_line(GRID))
        values = [ws.cell(r, vcol).value for r in range(info["header"] + 1, rows + 1)]
        nums = [v if isinstance(v, (int, float)) else float("-inf") for v in values]
        if nums and max(nums) > 0:
            labels.dLbl = [DataLabel(idx=nums.index(max(nums)), showVal=True, showSerName=False, showCatName=False,
                                     showLegendKey=False, showPercent=False, numFmt=COMPACT, dLblPos="outEnd",
                                     txPr=_text(9, INK, bold=True))]
    s.dLbls = labels
    return ch


def _summary(wb: Workbook, report: str, subtitle: str, source: str, summary: dict, infos: dict) -> None:
    """Front sheet: the headline figures as large tiles, then the charts on a tidy grid, nothing else."""
    ws = wb.create_sheet("Summary", 0)
    ws.sheet_properties.tabColor = TAB
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 100
    # Grid: a narrow margin, then 4 blocks of 3 columns with narrow gaps (each block is one tile, or half a chart).
    for j in range(1, 18):
        ws.column_dimensions[get_column_letter(j)].width = 2.5 if j in (1, 5, 9, 13, 17) else 11
    block = [2, 6, 10, 14]  # B, F, J, N
    cm = lambda chars: chars * 7.3 * 0.02646 + 0.1  # Excel column width (characters) to centimetres, roughly
    full, half = cm(12 * 11 + 3 * 2.5), cm(6 * 11 + 2.5)

    ws.cell(1, 2, report).font = Font(name="Calibri", size=18, bold=True, color=INK)
    ws.cell(2, 2, f"{COMPANY_NAME} · {subtitle}" if subtitle else COMPANY_NAME).font = Font(name="Calibri", size=11, color=MUTED)
    ws.cell(3, 2, source).font = Font(name="Calibri", size=9, italic=True, color=MUTED)
    ws.row_dimensions[1].height = 28

    fill = PatternFill("solid", fgColor=TILE_FILL)
    for b, tile in zip(block, summary.get("tiles", [])[:4]):
        for r in (5, 6, 7, 8):
            for j in range(b, b + 3):
                ws.cell(r, j).fill = fill
        for j in range(b, b + 3):
            ws.cell(5, j).border = Border(top=Side(style="thick", color=BRAND))
        pad = Alignment(indent=1, vertical="center")
        cells = [(6, tile["label"], Font(name="Calibri", size=10, color=MUTED)),
                 (7, tile["value"], Font(name="Calibri", size=20, bold=True, color=INK)),
                 (8, tile.get("note", ""), Font(name="Calibri", size=9, color=MUTED))]
        for r, v, f in cells:
            ws.merge_cells(start_row=r, start_column=b, end_row=r, end_column=b + 2)
            c = ws.cell(r, b, v)
            c.font, c.alignment = f, pad
            if isinstance(v, (int, float)):
                c.number_format = "#,##0"  # whole numbers read faster; exact figures are on the Key figures sheet
                c.alignment = Alignment(indent=1, vertical="center", horizontal="left")
    ws.row_dimensions[5].height = 6
    ws.row_dimensions[7].height = 34
    ws.row_dimensions[8].height = 18

    row, slot = 11, 0  # slot 0 = left half / full width, 1 = right half
    row_height_cm = 15 * 0.0353
    pending = 0.0
    for spec in summary.get("charts", []):
        info = infos.get(spec["sheet"])
        if not info:
            continue
        wide = spec.get("wide", False)
        if wide and slot == 1:
            row, slot = row + pending, 0
        n = min(info["last"], info["header"] + spec.get("top", 1000)) - info["header"]
        height = 7.5 if not spec.get("type") == "bar" else max(2.4, 0.62 * n + 0.8)  # bars keep one thickness
        ch = _chart(info, spec, full if wide else half, height)
        if ch is None:
            continue
        col = block[0] if slot == 0 else block[2]
        ws.cell(row, col, spec["title"]).font = Font(name="Calibri", size=12, bold=True, color=INK)
        if spec.get("subtitle"):
            ws.cell(row + 1, col, spec["subtitle"]).font = Font(name="Calibri", size=9, color=MUTED)
        ws.add_chart(ch, f"{get_column_letter(col)}{row + 2}")
        used = 2 + int(height / row_height_cm) + 2
        if wide or slot == 1:
            row, slot, pending = row + max(used, pending), 0, 0
        else:
            slot, pending = 1, used
    row += pending
    if summary.get("note"):
        ws.cell(row, 2, summary["note"]).font = Font(name="Calibri", size=9, italic=True, color=MUTED)

    ws.print_area = f"A1:Q{row + 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddFooter.left.text = "Confidential · " + COMPANY_NAME
    ws.oddFooter.right.text = "Page &P of &N"


def excel_link(filename: str, sheets: dict[str, pd.DataFrame], money_cols: tuple[str, ...] = (), *,
               title: str = "", subtitle: str = "", summary: dict | None = None,
               bars: dict[str, tuple[str, ...]] | None = None) -> str:
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
        infos[sheet_title] = _sheet(wb, sheet, _typed(df), report, subtitle, source, money, n + bool(summary),
                                     (bars or {}).get(sheet_title, ()))
    if summary:
        _summary(wb, report, subtitle, source, summary, infos)
    wb.save(folder / name)
    return f"📥 **[Download Excel: {name}]({URL_PREFIX}/{folder.name}/{name})** (link works for {KEEP_HOURS:g} hours)"
