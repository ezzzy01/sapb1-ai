"""Creating documents in B1 as DRAFTS (Joule-style "transactional"), only after the user confirms.

Two steps, so nothing reaches SAP by accident:
  1. prepare_b1_draft checks everything against B1 (read-only SQL): partner active, items valid for sale/purchase,
     price (given, else the partner's price list / last price), warehouse and stock. It shows a preview and a one-time
     approval ref, kept here (30 minutes, single use). The user approves by replying yes to the preview.
  2. create_b1_draft creates the B1 DRAFT document through the Service Layer, but only when the user's own latest
     message is a plain yes to that preview, or "confirm <ref>" (checked by the chat service, not by the AI model). A draft is not a posted document:
     a person opens it in B1, checks it and adds it.

Off unless the company's company.env sets B1_DRAFTS_ENABLED=yes with its own Service Layer user (B1_SL_URL,
B1_SL_USER, B1_SL_PASSWORD; B1_SL_COMPANYDB defaults to SAPB1_DB). Never for the client's live database (BLOCKED_DB_HASHES). Every draft is logged
in data/<B1 database>/b1_drafts.log.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta

import httpx
import pandas as pd

from . import access
from .db import query
from .numbers import label, money, qty
from .summaries import _find_partner
from .sync import DATA_DIR

KINDS = {  # kind -> (B1 draft object, name, partner type C/S, which way stock moves: -1 out, +1 in, 0 none)
    "sales_quotation": ("oQuotations", "Sales Quotation", "C", 0),
    "sales_order": ("oOrders", "Sales Order", "C", 0),
    "delivery": ("oDeliveryNotes", "Delivery", "C", -1),
    "ar_invoice": ("oInvoices", "A/R Invoice", "C", -1),
    "ar_credit_memo": ("oCreditNotes", "A/R Credit Memo", "C", +1),
    "return": ("oReturns", "Return", "C", +1),
    "purchase_order": ("oPurchaseOrders", "Purchase Order", "S", 0),
    "goods_receipt_po": ("oPurchaseDeliveryNotes", "Goods Receipt PO", "S", +1),
    "ap_invoice": ("oPurchaseInvoices", "A/P Invoice", "S", +1),
    "ap_credit_memo": ("oPurchaseCreditNotes", "A/P Credit Memo", "S", -1),
    "goods_return": ("oPurchaseReturns", "Goods Return", "S", -1),
    "ar_down_payment": ("oDownPayments", "A/R Down Payment Invoice", "C", 0),
    "ar_reserve_invoice": ("oInvoices", "A/R Reserve Invoice", "C", 0),
    "return_request": ("oReturnRequest", "Return Request", "C", 0),
    "purchase_request": ("oPurchaseRequest", "Purchase Request", None, 0),
    "purchase_quotation": ("oPurchaseQuotations", "Purchase Quotation", "S", 0),
    "ap_down_payment": ("oPurchaseDownPayments", "A/P Down Payment Invoice", "S", 0),
    "goods_return_request": ("oGoodsReturnRequest", "Goods Return Request", "S", 0),
    "goods_receipt": ("oInventoryGenEntry", "Goods Receipt", None, +1),
    "goods_issue": ("oInventoryGenExit", "Goods Issue", None, -1),
}
EXTRA = {"ar_down_payment": {"DownPaymentType": "dptInvoice"}, "ap_down_payment": {"DownPaymentType": "dptInvoice"},
         "ar_reserve_invoice": {"ReserveInvoice": "tYES"}}
NO_PARTNER_WORD = {"purchase_request": "Purchase request", "goods_receipt": "Stock coming in", "goods_issue": "Stock going out"}
# Like B1's "Copy From": which earlier documents each kind can be made from (open lines only, keeping their prices).
COPY_FROM = {
    "sales_order": ["sales_quotation"], "delivery": ["sales_order"], "ar_invoice": ["delivery", "sales_order"],
    "ar_credit_memo": ["ar_invoice", "return"], "return": ["delivery"],
    "goods_receipt_po": ["purchase_order"], "ap_invoice": ["goods_receipt_po", "purchase_order"],
    "ap_credit_memo": ["ap_invoice", "goods_return"], "goods_return": ["goods_receipt_po", "goods_return_request"],
    "ar_down_payment": ["sales_order"], "ar_reserve_invoice": ["sales_order"], "return_request": ["delivery", "ar_invoice"],
    "purchase_quotation": ["purchase_request"], "ap_down_payment": ["purchase_order"],
    "goods_return_request": ["goods_receipt_po", "ap_invoice"],
}
COPY_FROM["return"] = ["delivery", "return_request"]
COPY_FROM["purchase_order"] = ["purchase_request", "purchase_quotation"]
ALIASES = {"pesanan jualan": "sales_order", "pesanan belian": "purchase_order", "sebut harga": "sales_quotation",
           "penghantaran": "delivery", "invois": "ar_invoice", "nota kredit": "ar_credit_memo", "pemulangan": "return",
           "return": "return", "sales return": "return", "goods return": "goods_return", "purchase return": "goods_return",
           "goods receipt po": "goods_receipt_po", "a/p credit memo": "ap_credit_memo", "ap credit memo": "ap_credit_memo",
           "ap invoice": "ap_invoice", "ar invoice": "ar_invoice", "ar credit memo": "ar_credit_memo",
           "down payment": "ar_down_payment", "deposit invoice": "ar_down_payment", "reserve invoice": "ar_reserve_invoice",
           "return request": "return_request", "purchase request": "purchase_request", "pr": "purchase_request",
           "purchase quotation": "purchase_quotation", "rfq": "purchase_quotation", "supplier quotation": "purchase_quotation",
           "ap down payment": "ap_down_payment", "supplier down payment": "ap_down_payment",
           "goods return request": "goods_return_request", "goods receipt": "goods_receipt", "stock in": "goods_receipt",
           "goods issue": "goods_issue", "stock out": "goods_issue", "write off": "goods_issue",
           "permintaan belian": "purchase_request", "terimaan barang": "goods_receipt", "keluaran barang": "goods_issue"}
# Live databases where drafts are never created, whatever company.env says. Kept as SHA-256 of the upper-case
# database name, so the public code does not name the client.
BLOCKED_DB_HASHES = {"4d05944824d5546648b210062a6d903e9ab2f2fed7628832a9e53d3a373b4ba9"}
CODE_MINUTES = 30
PENDING_FILE = DATA_DIR / "b1_drafts_pending.json"
LOG_FILE = DATA_DIR / "b1_drafts.log"
_lock = threading.Lock()


def _enabled() -> tuple[bool, str]:
    db = os.getenv("B1_SL_COMPANYDB") or os.getenv("SAPB1_DB", "")
    if hashlib.sha256(db.strip().upper().encode()).hexdigest() in BLOCKED_DB_HASHES:
        return False, f"Creating drafts is blocked for {db}."
    if os.getenv("B1_DRAFTS_ENABLED", "").lower() not in ("1", "yes", "true", "on"):
        return False, "Creating drafts in B1 is not switched on for this company."
    if not all(os.getenv(k) for k in ("B1_SL_URL", "B1_SL_USER", "B1_SL_PASSWORD")):
        return False, "Creating drafts is switched on, but the Service Layer login is not set in company.env."
    return True, ""


def _kind(text: str) -> str | None:
    from .summaries import _kind as doc_kind
    t = (text or "").strip().lower().replace("_", " ").replace("-", " ")
    k = next((k for k in KINDS if t == k.replace("_", " ")), None) or ALIASES.get(t) or doc_kind(t)
    return k if k in KINDS else None


def _base(text: str, kind: str) -> tuple[dict | None, str]:
    """The earlier document to copy from, e.g. 'sales order 1186' or just '1186': (header + open lines, problem)."""
    from .summaries import DOCS, _kind as doc_kind
    m = re.match(r"\s*(.*?)\s*(?:no\.?|number|#)?\s*(\d+)\s*$", text or "", re.I)
    if not m:
        return None, f"Give the document to copy from as type and number, e.g. '{COPY_FROM[kind][0].replace('_', ' ')} 1186'."
    allowed = COPY_FROM.get(kind, [])
    base_kind = doc_kind(m.group(1)) if m.group(1) else (allowed[0] if len(allowed) == 1 else None)
    if base_kind is None and allowed:
        base_kind = next((bk for bk in allowed if not query(f"SELECT 1 FROM {DOCS[bk][0]} WHERE DocNum = ? AND DocStatus = 'O'",
                                                              (int(m.group(2)),)).empty), None)
    if base_kind not in allowed:
        names = " or ".join(a.replace("_", " ") for a in allowed) or "nothing"
        return None, f"A {KINDS[kind][1]} can be copied from: {names}."
    head, lines_t, objtype = DOCS[base_kind][0], DOCS[base_kind][1], DOCS[base_kind][2]
    h = query(f"SELECT DocEntry, DocNum, CardCode, CardName, DocStatus, CANCELED, DocCur, DocType, NumAtCard FROM {head} "
              "WHERE DocNum = ? ORDER BY DocEntry DESC", (int(m.group(2)),))
    if h.empty:
        return None, f"There is no {DOCS[base_kind][3]} {m.group(2)} in B1."
    h = h.iloc[0]
    if h.CANCELED != "N" or h.DocStatus != "O":
        return None, f"{DOCS[base_kind][3]} {h.DocNum} is {'cancelled' if h.CANCELED != 'N' else 'closed'}, so nothing can be copied from it."
    if h.DocType == "S":
        return None, f"{DOCS[base_kind][3]} {h.DocNum} is a service document (amounts only); copy it in B1 itself."
    lines = query(f"SELECT LineNum, ItemCode, Dscription, OpenQty, Price, WhsCode FROM {lines_t} "
                  "WHERE DocEntry = ? AND LineStatus = 'O' AND OpenQty > 0 ORDER BY LineNum", (int(h.DocEntry),))
    if lines.empty:
        return None, f"{DOCS[base_kind][3]} {h.DocNum} has no open lines left to copy."
    return {"kind": base_kind, "name": DOCS[base_kind][3], "objtype": objtype, "entry": int(h.DocEntry), "num": int(h.DocNum),
            "card": h.CardCode, "currency": h.DocCur, "ref": h.NumAtCard, "lines": lines}, ""


def _parse_items(text: str) -> list[tuple[str, float, float | None]]:
    """'A00001:5@400, PC Set 1 x 2' -> [('A00001', 5.0, 400.0), ('PC Set 1', 2.0, None)]"""
    import re
    out = []
    for part in re.split(r"[,;\n]+", text or ""):
        m = re.match(r"\s*(.+?)\s*(?:[:x×=]|\s)\s*([\d.]+)\s*(?:@\s*([\d.,]+))?\s*$", part, re.I)
        if m:
            out.append((m.group(1).strip(), float(m.group(2)), float(m.group(3).replace(",", "")) if m.group(3) else None))
    return out


def _pending() -> dict:
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    now = time.time()
    return {k: v for k, v in data.items() if v["expires"] > now}  # used ones stay until they expire, to say why


def _why_not_pending(code: str) -> str:
    """Why an approval ref can't be used: already used (draft made), expired, or unknown."""
    try:
        job = json.loads(PENDING_FILE.read_text()).get(code)
    except (OSError, ValueError):
        job = None
    if not job:
        return f"Approval ref {code} is unknown."
    if job.get("used"):
        made = f" (draft no. {job['draft_docentry']})" if job.get("draft_docentry") else ""
        return f"This preview was already approved{made}, so it is not created twice."
    return f"Approval ref {code} has expired ({CODE_MINUTES} minutes)."


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def approved_ref(previous_answer: str) -> str | None:
    """The approval ref of the preview the user just said yes to, but only if that preview is exactly the one
    prepare_b1_draft made (so a preview retyped or changed by the AI model can never be approved)."""
    refs = re.findall(r"Approval ref \*\*([A-HJ-NP-Z2-9]{6})\*\*", previous_answer or "")
    if not refs:
        return None
    job = _pending().get(refs[-1])
    if job and _squash(job.get("preview", "")) in _squash(previous_answer):
        return refs[-1]
    return None


