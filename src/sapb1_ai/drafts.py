"""Drafts (Joule-style "generative"): payment reminders, supplier order emails and cover notes for documents.

Every draft is built here from B1 data, so each figure in it is exactly B1's; the AI model only introduces it.
Drafts are never sent: they are shown in the chat to copy, and can be downloaded as an email file (.eml) that opens in
Outlook or Thunderbird as an unsent draft. Read-only: nothing is written to SAP.
"""

import json
import re
import secrets
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate

import pandas as pd

from .db import query
from .export import EXPORT_DIR, URL_PREFIX, _safe_name, cleanup
from .numbers import label, money, qty
from .summaries import DOCS, _find_partner, _kind, _read
from .sync import DATA_DIR

TONES = ("polite", "firm", "final", "statement")
PAY_WITHIN_DAYS = {"firm": 7, "final": 7}


def _company() -> dict:
    try:
        r = query("SELECT CompnyName, Phone1, E_Mail FROM OADM").iloc[0]
        return {"name": r.CompnyName, "phone": r.Phone1 or "", "email": r.E_Mail or ""}
    except Exception:
        import os
        return {"name": os.getenv("COMPANY_NAME", ""), "phone": "", "email": ""}


def _contact(code: str) -> tuple[str, str]:
    """(contact name, email) of a business partner: its main contact person, else the partner's own email."""
    r = query("SELECT p.CntctPrsn, p.E_Mail, c.E_MailL, c.FirstName, c.LastName FROM OCRD p "
              "LEFT JOIN OCPR c ON c.CardCode = p.CardCode AND c.Name = p.CntctPrsn WHERE p.CardCode = ?", (code,))
    if r.empty:
        return "", ""
    r = r.iloc[0]
    name = " ".join(x for x in (r.FirstName, r.LastName) if x) or r.CntctPrsn or ""
    return name, r.E_MailL or r.E_Mail or ""


def _d(v, lang: str) -> str:
    d = pd.Timestamp(v)
    if lang == "ms":
        months = ["Januari", "Februari", "Mac", "April", "Mei", "Jun", "Julai", "Ogos", "September", "Oktober", "November", "Disember"]
        return f"{d.day} {months[d.month - 1]} {d.year}"
    return f"{d.day} {d:%b %Y}"


