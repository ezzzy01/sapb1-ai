"""The rest of B1's documents, the same way as b1drafts.py: check against B1, show a preview, create after the user's yes.

    stock documents   inventory transfer (draft), transfer request (a request only: moves nothing),
                      inventory counting (draft), inventory posting (draft)
    payments          incoming payment from a customer / outgoing payment to a supplier, against their open invoices (draft)
    production order  created with status Planned (B1 has no production order drafts; Planned moves nothing)
    journal entry     created as a journal voucher: held in Financials > Journal Vouchers until someone posts it

Approval, roles, the Service Layer login, live-database blocking and the log are shared with b1drafts.py.
"""

import re
from datetime import date, timedelta

import pandas as pd

from . import access
from .b1drafts import _parse_items, new_code, offer
from .db import query
from .numbers import label, money, qty
from .summaries import _find_partner

STOCK_KINDS = {  # kind -> (name, Service Layer endpoint, how it is kept)
    "inventory_transfer": ("Inventory Transfer", "/StockTransferDrafts", "draft"),
    "transfer_request": ("Inventory Transfer Request", "/InventoryTransferRequests", "request"),
    "inventory_counting": ("Inventory Counting", "/InventoryCountingDrafts", "draft"),
    "inventory_posting": ("Inventory Posting", "/InventoryPostingDrafts", "draft"),
}
STOCK_ALIASES = {"transfer": "inventory_transfer", "stock transfer": "inventory_transfer", "pindahan stok": "inventory_transfer",
                 "request": "transfer_request", "transfer request": "transfer_request", "counting": "inventory_counting",
                 "count": "inventory_counting", "stock count": "inventory_counting", "stock take": "inventory_counting",
                 "posting": "inventory_posting", "stock adjustment": "inventory_posting", "kiraan stok": "inventory_counting"}


def _today() -> pd.Timestamp:
    return pd.Timestamp(date.today())


def _date(text: str, default: pd.Timestamp) -> pd.Timestamp | str:
    if not text:
        return default
    try:
        return pd.Timestamp(text)
    except ValueError:
        return f"'{text}' is not a date; use YYYY-MM-DD."


def _not_allowed(kind: str, name: str) -> dict | None:
    who = access.current()
    if not access.can_draft(kind, who):
        return {"error": f"Not allowed: creating a {name} is not part of the {who['role']} role.",
                "note": "Tell the user plainly; an administrator can change their access. Do not prepare it another way."}
    return None


def _note(remarks: str, code: str) -> str:
    who = access.current()
    by = f" for {who['name']}" if who.get("email") else ""
    return (f"Created by the SAP B1 AI chat{by}" + (f": {remarks}" if remarks else "") + f" (code {code}).")[:254]


def _local() -> str:
    return query("SELECT MainCurncy FROM OADM").MainCurncy.iloc[0]


def _warehouse(code: str) -> dict | str:
    if not code:
        return "Which warehouse?"
    w = query("SELECT WhsCode, WhsName, Inactive FROM OWHS WHERE WhsCode = ? OR WhsName LIKE ?", (code, f"%{code}%"))
    if len(w) != 1:
        return f"Warehouse '{code}': " + ("not found" if w.empty else "several match (" + ", ".join(w.WhsCode) + ")")
    if w.Inactive.iloc[0] == "Y":
        return f"Warehouse {w.WhsCode.iloc[0]} is inactive."
    return {"code": w.WhsCode.iloc[0], "name": w.WhsName.iloc[0]}


def _stock_item(text: str) -> dict | str:
    it = query("SELECT ItemCode, ItemName, InvntItem, validFor, frozenFor, AvgPrice, LastPurPrc FROM OITM WHERE ItemCode = ?", (text,))
    if it.empty:
        it = query("SELECT TOP 3 ItemCode, ItemName, InvntItem, validFor, frozenFor, AvgPrice, LastPurPrc FROM OITM WHERE ItemName LIKE ?",
                   (f"%{text}%",))
    if len(it) != 1:
        return f"{text}: " + ("no such item" if it.empty else "several items match (" + ", ".join(it.ItemCode) + "); use the code")
    it = it.iloc[0]
    if it.InvntItem != "Y":
        return f"{it.ItemCode}: not a stock item"
    if it.frozenFor == "Y" or it.validFor == "N":
        return f"{it.ItemCode}: inactive in B1"
    return {"code": it.ItemCode, "name": it.ItemName, "cost": float(it.AvgPrice or 0) or float(it.LastPurPrc or 0)}