def _save_pending(data: dict) -> None:
    PENDING_FILE.write_text(json.dumps(data, default=str))
    PENDING_FILE.chmod(0o600)


def _log(entry: dict) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), **entry}, default=str) + "\n")


def _price(kind: str, card: dict, item: str) -> tuple[float | None, str]:
    """Unit price when the user gave none: sales = the customer's price list, else the last price they paid;
    purchases = the last price paid to this supplier, else the item's last purchase price; stock in/out = item cost."""
    if KINDS[kind][2] is None:
        r = query("SELECT AvgPrice, LastPurPrc FROM OITM WHERE ItemCode = ?", (item,))
        if len(r) and r.AvgPrice.iloc[0]:
            return float(r.AvgPrice.iloc[0]), "item cost"
        if len(r) and r.LastPurPrc.iloc[0]:
            return float(r.LastPurPrc.iloc[0]), "item's last purchase price"
        return (0.0, "no cost in B1") if kind == "goods_issue" else (None, "no price found")
    if KINDS[kind][2] == "C":
        r = query("SELECT i.Price, i.Currency, l.ListName FROM ITM1 i JOIN OCRD p ON p.ListNum = i.PriceList "
                  "LEFT JOIN OPLN l ON l.ListNum = i.PriceList WHERE p.CardCode = ? AND i.ItemCode = ?", (card["CardCode"], item))
        if len(r) and r.Price.iloc[0]:
            return float(r.Price.iloc[0]), f"price list '{r.ListName.iloc[0]}'"
        r = query("SELECT TOP 1 l.Price FROM INV1 l JOIN OINV h ON h.DocEntry = l.DocEntry WHERE h.CardCode = ? AND "
                  "l.ItemCode = ? AND h.CANCELED = 'N' ORDER BY h.DocDate DESC, h.DocEntry DESC", (card["CardCode"], item))
        if len(r) and r.Price.iloc[0]:
            return float(r.Price.iloc[0]), "last price this customer paid"
        return None, "no price found"
    r = query("SELECT TOP 1 l.Price FROM PCH1 l JOIN OPCH h ON h.DocEntry = l.DocEntry WHERE h.CardCode = ? AND "
              "l.ItemCode = ? AND h.CANCELED = 'N' ORDER BY h.DocDate DESC, h.DocEntry DESC", (card["CardCode"], item))
    if len(r) and r.Price.iloc[0]:
        return float(r.Price.iloc[0]), "last price paid to this supplier"
    r = query("SELECT LastPurPrc FROM OITM WHERE ItemCode = ?", (item,))
    if len(r) and r.LastPurPrc.iloc[0]:
        return float(r.LastPurPrc.iloc[0]), "item's last purchase price"
    return None, "no price found"