def _table(head: list[str], rows: list[list[str]], right: set[int]) -> str:
    """A plain-text table that stays aligned in any email program using a fixed-width font, and readable otherwise."""
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    fmt = lambda r: "  ".join(str(x).rjust(w) if i in right else str(x).ljust(w) for i, (x, w) in enumerate(zip(r, widths))).rstrip()
    return "\n".join([fmt(head), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


def _signature(co: dict, lang: str) -> str:
    lines = ["Yang benar," if lang == "ms" else "Kind regards,", "", "[Your name]" if lang != "ms" else "[Nama anda]", co["name"]]
    lines += [x for x in (co["phone"], co["email"]) if x]
    return "\n".join(lines)


def _deliver(to: str, subject: str, body: str, filename: str, note: str) -> dict:
    """Show the draft in the chat (with a copy button) and save it as an unsent email file."""
    cleanup()
    folder = EXPORT_DIR / secrets.token_urlsafe(24)
    folder.mkdir(parents=True)
    msg = EmailMessage()
    msg["To"], msg["Subject"], msg["Date"] = to, subject, formatdate(localtime=True)
    msg["X-Unsent"] = "1"  # Outlook opens it as a new, unsent message
    msg.set_content(body)
    name = _safe_name(filename) + ".eml"
    (folder / name).write_bytes(bytes(msg))
    link = f"{URL_PREFIX}/{folder.name}/{name}"
    shown = (f"**To:** {to or '(no email in B1: add the address)'}  \n**Subject:** {subject}\n\n```text\n{body}\n```\n"
             f"📧 **[Open as an email draft: {name}]({link})** (opens unsent in Outlook; link works for 24 hours)\n")
    return {"_display": shown, "to": to, "subject": subject, "note": note,
            "instruction": "The full draft is already shown to the user. Do not rewrite it or repeat its figures. In one or "
                           "two sentences say what it is (and the tone), and offer to change the tone or language. "
                           "It is a draft: nothing has been sent."}


# ---------- payment reminders and statements ----------

TEXT = {
    "en": {
        "subject": {"polite": "Payment reminder: {n} overdue invoice(s), {total}",
                    "firm": "Second reminder: {total} overdue",
                    "final": "Final notice: {total} overdue",
                    "statement": "Statement of account as of {today}"},
        "intro": {"polite": "I hope you are well. According to our records, the following invoice(s) from {co} are now past "
                            "their due date:",
                  "firm": "We wrote to you earlier about the invoice(s) below, which are still unpaid although they are "
                          "past their due date:",
                  "final": "Despite our earlier reminders, we have not received payment for the overdue invoice(s) below:",
                  "statement": "Please find below your statement of account with {co} as of {today}:"},
        "ask": {"polite": "We would be grateful if you could arrange payment of {total} at your earliest convenience.",
                "firm": "Please arrange payment of {total} within {days} days, by {by}, or let us know if there is a problem "
                        "with any of these invoices.",
                "final": "Unless payment of {total} reaches us by {by}, we will have to put your account on hold, with no "
                         "further deliveries until it is settled. Please contact us straight away if you wish to discuss it.",
                "statement": "{overdue_line} Please let us know if anything on this statement does not match your "
                             "records."},
        "paid": "If you have already paid, please send us the payment details so we can match it, and accept our thanks.",
        "upcoming": "For your information, a further {amount} on {n} invoice(s) falls due by {date}.",
        "head": ["Invoice", "Date", "Due date", "Amount", "Still due", "Days late"],
        "total": "Total", "dear": "Dear {name},", "team": "Accounts Team",
        "overdue_some": "Of this, {overdue} is overdue.", "overdue_none": "None of it is overdue yet.",
    },
    "ms": {
        "subject": {"polite": "Peringatan bayaran: {n} invois tertunggak, {total}",
                    "firm": "Peringatan kedua: {total} tertunggak",
                    "final": "Notis terakhir: {total} tertunggak",
                    "statement": "Penyata akaun setakat {today}"},
        "intro": {"polite": "Semoga anda sihat. Menurut rekod kami, invois berikut daripada {co} telah melepasi tarikh "
                            "akhir bayaran:",
                  "firm": "Kami telah menghubungi pihak tuan/puan sebelum ini mengenai invois di bawah, yang masih belum "
                          "dibayar walaupun telah melepasi tarikh akhir:",
                  "final": "Walaupun peringatan telah diberikan, kami masih belum menerima bayaran bagi invois tertunggak "
                           "di bawah:",
                  "statement": "Bersama ini kami sertakan penyata akaun anda dengan {co} setakat {today}:"},
        "ask": {"polite": "Kami amat menghargai jika bayaran sebanyak {total} dapat dibuat secepat mungkin.",
                "firm": "Sila buat bayaran sebanyak {total} dalam tempoh {days} hari, iaitu sebelum {by}, atau maklumkan "
                        "kepada kami jika terdapat masalah dengan mana-mana invois ini.",
                "final": "Sekiranya bayaran sebanyak {total} tidak diterima sebelum {by}, kami terpaksa menggantung akaun "
                         "anda dan tiada penghantaran akan dibuat sehingga baki dijelaskan. Sila hubungi kami dengan segera "
                         "jika ingin berbincang.",
                "statement": "{overdue_line} Sila maklumkan kepada kami jika terdapat perbezaan dengan rekod anda."},
        "paid": "Sekiranya bayaran telah dibuat, sila kongsikan butiran bayaran supaya kami dapat memadankannya. Terima kasih.",
        "upcoming": "Untuk makluman, sejumlah {amount} bagi {n} invois lagi akan matang sebelum {date}.",
        "head": ["Invois", "Tarikh", "Tarikh akhir", "Amaun", "Baki", "Hari lewat"],
        "total": "Jumlah", "dear": "Tuan/Puan {name},", "team": "Bahagian Akaun",
        "overdue_some": "Daripada jumlah ini, {overdue} telah tertunggak.", "overdue_none": "Tiada amaun yang tertunggak lagi.",
    },
}


def payment_reminder(customer: str, tone: str = "", language: str = "en") -> dict:
    """Draft a payment reminder (polite, firm, final) or a statement of account for one customer, from open invoices."""
    lang = "ms" if (language or "").lower().startswith(("ms", "ma", "bm", "bahasa")) else "en"
    tone = (tone or "").lower().strip()
    if tone and tone not in TONES:
        return {"error": f"Tone must be one of: {', '.join(TONES)}."}
    found = _find_partner(customer, "C")
    if "match" not in found:
        return found
    p = found["match"]
    code, today = p["CardCode"], pd.Timestamp(date.today())
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    cur = label(meta.get("currency") or "")
    ar = _read("ar_open", "DocDate", "DocDueDate")
    ar = ar[ar.CardCode == code].sort_values(["DocDueDate", "DocNum"])
    if ar.empty:
        return {"note": f"{p['CardName']} has no unpaid invoices, so there is nothing to remind them about."}
    ar = ar.assign(days_late=(today - ar.DocDueDate).dt.days.clip(lower=0))
    late = ar[ar.days_late > 0]
    auto = not tone
    if auto:  # pick the tone from how late the oldest invoice is
        oldest = int(late.days_late.max()) if len(late) else 0
        tone = "statement" if late.empty else "polite" if oldest <= 30 else "firm" if oldest <= 60 else "final"
    if tone != "statement" and late.empty:
        return {"note": f"Nothing is overdue for {p['CardName']}: their {len(ar)} unpaid invoice(s) are not yet due. "
                        "Offer a statement of account instead (tone = statement)."}
    t = TEXT[lang]
    rows = ar if tone == "statement" else late
    total, overdue = float(rows.Balance.sum()), float(late.Balance.sum())
    by = today + timedelta(days=PAY_WITHIN_DAYS.get(tone, 0))
    co = _company()
    name, email = _contact(code)
    fields = {"n": len(rows), "total": money(total, cur), "overdue": money(overdue, cur), "co": co["name"],
              "today": _d(today, lang), "days": PAY_WITHIN_DAYS.get(tone, 0), "by": _d(by, lang)}
    fields["overdue_line"] = t["overdue_some"].format(**fields) if overdue > 0 else t["overdue_none"]
    table = _table(t["head"] + [], [[int(r.DocNum), _d(r.DocDate, lang), _d(r.DocDueDate, lang), money(r.DocTotal, cur),
                                     money(r.Balance, cur), qty(r.days_late) if r.days_late else "-"] for r in rows.itertuples()], {3, 4, 5})
    table += f"\n\n{t['total']}: {money(total, cur)}"
    parts = [t["dear"].format(name=name or t["team"]), "", t["intro"][tone].format(**fields), "", table, "",
             t["ask"][tone].format(**fields)]
    upcoming = ar[ar.days_late == 0]
    if tone != "statement" and len(upcoming):
        parts += ["", t["upcoming"].format(amount=money(float(upcoming.Balance.sum()), cur), n=len(upcoming),
                                           date=_d(upcoming.DocDueDate.max(), lang))]
    if tone != "statement":
        parts += ["", t["paid"]]
    parts += ["", _signature(co, lang)]
    subject = t["subject"][tone].format(**fields)
    return _deliver(email, subject, "\n".join(parts), f"{tone} {p['CardName']}",
                    f"{'statement of account' if tone == 'statement' else tone + ' reminder'} for {p['CardName']} ({code}): "
                    f"{len(rows)} invoice(s), {money(total, cur)}" +
                    (f"; tone chosen from the oldest invoice ({int(late.days_late.max())} days late)" if auto and len(late) else ""))


# ---------- supplier order email ----------

def _parse_items(text: str) -> list[tuple[str, float]]:
    """'LT-LAP-14:10, LT-MON-27 x 5' -> [('LT-LAP-14', 10.0), ('LT-MON-27', 5.0)]"""
    out = []
    for part in re.split(r"[,;\n]+", text or ""):
        m = re.match(r"\s*(.+?)\s*(?:[:x×=]|\s)\s*([\d.]+)\s*$", part, re.I)
        if m:
            out.append((m.group(1).strip(), float(m.group(2))))
    return out


def supplier_order_email(supplier: str = "", items: str = "", language: str = "en") -> dict:
    """Draft an order email to a supplier: from the reorder suggestions, or from the items and quantities given."""
    lang = "ms" if (language or "").lower().startswith(("ms", "ma", "bm", "bahasa")) else "en"
    rep = pd.read_csv(DATA_DIR / "reorder_report.csv", dtype={"WhsCode": str, "ItemCode": str})
    sugg = rep[rep.suggested_qty > 0]
    if not supplier:  # which suppliers have something to order
        g = sugg.groupby(sugg.PrefVendor.fillna("(no preferred supplier)")).agg(
            items=("ItemCode", "nunique"), suggested_value=("suggested_value", "sum")).reset_index()
        return {"note": "Ask which supplier to draft for (or the items to order).",
                "suppliers_with_suggested_orders": json.loads(g.round(2).to_json(orient="records"))}
    found = _find_partner(supplier, "S")
    if "match" not in found:
        return found
    p = found["match"]
    code = p["CardCode"]
    meta = json.loads((DATA_DIR / "sync_meta.json").read_text())
    cur = label(meta.get("currency") or "")
    master = pd.read_parquet(DATA_DIR / "items.parquet").set_index("ItemCode")
    ap = _read("ap_lines", "DocDate")
    ap = ap[(ap.CardCode == code) & (ap.InvQty > 0)].sort_values("DocDate")
    last_price = (ap.LineTotal / ap.InvQty).groupby(ap.ItemCode).last()

    lines, unknown = [], []
    if items:
        for text, q in _parse_items(items):
            hit = master.index[master.index.str.upper() == text.upper()]
            if not len(hit):
                hit = master.index[master.ItemName.str.contains(text, case=False, regex=False, na=False)]
            if len(hit) != 1:
                unknown.append(text)
                continue
            lines.append({"code": hit[0], "qty": q, "need": None, "whs": ""})
        if unknown:
            return {"error": f"Could not find exactly one item for: {', '.join(unknown)}. Use search_items for the codes."}
        source = "the items given"
    else:
        mine = sugg[sugg.PrefVendor == code]
        if mine.empty:
            return {"note": f"Nothing is suggested to order from {p['CardName']} right now (reorder report). "
                            "Give the items and quantities to draft an order anyway, e.g. items='LT-LAP-14:10'."}
        for r in mine.sort_values("suggested_value", ascending=False).itertuples():
            lines.append({"code": r.ItemCode, "qty": float(r.suggested_qty), "whs": r.WhsCode,
                          "need": r.runout_date if isinstance(r.runout_date, str) and r.runout_date else None})
        source = "the reorder suggestions"
    rows, total = [], 0.0
    for ln in lines:
        it = master.loc[ln["code"]]
        price = last_price.get(ln["code"])
        price = float(price) if price is not None and pd.notna(price) else (float(it.LastPurPrc) if pd.notna(it.LastPurPrc) and it.LastPurPrc else None)
        value = price * ln["qty"] if price else None
        total += value or 0
        rows.append([ln["code"], it.ItemName, qty(ln["qty"]), it.InvntryUom if isinstance(it.InvntryUom, str) else "", money(price, cur) if price else "-",
                     money(value, cur) if value else "-", _d(ln["need"], lang) if ln["need"] else "-"])
    head = (["Kod", "Barang", "Kuantiti", "Unit", "Harga terakhir", "Nilai", "Diperlukan"] if lang == "ms" else
            ["Code", "Item", "Qty", "Unit", "Last price", "Value", "Needed by"])
    table = _table(head, rows, {2, 4, 5}) + (f"\n\n{'Anggaran jumlah' if lang == 'ms' else 'Estimated total'} "
                                              f"({'sebelum cukai' if lang == 'ms' else 'before tax'}): {money(total, cur)}")
    co = _company()
    name, email = _contact(code)
    if lang == "ms":
        body = [f"Tuan/Puan {name or 'Bahagian Jualan'},", "",
                f"Kami ingin membuat pesanan bagi barang berikut daripada {p['CardName']}:", "", table, "",
                "Harga di atas ialah harga belian terakhir kami. Sila sahkan harga semasa dan tarikh penghantaran, "
                "dan kami akan menghantar pesanan belian (PO) rasmi.", "", _signature(co, lang)]
        subject = f"Permintaan pesanan: {len(rows)} barang daripada {co['name']}"
    else:
        body = [f"Dear {name or 'Sales Team'},", "",
                f"We would like to order the following items from {p['CardName']}:", "", table, "",
                "The prices shown are our last purchase prices. Please confirm current prices and the delivery date, "
                "and we will send our formal purchase order.", "", _signature(co, lang)]
        subject = f"Order request: {len(rows)} item(s) from {co['name']}"
    note = f"Order email to {p['CardName']} ({code}) from {source}: {len(rows)} item(s), estimated {money(total, cur)} before tax."
    no_vendor = sugg[sugg.PrefVendor.isna()]
    if not items and len(no_vendor):
        note += f" {no_vendor.ItemCode.nunique()} other suggested item(s) have no preferred supplier in B1."
    return _deliver(email, subject, "\n".join(body), f"order {p['CardName']}", note)


# ---------- cover notes for documents ----------

COVER = {
    "sales_quotation": ("Quotation {n}", "Please find attached our quotation {n} dated {date} for {total}, valid until {due}. "
                        "Do let us know if you have any questions or would like us to adjust anything.",
                        "Sebut harga {n}", "Bersama ini kami lampirkan sebut harga {n} bertarikh {date} berjumlah {total}, "
                        "sah sehingga {due}. Sila hubungi kami jika ada sebarang pertanyaan."),
    "sales_order": ("Order confirmation {n}", "Thank you for your order. Please find attached our order confirmation {n} dated "
                    "{date} for {total}, with delivery planned for {due}.",
                    "Pengesahan pesanan {n}", "Terima kasih atas pesanan anda. Bersama ini kami lampirkan pengesahan pesanan "
                    "{n} bertarikh {date} berjumlah {total}, dengan penghantaran dijadualkan pada {due}."),
    "delivery": ("Delivery order {n}", "Please find attached delivery order {n} for the goods delivered on {date}.",
                 "Nota hantaran {n}", "Bersama ini kami lampirkan nota hantaran {n} bagi barang yang dihantar pada {date}."),
    "ar_invoice": ("Invoice {n}", "Please find attached invoice {n} dated {date} for {total}, due for payment by {due}.",
                   "Invois {n}", "Bersama ini kami lampirkan invois {n} bertarikh {date} berjumlah {total}, perlu dibayar "
                   "sebelum {due}."),
    "ar_credit_memo": ("Credit note {n}", "Please find attached credit note {n} dated {date} for {total}.",
                       "Nota kredit {n}", "Bersama ini kami lampirkan nota kredit {n} bertarikh {date} berjumlah {total}."),
    "purchase_order": ("Purchase order {n}", "Please find attached our purchase order {n} dated {date} for {total}, required "
                       "by {due}. Please confirm the order and the delivery date.",
                       "Pesanan belian {n}", "Bersama ini kami lampirkan pesanan belian {n} bertarikh {date} berjumlah "
                       "{total}, diperlukan sebelum {due}. Sila sahkan pesanan dan tarikh penghantaran."),
}


def cover_note(kind: str, number: int, language: str = "en") -> dict:
    """Short covering email for a quotation, order confirmation, delivery, invoice, credit note or purchase order."""
    lang = "ms" if (language or "").lower().startswith(("ms", "ma", "bm", "bahasa")) else "en"
    k = _kind(kind)
    if k not in COVER:
        return {"error": f"Cover notes are for: {', '.join(COVER)}."}
    head = DOCS[k][0]
    h = query(f"SELECT TOP 1 DocNum, DocDate, DocDueDate, CardCode, CardName, DocCur, DocTotal, DocTotalFC, CANCELED "
              f"FROM {head} WHERE DocNum = ? ORDER BY DocEntry DESC", (int(number),))
    if h.empty:
        return {"error": f"No {DOCS[k][3]} number {number} in B1."}
    r = h.iloc[0]
    if r.CANCELED == "Y":
        return {"error": f"{DOCS[k][3]} {number} is cancelled; no cover note."}
    local = label(json.loads((DATA_DIR / "sync_meta.json").read_text()).get("currency") or "")
    doc_cur = label(r.DocCur or "") or local
    total = money(float(r.DocTotalFC if doc_cur != local else r.DocTotal), doc_cur)
    subj_en, body_en, subj_ms, body_ms = COVER[k]
    fields = {"n": int(r.DocNum), "date": _d(r.DocDate, lang), "due": _d(r.DocDueDate, lang), "total": total}
    co = _company()
    name, email = _contact(r.CardCode)
    team = ("Bahagian Akaun" if lang == "ms" else "Accounts Team") if k.startswith("ar_") else ("Bahagian Jualan" if lang == "ms" else "Sales Team") if k == "purchase_order" else ""
    dear = f"Tuan/Puan {name or team}".strip() if lang == "ms" else f"Dear {name or team or r.CardName}"
    body = "\n".join([dear + ",", "", (body_ms if lang == "ms" else body_en).format(**fields), "", _signature(co, lang)])
    subject = f"{(subj_ms if lang == 'ms' else subj_en).format(**fields)} - {co['name']}"
    return _deliver(email, subject, body, f"{DOCS[k][3]} {int(r.DocNum)} cover note",
                    f"Cover note for {DOCS[k][3]} {int(r.DocNum)} to {r.CardName}; attach the document printed from B1.")