def _on_hand(item: str, whs: str) -> float:
    r = query("SELECT OnHand FROM OITW WHERE ItemCode = ? AND WhsCode = ?", (item, whs))
    return float(r.OnHand.iloc[0]) if len(r) else 0.0


# ---------------------------------------------------------------- stock documents

def prepare_b1_stock_document(kind: str, items: str, warehouse: str = "", to_warehouse: str = "", doc_date: str = "",
                              remarks: str = "") -> dict:
    """Inventory transfer / transfer request (items:quantity from warehouse to to_warehouse), or inventory counting /
    posting (items:counted quantity in one warehouse)."""
    t = (kind or "").strip().lower().replace("_", " ")
    k = next((x for x in STOCK_KINDS if t == x.replace("_", " ")), None) or STOCK_ALIASES.get(t)
    if not k:
        from .b1drafts import _kind, prepare_b1_draft
        if _kind(kind) in ("goods_receipt", "goods_issue", "purchase_request"):  # documents, not stock movements between warehouses
            return prepare_b1_draft(_kind(kind), items=items, due_date=doc_date, remarks=remarks, warehouse=warehouse)
        return {"error": "Stock documents can be: " + ", ".join(STOCK_KINDS) + "."}
    name, endpoint, kept = STOCK_KINDS[k]
    if (e := _not_allowed(k, name)):
        return e
    when = _date(doc_date, _today())
    if isinstance(when, str):
        return {"error": when}
    src = _warehouse(warehouse)
    if isinstance(src, str):
        return {"error": src}
    dst = None
    if k in ("inventory_transfer", "transfer_request"):
        dst = _warehouse(to_warehouse)
        if isinstance(dst, str):
            return {"error": ("To which warehouse? " if not to_warehouse else "") + dst}
        if dst["code"] == src["code"]:
            return {"error": "The two warehouses are the same."}
    parsed = _parse_items(items)
    if not parsed:
        return {"error": "Give items and quantities, e.g. items='A00001:5, C00010:12'."}
    cur = label(_local())
    lines, problems, warnings, rows = [], [], [], []
    for text, q, _ in parsed:
        it = _stock_item(text)
        if isinstance(it, str):
            problems.append(it)
            continue
        have = _on_hand(it["code"], src["code"])
        if dst:
            if q <= 0:
                problems.append(f"{it['code']}: quantity must be more than 0")
                continue
            if have < q:
                warnings.append(f"{it['name']}: only {qty(have)} in warehouse {src['code']} for {qty(q)}")
            lines.append({"ItemCode": it["code"], "Quantity": q, "FromWarehouseCode": src["code"], "WarehouseCode": dst["code"]})
            rows.append(f"| {it['code']} | {it['name']} | {qty(q)} | {qty(have)} |")
        else:
            if q < 0:
                problems.append(f"{it['code']}: a counted quantity cannot be negative")
                continue
            line = {"ItemCode": it["code"], "WarehouseCode": src["code"], "CountedQuantity": q}
            if k == "inventory_counting":
                line["Counted"] = "tYES"
            lines.append(line)
            diff = q - have
            rows.append(f"| {it['code']} | {it['name']} | {qty(have)} | {qty(q)} | {'+' if diff > 0 else ''}{qty(diff)} | "
                        f"{money(diff * it['cost'], cur)} |")
    if problems:
        return {"error": "Fix these first: " + "; ".join(problems)}
    code = new_code()
    if dst:
        payload = {"DocDate": f"{when:%Y-%m-%d}", "FromWarehouse": src["code"], "ToWarehouse": dst["code"],
                   "Comments": _note(remarks, code), "StockTransferLines": lines}
        if k == "transfer_request":
            payload["DueDate"] = f"{when:%Y-%m-%d}"
        table = "| Item | Description | Qty | In stock at source |\n|---|---|---|---|\n" + "\n".join(rows)
        what = f"from {src['code']} {src['name']} to {dst['code']} {dst['name']}"
    else:
        if k == "inventory_counting":
            payload = {"CountDate": f"{when:%Y-%m-%d}", "Remarks": _note(remarks, code), "InventoryCountingLines": lines}
        else:
            payload = {"PostingDate": f"{when:%Y-%m-%d}", "CountDate": f"{when:%Y-%m-%d}", "Comments": _note(remarks, code),
                       "InventoryPostingLines": lines}
        table = ("| Item | Description | In B1 now | Counted | Difference | Value of difference |\n|---|---|---|---|---|---|\n"
                 + "\n".join(rows))
        what = f"in {src['code']} {src['name']}"
    label_ = "draft preview" if kept == "draft" else "preview"
    header = f"**{name} ({label_})** {what} · Date: {when:%d %b %Y}"
    if k == "inventory_posting":
        warnings.append("When someone adds this posting in B1, stock is set to the counted quantities and the difference is booked")
    if kept == "draft":
        done = (f"**Draft created in B1:** {name} draft no. **{{entry}}**. Open it from B1's **Document Drafts** report, check it "
                "and add it." if k == "inventory_transfer" else
                f"**Draft created in B1:** {name} draft no. **{{entry}}**. Open it in B1 (Inventory > Inventory Transactions > "
                f"{'Inventory Counting Transactions > Inventory Counting' if k == 'inventory_counting' else 'Inventory Counting Transactions > Inventory Posting'}), "
                "check it and add it.")
    else:
        done = (f"**Transfer request no. {{number}} created in B1.** It moves no stock: the warehouse makes the transfer from it "
                "(Inventory > Inventory Transactions > Inventory Transfer Request).")
    return offer(name, k, header, table, 0.0, cur, warnings, endpoint, payload, what, access.current(), code, done,
                 "The result is already shown. Confirm it in one sentence; give no other menu path.",
                 id_field="DocEntry" if dst else "DocumentEntry", number_field="DocNum" if dst else "DocumentNumber")