MIN_MARGIN_PCT = float(os.getenv("MIN_MARGIN_PCT", "10"))  # price advice: warn below this margin


def _price_advice(card: dict, lines: list[dict], cur: str) -> tuple[list[str], list[str]]:
    """Per line: last price to this customer, what others paid (12 months), item cost and the margin at this price."""
    notes, warnings = [], []
    since = date.today() - timedelta(days=365)
    for l in lines:
        item, price = l["ItemCode"], float(l["UnitPrice"])
        last = query("SELECT TOP 1 l.Price, h.DocDate FROM INV1 l JOIN OINV h ON h.DocEntry = l.DocEntry WHERE h.CardCode = ? "
                     "AND l.ItemCode = ? AND h.CANCELED = 'N' ORDER BY h.DocDate DESC, h.DocEntry DESC", (card["CardCode"], item))
        others = query("SELECT SUM(l.LineTotal) / NULLIF(SUM(l.Quantity), 0) AS avg_price FROM INV1 l JOIN OINV h ON h.DocEntry = l.DocEntry "
                       "WHERE h.CardCode <> ? AND l.ItemCode = ? AND h.CANCELED = 'N' AND h.DocDate >= ?", (card["CardCode"], item, since))
        c = query("SELECT AvgPrice, LastPurPrc, InvntItem FROM OITM WHERE ItemCode = ?", (item,))
        cost = float(c.AvgPrice.iloc[0] or 0) or float(c.LastPurPrc.iloc[0] or 0) if len(c) else 0.0
        bits = []
        if len(last) and last.Price.iloc[0]:
            bits.append(f"last sold to them at {money(float(last.Price.iloc[0]), cur)} ({pd.Timestamp(last.DocDate.iloc[0]):%d %b %Y})")
        if len(others) and pd.notna(others.avg_price.iloc[0]):
            bits.append(f"others paid {money(float(others.avg_price.iloc[0]), cur)} on average (12 months)")
        if cost and price:
            margin = (price - cost) / price * 100
            bits.append(f"cost {money(cost, cur)}, margin at {money(price, cur)}: {margin:.1f}%")
            if price < cost:
                warnings.append(f"{l['ItemName']}: the price {money(price, cur)} is below cost ({money(cost, cur)})")
            elif margin < MIN_MARGIN_PCT:
                warnings.append(f"{l['ItemName']}: margin {margin:.1f}% is below the {MIN_MARGIN_PCT:g}% minimum")
        if bits:
            notes.append(f"{l['ItemCode']}: " + "; ".join(bits))
    return notes, warnings


