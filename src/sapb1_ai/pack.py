"""Management pack: every dashboard the person's role may see, as one PDF (charts + key figures, ready to print or
send) and one Excel summary of the headline figures. Built from the same live dashboards as the chat."""

import base64
import datetime as dt
import io
import logging
import re
import secrets

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

from . import access
from .analytics import (SKIP_EXCEL, compare_periods, customer_insights, delivery_performance, inventory_health,
                        product_profitability, profit_and_loss, quotation_pipeline, salesperson_performance)

from .export import COMPANY_NAME, EXPORT_DIR, KEEP_HOURS, URL_PREFIX, _safe_name, cleanup, excel_link
from .sales_forecast import sales_forecast

log = logging.getLogger("uvicorn.error")
TEXT, MUTED = "#1d2d3e", "#556b82"

SECTIONS = [  # (tool name for the role check, title, function, arguments)
    ("compare_periods", "Last month vs the month before", compare_periods, {"period": "month", "which": "last"}),
    ("compare_periods", "Year to date vs last year", compare_periods, {"period": "year", "which": "this", "against": "last_year"}),
    ("profit_and_loss", "Profit and loss", profit_and_loss, {"months": 12}),
    ("quotation_pipeline", "Quotation pipeline", quotation_pipeline, {"months": 6}),
    ("customer_insights", "Customer insights", customer_insights, {"months": 12}),
    ("product_profitability", "Product profitability", product_profitability, {"months": 12}),
    ("salesperson_performance", "Salesperson performance", salesperson_performance, {"months": 3}),
    ("delivery_performance", "Delivery performance", delivery_performance, {"months": 6}),
    ("inventory_health", "Inventory health", inventory_health, {}),
    ("sales_forecast", "Sales forecast", sales_forecast, {"months_ahead": 3}),
]


def _parts(display: str) -> tuple[str, str, list[list[str]], list[bytes]]:
    """Title, subtitle, first table (rows of cells) and chart images from a dashboard's chat output."""
    images = [base64.b64decode(b) for b in re.findall(r"\(data:image/png;base64,([^)]*)\)", display)]
    lines = [l for l in display.splitlines() if not l.startswith(("📥", "![")) and l.strip()]
    title = next((l[3:] for l in lines if l.startswith("## ")), "")
    sub = next((l for l in lines if not l.startswith(("#", "|"))), "")
    table, started = [], False
    for l in display.splitlines():
        if l.startswith("|"):
            started = True
            if not re.match(r"^\|[\s:|-]+\|$", l):
                table.append([c.strip().replace("**", "") for c in l.strip("|").split("|")])
        elif started:
            break
    return title, sub, table[:12], images


def _page_title(fig, title: str, sub: str) -> None:
    fig.text(0.04, 0.95, title, fontsize=16, fontweight="bold", color=TEXT, va="top")
    fig.text(0.04, 0.905, sub[:160], fontsize=9.5, color=MUTED, va="top")


def _add_table(fig, rect, rows: list[list[str]]) -> None:
    if not rows:
        return
    ax = fig.add_axes(rect)
    ax.axis("off")
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    t = ax.table(cellText=rows[1:] or [[""] * width], colLabels=rows[0], loc="upper left", cellLoc="left", colLoc="left")
    t.auto_set_font_size(False)
    t.set_fontsize(8.5)
    t.scale(1, 1.35)
    for (r, c), cell in t.get_celld().items():
        cell.set_edgecolor("#e4e8ec")
        if r == 0:
            cell.set_text_props(fontweight="bold", color=TEXT)
            cell.set_facecolor("#f5f6f7")


def _add_image(fig, rect, png: bytes) -> None:
    ax = fig.add_axes(rect)
    ax.imshow(mpimg.imread(io.BytesIO(png), format="png"))
    ax.axis("off")