# ---------------------------------------------------------------- payments

def prepare_b1_payment(kind: str, partner: str, invoices: str = "", amount: float | str = "", bank: str = "",
                       doc_date: str = "", reference: str = "", remarks: str = "") -> dict:
    """Payment draft: incoming (a customer pays us) or outgoing (we pay a supplier), against their open invoices."""
    t = (kind or "").lower()
    k = ("incoming_payment" if re.search(r"incoming|receive|received|receipt|customer|terima", t) else
         "outgoing_payment" if re.search(r"outgoing|pay|vendor|supplier|bayar", t) else None)
    if not k:
        return {"error": "kind must be incoming_payment (a customer pays us) or outgoing_payment (we pay a supplier)."}
    name = "Incoming Payment" if k == "incoming_payment" else "Outgoing Payment"
    if (e := _not_allowed(k, name)):
        return e
    card_type = "C" if k == "incoming_payment" else "S"
    found = _find_partner(partner, card_type)
    if "match" not in found:
        return found
    card = found["match"]
    when = _date(doc_date, _today())
    if isinstance(when, str):
        return {"error": when}
    head, inv_type = ("OINV", "it_Invoice") if card_type == "C" else ("OPCH", "it_PurchaseInvoice")
    open_inv = query(f"SELECT DocEntry, DocNum, DocDate, DocDueDate, DocTotal - PaidToDate AS balance, DocCur FROM {head} "
                     "WHERE CardCode = ? AND DocStatus = 'O' AND CANCELED = 'N' AND DocTotal - PaidToDate > 0 ORDER BY DocDueDate",
                     (card["CardCode"],))
    if open_inv.empty:
        return {"error": f"{card['CardName']} has no open {'invoices' if card_type == 'C' else 'supplier invoices'} to pay."}
    wanted = [int(n) for n in re.findall(r"\d+", invoices or "")]
    if wanted:
        missing = [n for n in wanted if n not in set(open_inv.DocNum)]
        if missing:
            return {"error": f"Not open invoices of {card['CardName']}: {', '.join(map(str, missing))}. "
                             f"Open ones: {', '.join(map(str, open_inv.DocNum))}."}
        open_inv = open_inv[open_inv.DocNum.isin(wanted)]
    total_due = float(open_inv.balance.sum())
    try:
        pay = float(str(amount).replace(",", "")) if str(amount).strip() else total_due
    except ValueError:
        return {"error": f"'{amount}' is not an amount."}
    if pay <= 0:
        return {"error": "The amount must be more than 0."}
    if pay > total_due + 0.005:
        return {"error": f"{money(pay, label(_local()))} is more than the {money(total_due, label(_local()))} still open on these invoices."}
    left, applied, rows = pay, [], []
    cur = label(_local())
    for _, r in open_inv.iterrows():  # oldest due first
        if left <= 0.005:
            break
        part = round(min(left, float(r.balance)), 2)
        left -= part
        applied.append({"DocEntry": int(r.DocEntry), "SumApplied": part, "InvoiceType": inv_type})
        rows.append(f"| {int(r.DocNum)} | {pd.Timestamp(r.DocDueDate):%d %b %Y} | {money(float(r.balance), cur)} | {money(part, cur)} |")
    banks = query("SELECT b.BankCode, b.Account, b.GLAccount, a.AcctName, a.FormatCode FROM DSC1 b LEFT JOIN OACT a ON a.AcctCode = b.GLAccount "
                  "WHERE b.GLAccount IS NOT NULL AND b.GLAccount <> ''")
    warnings, acct, acct_name = [], None, ""
    if bank:
        hit = banks[(banks.BankCode.str.lower() == bank.lower()) | banks.Account.astype(str).str.contains(bank, regex=False)
                    | banks.AcctName.fillna("").str.lower().str.contains(bank.lower(), regex=False)]
        if hit.empty:
            a = query("SELECT AcctCode, AcctName FROM OACT WHERE (FormatCode = ? OR AcctCode = ?) AND Postable = 'Y'", (bank, bank))
            if a.empty:
                return {"error": f"Bank account '{bank}' not found. House bank accounts: "
                                 + ", ".join(f"{b.BankCode} {b.Account}" for b in banks.itertuples()) + "."}
            acct, acct_name = a.AcctCode.iloc[0], a.AcctName.iloc[0]
        else:
            acct, acct_name = hit.GLAccount.iloc[0], f"{hit.BankCode.iloc[0]} {hit.Account.iloc[0]}"
    elif len(banks) == 1:
        acct, acct_name = banks.GLAccount.iloc[0], f"{banks.BankCode.iloc[0]} {banks.Account.iloc[0]}"
    else:
        warnings.append("No bank account chosen: the person adding this draft in B1 fills in the payment means "
                        "(say e.g. 'by transfer to MBB' to set it)")
    code = new_code()
    payload = {"DocObjectCode": "bopot_IncomingPayments" if card_type == "C" else "bopot_OutgoingPayments",
               "DocType": "rCustomer" if card_type == "C" else "rSupplier", "CardCode": card["CardCode"],
               "DocDate": f"{when:%Y-%m-%d}", "Remarks": _note(remarks, code), "PaymentInvoices": applied}
    if acct:
        payload.update({"TransferAccount": acct, "TransferSum": round(pay, 2), "TransferDate": f"{when:%Y-%m-%d}"})
        if reference:
            payload["TransferReference"] = reference[:27]
    elif reference:
        payload["CounterReference"] = reference[:8]
    word = "from" if card_type == "C" else "to"
    header = (f"**{name} (draft preview)** {word} **{card['CardName']}** ({card['CardCode']}) · Date: {when:%d %b %Y}"
              + (f" · Bank transfer: {acct_name}" if acct else "") + (f" · Ref: {reference}" if reference else ""))
    table = ("| Invoice | Due | Still open | Paid now |\n|---|---|---|---|\n" + "\n".join(rows)
             + f"\n\n**Total paid: {money(pay, cur)}**" + (f" (of {money(total_due, cur)} open)" if pay < total_due - 0.005 else ""))
    done = (f"**Payment draft created in B1:** {name} draft no. **{{entry}}** {word} {{partner}}, {money(pay, cur)}. Open it in "
            "B1 (Banking > Payment Drafts Report), check it and add it.")
    return offer(name, k, header, table, pay, cur, warnings, "/PaymentDrafts", payload,
                 f"{card['CardName']} ({card['CardCode']})", access.current(), code, done,
                 "The result is already shown. Confirm it in one sentence; it is a draft until someone adds it in B1.")