def _credit_check(card: dict, total: float, cur: str) -> list[str]:
    """Customer over their credit limit with this document, or with invoices more than 60 days overdue."""
    out = []
    r = query("SELECT CreditLine, Balance, OrdersBal, DNotesBal FROM OCRD WHERE CardCode = ?", (card["CardCode"],))
    if len(r) and float(r.CreditLine.iloc[0] or 0) > 0:
        limit = float(r.CreditLine.iloc[0])
        used = float(r.Balance.iloc[0] or 0) + float(r.OrdersBal.iloc[0] or 0) + float(r.DNotesBal.iloc[0] or 0)
        if used + total > limit:
            out.append(f"Credit limit: {card['CardName']} would be at {money(used + total, cur)} against a limit of "
                       f"{money(limit, cur)} (over by {money(used + total - limit, cur)})")
    od = query("SELECT COUNT(*) AS n, SUM(DocTotal - PaidToDate) AS amt, MAX(DATEDIFF(day, DocDueDate, GETDATE())) AS days "
               "FROM OINV WHERE CardCode = ? AND DocStatus = 'O' AND CANCELED = 'N' AND DocDueDate < DATEADD(day, -60, GETDATE())",
               (card["CardCode"],))
    if len(od) and int(od.n.iloc[0] or 0):
        out.append(f"{card['CardName']} has {int(od.n.iloc[0])} invoice(s) more than 60 days overdue "
                   f"({money(float(od.amt.iloc[0] or 0), cur)}, oldest {int(od.days.iloc[0]):,} days)")
    return out