def management_pack() -> dict:
    """Run every dashboard the person may see and put them into one PDF and one Excel summary."""
    who = access.current()
    today = dt.date.today()
    token = SKIP_EXCEL.set(True)  # the pack makes its own files, not one per dashboard
    done, skipped, summary = [], [], []
    try:
        for tool, title, fn, args in SECTIONS:
            if access.allowed(tool, args):
                skipped.append(title)
                continue
            try:
                r = fn(**args)
            except Exception:
                log.exception("Management pack: %s failed", title)
                skipped.append(title + " (failed)")
                continue
            if "_display" not in r:
                skipped.append(f"{title} ({r.get('note') or r.get('error') or 'no data'})"[:120])
                continue
            done.append((title, r))
            for k, v in r.items():
                if k in ("_display", "instruction") or isinstance(v, (list, dict)):
                    continue
                summary.append({"Section": title, "Figure": k.replace("_", " "), "Value": v})
    finally:
        SKIP_EXCEL.reset(token)
    if not done:
        return {"error": "None of the dashboards had data for this pack."}

    cleanup()
    folder = EXPORT_DIR / secrets.token_urlsafe(24)
    folder.mkdir(parents=True)
    name = _safe_name(f"Management_pack_{today:%Y%m%d}") + ".pdf"
    with PdfPages(folder / name) as pdf:
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.text(0.06, 0.80, "Management pack", fontsize=30, fontweight="bold", color=TEXT)
        fig.text(0.06, 0.74, f"{COMPANY_NAME} · {today:%d %B %Y}", fontsize=14, color=MUTED)
        fig.text(0.06, 0.70, "Live from SAP Business One (read-only) · SAP B1 AI", fontsize=10, color=MUTED)
        y = 0.60
        for i, (title, _) in enumerate(done, 1):
            fig.text(0.08, y, f"{i}.  {title}", fontsize=12, color=TEXT)
            y -= 0.04
        if skipped:
            fig.text(0.06, max(y - 0.03, 0.05), "Not included: " + "; ".join(skipped), fontsize=8.5, color=MUTED, wrap=True)
        pdf.savefig(fig)
        plt.close(fig)
        for title, r in done:
            head, sub, table, images = _parts(r["_display"])
            fig = plt.figure(figsize=(11.69, 8.27))
            _page_title(fig, head or title, sub)
            if images:
                _add_table(fig, [0.04, 0.10, 0.40, 0.76], table)
                _add_image(fig, [0.46, 0.42, 0.52, 0.46], images[0])
                if len(images) > 1:
                    _add_image(fig, [0.46, 0.02, 0.52, 0.38], images[1])
            else:
                _add_table(fig, [0.04, 0.10, 0.92, 0.76], table)
            pdf.savefig(fig)
            plt.close(fig)
            for i in range(2, len(images), 2):  # more charts: two per page
                fig = plt.figure(figsize=(11.69, 8.27))
                _page_title(fig, (head or title) + " (continued)", "")
                _add_image(fig, [0.04, 0.46, 0.92, 0.42], images[i])
                if i + 1 < len(images):
                    _add_image(fig, [0.04, 0.02, 0.92, 0.42], images[i + 1])
                pdf.savefig(fig)
                plt.close(fig)
    pdf_link = f"📄 **[Download PDF: {name}]({URL_PREFIX}/{folder.name}/{name})** (link works for {KEEP_HOURS:g} hours)"
    xl = excel_link(f"Management_pack_{today:%Y%m%d}", {"Summary": pd.DataFrame(summary)}, title="Management pack",
                    subtitle=f"{COMPANY_NAME} · {today:%d %b %Y} · headline figures of each dashboard")
    shown = [f"## Management pack: {today:%d %B %Y}", "", pdf_link, xl, "",
             "Contains: " + ", ".join(t for t, _ in done) + "."]
    if skipped:
        shown.append("Not included: " + "; ".join(skipped) + ".")
    return {"_display": "\n".join(shown) + "\n", "sections": [t for t, _ in done], "not_included": skipped,
            "for": who.get("name") or "",
            "instruction": "The PDF and Excel links are already shown. Say in one or two sentences what the pack contains; "
                           "do not repeat figures or write links yourself."}