# ---------------------------------------------------------------- production orders

def prepare_b1_production_order(item: str, quantity: float, due_date: str = "", warehouse: str = "", remarks: str = "") -> dict:
    """A production order for an item with a bill of materials, created as Planned (nothing is released or issued)."""
    name = "Production Order"
    if (e := _not_allowed("production_order", name)):
        return e
    it = _stock_item(item)
    if isinstance(it, str):
        return {"error": it}
    bom = query("SELECT Code, ToWH, Qauntity FROM OITT WHERE Code = ?", (it["code"],))
    if bom.empty:
        return {"error": f"{it['code']} has no bill of materials in B1, so it cannot be produced."}
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return {"error": f"'{quantity}' is not a quantity."}
    if q <= 0:
        return {"error": "The quantity must be more than 0."}
    due = _date(due_date, _today() + timedelta(days=7))
    if isinstance(due, str):
        return {"error": due}
    if due < _today():
        return {"error": "The due date is in the past."}
    whs = _warehouse(warehouse or bom.ToWH.iloc[0] or "01")
    if isinstance(whs, str):
        return {"error": whs}
    comps = query("SELECT c.Code, i.ItemName, c.Quantity, c.Warehouse, i.InvntItem FROM ITT1 c JOIN OITM i ON i.ItemCode = c.Code "
                  "WHERE c.Father = ? ORDER BY c.ChildNum", (it["code"],))
    per = float(bom.Qauntity.iloc[0] or 1)
    rows, warnings = [], []
    for c in comps.itertuples():
        need = float(c.Quantity) * q / per
        have = _on_hand(c.Code, c.Warehouse or whs["code"]) if c.InvntItem == "Y" else None
        rows.append(f"| {c.Code} | {c.ItemName} | {qty(need)} | {c.Warehouse or whs['code']} | {qty(have) if have is not None else '-'} |")
        if have is not None and have < need:
            warnings.append(f"{c.ItemName}: only {qty(have)} in warehouse {c.Warehouse or whs['code']} for {qty(need)} needed")
    code = new_code()
    payload = {"ItemNo": it["code"], "PlannedQuantity": q, "DueDate": f"{due:%Y-%m-%d}", "PostingDate": f"{_today():%Y-%m-%d}",
               "Warehouse": whs["code"], "ProductionOrderStatus": "boposPlanned", "ProductionOrderType": "bopotStandard",
               "Remarks": _note(remarks, code)}
    header = (f"**{name} (preview, status Planned)** · **{it['name']}** ({it['code']}) · {qty(q)} to make in {whs['code']} · "
              f"Due: {due:%d %b %Y}")
    table = "| Component | Description | Needed | Whs | In stock |\n|---|---|---|---|---|\n" + "\n".join(rows or ["| - | (no components) | | | |"])
    done = (f"**Production order no. {{number}} created in B1 with status Planned** for {qty(q)} {it['name']}. Nothing is "
            "released or issued: open it in B1 (Production > Production Order), check it and set it to Released when ready.")
    return offer(name, "production_order", header, table, 0.0, label(_local()), warnings, "/ProductionOrders", payload,
                 it["name"], access.current(), code, done,
                 "The result is already shown. Confirm it in one sentence; it is Planned until someone releases it in B1.",
                 id_field="AbsoluteEntry", number_field="DocumentNumber")