def prepare_b1_draft(kind: str, partner: str = "", items: str = "", due_date: str = "", reference: str = "",
                     remarks: str = "", copy_from: str = "", warehouse: str = "") -> dict:
    """Check a document against B1 and show it; the user approves it by replying yes. With copy_from it is filled from
    an earlier document's open lines, like B1's Copy From (e.g. a delivery from sales order 1186)."""
    k = _kind(kind)
    if not k:
        return {"error": "Drafts can be: " + ", ".join(KINDS) + "."}
    if k == "goods_receipt" and copy_from:
        k = "goods_receipt_po"  # "goods receipt for PO 340" means a goods receipt PO
    obj, name, card_type, stock_move = KINDS[k]
    who = access.current()
    if not access.can_draft(k, who):
        return {"error": f"Not allowed: creating a {name} is not part of the {who['role']} role.",
                "note": "Tell the user plainly; an administrator can change their access. Do not prepare it another way."}
    today = pd.Timestamp(date.today())
    lines, problems, warnings, base = [], [], [], None

    if copy_from:
        if k not in COPY_FROM:
            return {"error": f"A {name} cannot be copied from another document; give the partner and items instead."}
        base, problem = _base(copy_from, k)
        if problem:
            return {"error": problem}
        if card_type is None:
            card = {"CardCode": "", "CardName": NO_PARTNER_WORD.get(k, name)}
        elif not base["card"]:  # e.g. a purchase request has no supplier yet
            if not partner:
                return {"error": f"{base['name']} {base['num']} has no supplier; say which supplier it is for."}
            found = _find_partner(partner, card_type)
            if "match" not in found:
                return found
            card = found["match"]
        else:
            found = _find_partner(base["card"], card_type)
            if "match" not in found:
                return {"error": f"The partner {base['card']} of {base['name']} {base['num']} was not found as a "
                                 f"{'customer' if card_type == 'C' else 'supplier'}."}
            card = found["match"]
        if base["card"] and partner and partner.strip().lower() not in (card["CardCode"].lower(), card["CardName"].lower()) \
                and partner.strip().lower() not in card["CardName"].lower():
            return {"error": f"{base['name']} {base['num']} belongs to {card['CardName']} ({card['CardCode']}), not {partner}."}
        cur = label(base["currency"] or query("SELECT MainCurncy FROM OADM").MainCurncy.iloc[0])
        wanted = _parse_items(items) if items else []
        open_lines = base["lines"]
        for text, q, given in wanted or [(None, None, None)]:
            if text is None:
                chosen = [(r, float(r.OpenQty)) for _, r in open_lines.iterrows()]
            else:
                hit = open_lines[(open_lines.ItemCode.str.lower() == text.lower()) |
                                 open_lines.Dscription.str.lower().str.contains(re.escape(text.lower()))]
                if hit.empty:
                    problems.append(f"{text}: not an open line of {base['name']} {base['num']}")
                    continue
                r = hit.iloc[0]
                if q > float(r.OpenQty):
                    problems.append(f"{r.ItemCode}: only {qty(float(r.OpenQty))} still open on {base['name']} {base['num']}")
                    continue
                if given is not None:
                    warnings.append(f"{r.ItemCode}: the price is copied from {base['name']} {base['num']}; change it in B1 if needed")
                chosen = [(r, q)]
            for r, q2 in chosen:
                lines.append({"ItemCode": r.ItemCode, "ItemName": r.Dscription, "Quantity": q2, "UnitPrice": float(r.Price),
                              "WarehouseCode": r.WhsCode, "source": f"{base['name']} {base['num']}",
                              "BaseType": base["objtype"], "BaseEntry": base["entry"], "BaseLine": int(r.LineNum)})
    else:
        if card_type is None:
            card = {"CardCode": "", "CardName": NO_PARTNER_WORD.get(k, name)}
        elif not partner:
            return {"error": f"Who is the {'customer' if card_type == 'C' else 'supplier'}? Or give copy_from (e.g. 'sales order 1186')."}
        else:
            found = _find_partner(partner, card_type)
            if "match" not in found:
                return found
            card = found["match"]
        lines_in = _parse_items(items)
        if not lines_in:
            return {"error": "Give the items as code (or name) and quantity, e.g. items='A00001:5, PC Set 1:2' (add @price to set a price)"
                             + (", or copy_from an earlier document." if k in COPY_FROM else ".")}
        cur = label(query("SELECT MainCurncy FROM OADM").MainCurncy.iloc[0])
        whs_override = ""
        if warehouse:
            w = query("SELECT WhsCode FROM OWHS WHERE (WhsCode = ? OR WhsName LIKE ?) AND Inactive = 'N'", (warehouse, f"%{warehouse}%"))
            if len(w) != 1:
                return {"error": f"Warehouse '{warehouse}': " + ("not found" if w.empty else "several match (" + ", ".join(w.WhsCode) + ")")}
            whs_override = w.WhsCode.iloc[0]
        for text, q, given in lines_in:
            if q <= 0:
                problems.append(f"{text}: quantity must be more than 0")
                continue
            it = query("SELECT ItemCode, ItemName, SellItem, PrchseItem, InvntItem, DfltWH, validFor, frozenFor FROM OITM WHERE ItemCode = ?", (text,))
            if it.empty:
                it = query("SELECT TOP 3 ItemCode, ItemName, SellItem, PrchseItem, InvntItem, DfltWH, validFor, frozenFor FROM OITM "
                           "WHERE ItemName LIKE ?", (f"%{text}%",))
            if len(it) != 1:
                problems.append(f"{text}: " + ("no such item" if it.empty else "several items match (" + ", ".join(it.ItemCode) + "); use the code"))
                continue
            it = it.iloc[0]
            if it.frozenFor == "Y" or it.validFor == "N":
                problems.append(f"{it.ItemCode}: inactive in B1")
                continue
            if (card_type == "C" and it.SellItem != "Y") or ((card_type == "S" or k == "purchase_request") and it.PrchseItem != "Y"):
                problems.append(f"{it.ItemCode}: not a {'sales' if card_type == 'C' else 'purchase'} item in B1")
                continue
            if k in ("goods_receipt", "goods_issue") and it.InvntItem != "Y":
                problems.append(f"{it.ItemCode}: not a stock item, so it cannot be received or issued")
                continue
            price, source = (given, "price given") if given is not None else _price(k, card, it.ItemCode)
            if price is None:
                problems.append(f"{it.ItemCode}: no price found; give one, e.g. {it.ItemCode}:{q:g}@123.45")
                continue
            lines.append({"ItemCode": it.ItemCode, "ItemName": it.ItemName, "Quantity": q, "UnitPrice": round(price, 6),
                          "WarehouseCode": whs_override or it.DfltWH or "01", "source": source})
    if card.get("frozenFor") == "Y":
        return {"error": f"{card['CardName']} is inactive in B1; activate it first."}
    if problems:
        return {"error": "Fix these before a draft can be prepared: " + "; ".join(problems)}

    # Stock: an order promises stock (free = on hand - committed); a delivery, invoice without delivery, or return to a
    # supplier takes it out (on hand). Copying an invoice from a delivery moves nothing.
    moves_out = stock_move < 0 and not (base and base["kind"] in ("delivery", "goods_return"))
    for l in lines:
        if k == "sales_order" or moves_out:
            s = query("SELECT w.OnHand, w.IsCommited FROM OITW w JOIN OITM i ON i.ItemCode = w.ItemCode "
                      "WHERE w.ItemCode = ? AND w.WhsCode = ? AND i.InvntItem = 'Y'", (l["ItemCode"], l["WarehouseCode"]))
            if s.empty:
                continue
            avail = float(s.OnHand.iloc[0]) - (float(s.IsCommited.iloc[0]) if k == "sales_order" else 0)
            if avail < l["Quantity"]:
                what = "available" if k == "sales_order" else "in stock"
                warnings.append(f"{l['ItemName']}: only {qty(max(avail, 0))} {what} in warehouse {l['WarehouseCode']} for {qty(l['Quantity'])}")

    try:
        due = pd.Timestamp(due_date) if due_date else None
    except ValueError:
        return {"error": f"'{due_date}' is not a date; use YYYY-MM-DD."}
    if due is not None and due < today:
        return {"error": "The date is in the past."}
    if k == "sales_quotation":
        due, due_word = due or today + timedelta(days=30), "Valid until"
    elif k in ("sales_order", "purchase_order"):
        due, due_word = due or today + timedelta(days=7), "Delivery date"
    elif k in ("purchase_request", "purchase_quotation"):
        due, due_word = due or today + timedelta(days=7), "Required by"
    elif k in ("ar_invoice", "ap_invoice", "ar_credit_memo", "ap_credit_memo", "ar_down_payment", "ap_down_payment",
               "ar_reserve_invoice"):
        due_word = "Due date"  # none given: B1 works it out from the payment terms
    else:
        due, due_word = due or today, "Date"

    total = sum(l["Quantity"] * l["UnitPrice"] for l in lines)
    notes = []
    if card_type == "C" and k in ("sales_quotation", "sales_order", "ar_invoice", "ar_reserve_invoice") and not base:
        n, w = _price_advice(card, lines, cur)
        notes += n
        warnings += w
    if card_type == "C" and k in ("sales_order", "delivery", "ar_invoice", "ar_reserve_invoice", "ar_down_payment"):
        warnings += _credit_check(card, total, cur)
    ok, why = _enabled()
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    by = f" for {who['name']}" if who.get("email") else ""
    note = f"Created by the SAP B1 AI chat as a draft{by}" + (f": {remarks}" if remarks else "") + f" (code {code})."
    keys = ("BaseType", "BaseEntry", "BaseLine", "Quantity") if base else ("ItemCode", "Quantity", "UnitPrice", "WarehouseCode")
    if k == "goods_issue" and not base:
        keys = ("ItemCode", "Quantity", "WarehouseCode")  # B1 takes the item's cost itself
    payload = {"DocObjectCode": obj, "DocDate": f"{today:%Y-%m-%d}", "Comments": note[:254],
               "DocumentLines": [{k2: l[k2] for k2 in keys} for l in lines], **EXTRA.get(k, {})}
    if card["CardCode"]:
        payload["CardCode"] = card["CardCode"]
    if due is not None:
        payload["DocDueDate"] = f"{due:%Y-%m-%d}"
    if k in ("purchase_request", "purchase_quotation"):
        payload["RequriedDate"] = f"{due:%Y-%m-%d}"  # (sic: B1's own spelling)
        for line in payload["DocumentLines"]:
            line["RequiredDate"] = f"{due:%Y-%m-%d}"
    ref = reference or (base["ref"] if base and base["ref"] else "")
    if ref:
        payload["NumAtCard"] = str(ref)[:100]
    rows = "\n".join(f"| {l['ItemCode']} | {l['ItemName']} | {qty(l['Quantity'])} | {money(l['UnitPrice'], cur)} | "
                     f"{money(l['Quantity'] * l['UnitPrice'], cur)} | {l['WarehouseCode']} | {l['source']} |" for l in lines)
    when = f"{due_word}: {due:%d %b %Y}" if due is not None else f"{due_word}: from the payment terms"
    party = (f"for **{card['CardName']}** ({card['CardCode']})" if card["CardCode"] else
             f"· requested by **{who['name']}**" if k == "purchase_request" and who.get("email") else "")
    shown = (f"**{name} (draft preview)**" + (f" {party}" if party else "")
             + (f" · copied from {base['name']} {base['num']}" if base else "") + f" · {when}"
             + (f" · Ref: {ref}" if ref else "") + "\n\n"
             "| Item | Description | Qty | Unit price | Line total | Whs | Price from |\n|---|---|---|---|---|---|---|\n"
             f"{rows}\n\n**Total before tax: {money(total, cur)}** (B1 adds tax and rounding when the draft is created)\n")
    if notes:
        shown += "\n**Price check:**  \n" + "  \n".join(f"ℹ️ {n}" for n in notes) + "\n"
    if warnings:
        shown += "\n" + "\n".join(f"⚠️ {w}" for w in warnings) + "\n"
    if ok:
        shown += ("\n**Is this what you want?** Reply **yes** to create it as a **draft** in B1, or tell me what to change.  \n"
                  f"*(Approval ref **{code}**, valid once for {CODE_MINUTES} minutes. A draft is not posted: someone "
                  "opens it in B1, checks it and adds it.)*\n")
        with _lock:
            pend = _pending()
            pend[code] = {"expires": time.time() + CODE_MINUTES * 60, "payload": payload, "kind": name, "kind_key": k,
                          "preview": shown, "partner": f"{card['CardName']} ({card['CardCode']})" if card["CardCode"] else card["CardName"],
                          "total_before_tax": round(total, 2), "currency": cur, "requested_by": who.get("email", "")}
            _save_pending(pend)
    else:
        shown += f"\n*{why} Enter this in B1 yourself, or ask your administrator to switch on B1 drafts.*\n"
    return {"_display": shown, "document": name, "partner": card["CardName"], "lines": len(lines),
            "copied_from": f"{base['name']} {base['num']}" if base else None,
            "total_before_tax": round(total, 2), "currency": cur, "warnings": warnings, "drafts_enabled": ok,
            "instruction": ("The preview is already shown. Do not repeat its table. In one or two sentences say what will be "
                            "created and ask whether this is what they want: they reply yes to create the draft, or say what "
                            "to change. Nothing has been created yet; never say it has, and never call create_b1_draft now.")
            if ok else "The preview is shown. Say that drafts are not switched on, so it must be entered in B1 by hand."}