# ---------------------------------------------------------------- journal entries

def prepare_b1_journal_entry(lines: str, memo: str = "", doc_date: str = "", reference: str = "") -> dict:
    """A journal entry as a JOURNAL VOUCHER (not posted): lines like '6100 debit 500; 1100 credit 500'. An account is a
    G/L account code / segment code / name, or a customer or supplier code (posted to their control account)."""
    name = "Journal Entry"
    if (e := _not_allowed("journal_entry", name)):
        return e
    when = _date(doc_date, _today())
    if isinstance(when, str):
        return {"error": when}
    parts = [p.strip() for p in re.split(r"[;\n]+", lines or "") if p.strip()]
    rows, jl, problems = [], [], []
    debit = credit = 0.0
    for p in parts:
        m = re.match(r"(.+?)\s+(debit|credit|dr|cr|d|c)\s*[: ]\s*([\d.,]+)\s*$", p, re.I)
        if not m:
            problems.append(f"'{p}': write it as '<account> debit|credit <amount>'")
            continue
        acc_text, side, amt = m.group(1).strip(), m.group(2).lower()[0], float(m.group(3).replace(",", ""))
        if amt <= 0:
            problems.append(f"'{p}': the amount must be more than 0")
            continue
        a = query("SELECT AcctCode, AcctName, FormatCode, Postable, LocManTran FROM OACT WHERE AcctCode = ? OR FormatCode = ? "
                  "OR REPLACE(FormatCode, '-', '') = ?", (acc_text, acc_text, acc_text.replace("-", "")))
        if a.empty:
            a = query("SELECT TOP 3 AcctCode, AcctName, FormatCode, Postable, LocManTran FROM OACT WHERE AcctName LIKE ? AND Postable = 'Y'",
                      (f"%{acc_text}%",))
        line = None
        if len(a) == 1:
            r = a.iloc[0]
            if r.Postable != "Y":
                problems.append(f"{r.FormatCode or r.AcctCode} {r.AcctName} is a title account (nothing can be posted to it)")
                continue
            if r.LocManTran == "Y":
                problems.append(f"{r.FormatCode or r.AcctCode} {r.AcctName} is a control account; use the customer or supplier code instead")
                continue
            line, shown = {"AccountCode": r.AcctCode}, f"{r.FormatCode or r.AcctCode} {r.AcctName}"
        elif a.empty:
            bp = query("SELECT CardCode, CardName, DebPayAcct FROM OCRD WHERE CardCode = ?", (acc_text,))
            if bp.empty:
                problems.append(f"'{acc_text}': no such G/L account, customer or supplier")
                continue
            line, shown = {"ShortName": bp.CardCode.iloc[0], "AccountCode": bp.DebPayAcct.iloc[0]}, f"{bp.CardCode.iloc[0]} {bp.CardName.iloc[0]}"
        else:
            problems.append(f"'{acc_text}': several accounts match (" + ", ".join(f"{x.FormatCode or x.AcctCode} {x.AcctName}" for x in a.itertuples()) + ")")
            continue
        line.update({"Debit": amt if side == "d" else 0.0, "Credit": amt if side == "c" else 0.0, "LineMemo": (memo or "")[:50]})
        jl.append(line)
        debit += line["Debit"]
        credit += line["Credit"]
        rows.append((shown, line["Debit"], line["Credit"]))
    if problems:
        return {"error": "Fix these first: " + "; ".join(problems)}
    if len(jl) < 2:
        return {"error": "A journal entry needs at least two lines (debit and credit)."}
    cur = label(_local())
    if abs(debit - credit) > 0.005:
        return {"error": f"Debits ({money(debit, cur)}) and credits ({money(credit, cur)}) must be equal."}
    code = new_code()
    entry = {"ReferenceDate": f"{when:%Y-%m-%d}", "Memo": ((memo or "Journal entry") + f" (AI chat {code})")[:50],
             "JournalEntryLines": jl}
    if reference:
        entry["Reference"] = reference[:100]
    table = ("| Account | Debit | Credit |\n|---|---|---|\n"
             + "\n".join(f"| {a} | {money(d, cur) if d else ''} | {money(c, cur) if c else ''} |" for a, d, c in rows)
             + f"\n| **Total** | **{money(debit, cur)}** | **{money(credit, cur)}** |")
    header = (f"**Journal Entry (preview, as a journal voucher)** · Date: {when:%d %b %Y}" + (f" · {memo}" if memo else "")
              + (f" · Ref: {reference}" if reference else ""))
    done = ("**Journal voucher created in B1** (not posted). Open it in B1 (Financials > Journal Vouchers), check it and "
            "post it there when ready.")
    return offer(name, "journal_entry", header, table, debit, cur, [], "/JournalVouchersService_Add",
                 {"JournalVoucher": [entry]}, memo or "journal voucher", access.current(), code, done,
                 "The result is already shown. Confirm it in one sentence; nothing is posted until someone posts the voucher in B1.")