def offer(name: str, kind_key: str, header: str, table: str, total: float, cur: str, warnings: list[str], endpoint: str,
          payload: dict, partner: str, who: dict, code: str, done: str, done_instruction: str, id_field: str = "DocEntry",
          number_field: str = "", extra_result: dict | None = None) -> dict:
    """Finish any preview (b1create.py uses this too): approval question, pending job, result for the model."""
    ok, why = _enabled()
    shown = f"{header}\n\n{table}\n"
    if warnings:
        shown += "\n" + "\n".join(f"⚠️ {w}" for w in warnings) + "\n"
    if ok:
        shown += ("\n**Is this what you want?** Reply **yes** to create it in B1, or tell me what to change.  \n"
                  f"*(Approval ref **{code}**, valid once for {CODE_MINUTES} minutes.)*\n")
        with _lock:
            pend = _pending()
            pend[code] = {"expires": time.time() + CODE_MINUTES * 60, "payload": payload, "kind": name, "kind_key": kind_key,
                          "preview": shown, "partner": partner, "total_before_tax": round(total, 2), "currency": cur,
                          "requested_by": who.get("email", ""), "endpoint": endpoint, "id_field": id_field,
                          "number_field": number_field, "done": done, "done_instruction": done_instruction}
            _save_pending(pend)
    else:
        shown += f"\n*{why} Enter this in B1 yourself, or ask your administrator to switch on B1 drafts.*\n"
    return {"_display": shown, "document": name, "partner": partner, "total": round(total, 2), "currency": cur,
            "warnings": warnings, "drafts_enabled": ok, **(extra_result or {}),
            "instruction": ("The preview is already shown. Do not repeat its table. In one or two sentences say what will be "
                            "created and ask whether this is what they want: they reply yes to create it, or say what to "
                            "change. Nothing has been created yet; never say it has, and never call create_b1_draft now.")
            if ok else "The preview is shown. Say that creating it in B1 is not switched on, so it must be entered in B1 by hand."}


def new_code() -> str:
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


def create_b1_draft(code: str, _confirmed_by_user: bool = False) -> dict:
    """Create the prepared draft in B1. Only runs when the chat service saw the code in the user's own message."""
    code = (code or "").strip().upper()
    if not _confirmed_by_user:
        return {"error": "Not approved. Nothing was created. Show the preview again (prepare_b1_draft) and wait for the user to reply yes."}
    ok, why = _enabled()
    if not ok:
        return {"error": why}
    with _lock:
        pend = _pending()
        job = pend.get(code)
        who = access.current()
        if job and (job.get("requested_by", "") != who.get("email", "") or not access.can_draft(job.get("kind_key", ""), who)):
            # only the person who asked for the preview can approve it, and only within their role
            msg = "This preview was prepared for someone else or is outside your role, so it cannot be approved here."
            return {"_display": f"ℹ️ {msg} Nothing was created.\n", "error": msg, "instruction": "Already shown. Do not look anything up."}
        if not job or job.get("used"):
            why_not = _why_not_pending(code)
            return {"_display": f"ℹ️ {why_not} Nothing new was created.\n", "error": why_not,
                    "instruction": ("Already shown. Do not look anything up. The number is a DRAFT number in B1's Document "
                                    "Drafts report, not a sales order / PO number. If the user wants another document, prepare it again.")}
        job["used"] = True  # single use, even if B1 then refuses it
        _save_pending(pend)
    url = os.environ["B1_SL_URL"].rstrip("/")
    db = os.getenv("B1_SL_COMPANYDB") or os.environ["SAPB1_DB"]
    verify = os.getenv("B1_SL_VERIFY_TLS", "yes").lower() not in ("0", "no", "false")
    try:
        with httpx.Client(base_url=url, timeout=60, verify=verify) as sl:
            r = sl.post("/Login", json={"CompanyDB": db, "UserName": os.environ["B1_SL_USER"], "Password": os.environ["B1_SL_PASSWORD"]})
            if r.status_code != 200:
                _log({"code": code, "result": "login failed", "status": r.status_code})
                return {"error": "B1 refused the draft login: " + r.json().get("error", {}).get("message", {}).get("value", r.text[:200])}
            try:
                r = sl.post(job.get("endpoint", "/Drafts"), json=job["payload"])
                if r.status_code not in (200, 201, 204):
                    msg = r.json().get("error", {}).get("message", {}).get("value", r.text[:300])
                    _log({"code": code, "result": "refused", "message": msg, "document": job["kind"], "partner": job["partner"]})
                    return {"error": f"B1 did not accept the draft: {msg}"}
                d = r.json() if r.content else {}
            finally:
                sl.post("/Logout")
    except httpx.HTTPError as e:
        _log({"code": code, "result": "no connection", "detail": type(e).__name__})
        return {"error": f"Could not reach the B1 Service Layer ({type(e).__name__}). Nothing was created."}
    entry = d.get(job.get("id_field", "DocEntry"))
    number = d.get(job.get("number_field", "")) if job.get("number_field") else None
    with _lock:
        pend = _pending()
        if code in pend:
            pend[code]["draft_docentry"] = entry
            _save_pending(pend)
    _log({"code": code, "result": "created", "draft_docentry": entry, "document": job["kind"], "partner": job["partner"],
          "requested_by": job.get("requested_by", ""),
          "doc_total": d.get("DocTotal"), "company_db": db})
    cur = job["currency"]
    if job.get("done"):  # payments, stock documents, production orders, journal vouchers (b1create.py)
        shown = "✅ " + job["done"].format(entry=entry if entry is not None else "", number=number or entry or "",
                                          partner=job["partner"], total=money(float(d.get("DocTotal") or job.get("total_before_tax") or 0), cur)) + "\n"
    else:
        shown = (f"✅ **Draft created in B1:** {job['kind']} draft no. **{entry}** for {job['partner']}, total "
                 f"{money(float(d.get('DocTotal') or 0), cur)} including tax.  \nOpen it from B1's **Document Drafts** report "
                 "(type *Document Drafts* in the B1 menu search), check it and click **Add** to post it.\n")
    return {"_display": shown, "draft_docentry": entry, "document": job["kind"], "partner": job["partner"],
            "total_incl_tax": d.get("DocTotal"),
            "instruction": job.get("done_instruction") or ("The result is already shown. Confirm it in one sentence. The number is the DRAFT number, found "
                            "in B1's Document Drafts report; it is not a sales order / PO number yet, so give no other menu path.")}
