"""Where do I do this in B1? (Joule-style "navigational" help.)

A short guide to common SAP Business One tasks: menu path, steps and tips, searchable in English and Malay.
Menu names are those of SAP Business One 10.0; other versions may name a few entries slightly differently.
When the user names a document ("credit memo for invoice 1049"), the document is looked up read-only so the answer
can say whether it can still be copied on, and which lines are still open.
"""

import re

from .db import query
from .summaries import DOCS, _kind, _num, _status

# Each task: id, title, menu path, steps, words people use (English and Malay), optional source document kind
# (the document this one is usually copied from) and tips.
GUIDE = [
    # ---------- Sales ----------
    {"id": "sales_quotation", "task": "Create a sales quotation", "path": "Sales - A/R > Sales Quotation",
     "steps": ["Choose the customer (Tab in the Customer field opens the list).", "Set the valid-until date.",
               "Add the items, quantities and prices on the Contents tab.", "Click Add."],
     "words": ["quotation", "quote", "sales quotation", "sebut harga", "quotation pelanggan"], "tips": [
         "Later, open it and use Copy To > Sales Order so nothing is retyped."]},
    {"id": "sales_order", "task": "Create a sales order", "path": "Sales - A/R > Sales Order",
     "steps": ["Choose the customer.", "Set the Delivery Date.", "Either click Copy From > Sales Quotations, or add items by hand.",
               "Check prices, warehouse and quantities.", "Click Add."],
     "words": ["sales order", "customer order", "pesanan jualan", "pesanan pelanggan", "order pelanggan"],
     "source": "sales_quotation", "tips": ["Committed stock rises as soon as the order is added."]},
    {"id": "delivery", "task": "Deliver goods to a customer (delivery order)", "path": "Sales - A/R > Delivery",
     "steps": ["Choose the customer.", "Click Copy From > Sales Orders and pick the order(s) and lines.",
               "Adjust quantities for a part delivery.", "Click Add (stock goes out now)."],
     "words": ["delivery", "deliver", "delivery order", "ship", "dispatch", "penghantaran", "hantar barang", "nota hantaran"],
     "source": "sales_order", "tips": ["Part deliveries leave the rest of the order open."]},
    {"id": "ar_invoice", "task": "Create a customer invoice (A/R invoice)", "path": "Sales - A/R > A/R Invoice",
     "steps": ["Choose the customer.", "Click Copy From > Deliveries (or Sales Orders if there was no delivery).",
               "Check the lines, tax and due date.", "Click Add."],
     "words": ["invoice", "a/r invoice", "ar invoice", "bill customer", "customer invoice", "sales invoice", "invois", "bil pelanggan"],
     "source": "delivery", "tips": ["Copying from a sales order without a delivery also takes the stock out.",
                                    "A/R Invoice + Payment (same menu) is for cash sales."]},
    {"id": "ar_credit_memo", "task": "Credit a customer (A/R credit memo)", "path": "Sales - A/R > A/R Credit Memo",
     "steps": ["Open the A/R invoice and click Copy To > A/R Credit Memo (or in a new credit memo, Copy From > A/R Invoices).",
               "Keep only the lines and quantities to credit.",
               "For a price-only credit, untick stock (or use a service-type credit memo) so no stock comes back.",
               "Click Add."],
     "words": ["credit memo", "credit note", "cn", "refund customer", "nota kredit", "kredit pelanggan", "pulang wang"],
     "source": "ar_invoice", "tips": ["Copying from the invoice links them. On an unpaid invoice the amount owed goes down.",
                                      "A paid (closed) invoice can still be credited: the customer then has a credit on account, "
                                      "used on a later invoice or refunded with an outgoing payment."]},
    {"id": "return", "task": "Take goods back from a customer (return)", "path": "Sales - A/R > Return",
     "steps": ["Choose the customer.", "Click Copy From > Deliveries.", "Keep the returned quantities.", "Click Add (stock comes back)."],
     "words": ["return", "customer return", "goods back", "pemulangan", "pulang barang", "barang dipulangkan"],
     "source": "delivery", "tips": ["If the goods were already invoiced, make an A/R credit memo from the invoice instead.",
                                     "Return Request (same menu) records the request before the goods arrive."]},
    {"id": "ar_down_payment", "task": "Ask a customer for a deposit (down payment)",
     "path": "Sales - A/R > A/R Down Payment Request (or A/R Down Payment Invoice)",
     "steps": ["Create it from the sales order (Copy To > A/R Down Payment).", "Set the percentage or amount.",
               "Click Add, then receive the money in Incoming Payments.",
               "On the final A/R invoice, use Total Down Payment to deduct it."],
     "words": ["down payment", "deposit", "deposit from customer", "take a deposit", "customer deposit", "advance payment", "bayaran pendahuluan", "deposit pelanggan"],
     "source": "sales_order"},
    {"id": "cancel_document", "task": "Cancel an invoice or other document",
     "path": "Open the document > Data menu > Cancel (or right-click > Cancel)",
     "steps": ["Find the document (Ctrl+F, type the number, Enter).", "Choose Data > Cancel.",
               "B1 creates a cancellation document on the date you confirm."],
     "words": ["!cancel", "!void", "reverse invoice", "!batal", "!batalkan", "batal invois"],
     "tips": ["A document that has been copied on (e.g. an invoice with a payment) must have that cancelled first.",
              "Orders and quotations are closed instead (Data > Close) when they won't go ahead."]},
    {"id": "close_document", "task": "Close an open sales or purchase order", "path": "Open the order > Data menu > Close (or right-click > Close)",
     "steps": ["Find the order.", "Choose Data > Close and confirm."],
     "words": ["!close", "close order", "close sales order", "close po", "!tutup", "tutup pesanan", "tutup order"],
     "tips": ["To close only some lines, right-click a line > Close Row."]},
    {"id": "dunning", "task": "Send payment reminders (dunning)", "path": "Sales - A/R > Dunning Wizard",
     "steps": ["Start a new run, choose the customers and the cut-off date.", "Review the recommended letters.",
               "Print or email the letters and finish the run."],
     "words": ["dunning", "payment reminder", "reminder letter", "chase payment", "overdue letter", "peringatan bayaran",
               "surat peringatan", "kutip hutang"],
     "tips": ["Dunning levels and letters are set in Administration > Setup > Business Partners > Dunning Terms."]},

    # ---------- Purchasing ----------
    {"id": "purchase_request", "task": "Request a purchase (internal)", "path": "Purchasing - A/P > Purchase Request",
     "steps": ["Enter the requester and required date.", "Add items and quantities.", "Click Add; purchasing copies it to a PO."],
     "words": ["purchase request", "requisition", "pr", "permohonan belian", "permintaan belian"]},
    {"id": "purchase_quotation", "task": "Ask a supplier for a price (purchase quotation)", "path": "Purchasing - A/P > Purchase Quotation",
     "steps": ["Choose the supplier.", "Add items, quantities and the required date.", "Click Add."],
     "words": ["purchase quotation", "rfq", "supplier quote", "sebut harga pembekal"]},
    {"id": "purchase_order", "task": "Create a purchase order", "path": "Purchasing - A/P > Purchase Order",
     "steps": ["Choose the supplier.", "Set the Delivery Date.", "Copy From > Purchase Quotations / Purchase Requests, or add items.",
               "Check prices and warehouse.", "Click Add."],
     "words": ["purchase order", "po", "order from supplier", "buy", "pesanan belian", "pesanan pembekal", "beli barang"],
     "source": "purchase_quotation", "tips": ["MRP > Order Recommendation can create POs from the MRP run.",
                                               "Ask this chat 'what needs ordering' for suggested quantities."]},
    {"id": "goods_receipt_po", "task": "Receive goods from a supplier (GRPO)", "path": "Purchasing - A/P > Goods Receipt PO",
     "steps": ["Choose the supplier.", "Click Copy From > Purchase Orders and pick the PO lines.",
               "Enter the quantities actually received.", "Click Add (stock comes in now)."],
     "words": ["goods receipt po", "grpo", "receive goods", "goods received", "terima barang", "penerimaan barang", "barang sampai"],
     "source": "purchase_order", "tips": ["Part receipts leave the rest of the PO open."]},
    {"id": "ap_invoice", "task": "Enter a supplier invoice (A/P invoice)", "path": "Purchasing - A/P > A/P Invoice",
     "steps": ["Choose the supplier and enter their invoice number in Vendor Ref. No.",
               "Click Copy From > Goods Receipt POs (so price and quantity are matched).", "Check the totals against the paper invoice.",
               "Click Add."],
     "words": ["a/p invoice", "ap invoice", "supplier invoice", "vendor invoice", "purchase invoice", "bill from supplier",
               "invois pembekal", "bil pembekal"],
     "source": "goods_receipt_po", "tips": ["Always fill Vendor Ref. No.; this chat's nightly check flags duplicate supplier invoices with it."]},
    {"id": "ap_credit_memo", "task": "Get a credit from a supplier (A/P credit memo)", "path": "Purchasing - A/P > A/P Credit Memo",
     "steps": ["Open the A/P invoice and click Copy To > A/P Credit Memo.", "Keep the lines and quantities credited.", "Click Add."],
     "words": ["a/p credit memo", "ap credit memo", "supplier credit note", "debit note", "nota kredit pembekal", "nota debit"],
     "source": "ap_invoice"},
    {"id": "goods_return", "task": "Return goods to a supplier", "path": "Purchasing - A/P > Goods Return",
     "steps": ["Choose the supplier.", "Copy From > Goods Receipt POs.", "Keep the returned quantities.", "Click Add (stock goes out)."],
     "words": ["goods return", "return to supplier", "return goods to supplier", "supplier return", "return to vendor", "pulang barang pembekal", "pemulangan pembekal"],
     "source": "goods_receipt_po", "tips": ["If the supplier invoice is already entered, make an A/P credit memo instead."]},
    {"id": "landed_costs", "task": "Add freight and duty to stock cost (landed costs)", "path": "Purchasing - A/P > Landed Costs",
     "steps": ["Copy From > Goods Receipt POs (or A/P invoices).", "Enter freight, duty and other costs on the Costs tab.",
               "Choose how they are allocated (by value, quantity, weight...).", "Click Add."],
     "words": ["landed cost", "freight", "duty", "import cost", "kos import", "kos penghantaran", "duti"]},

    # ---------- Banking ----------
    {"id": "incoming_payment", "task": "Record money received from a customer", "path": "Banking > Incoming Payments > Incoming Payments",
     "steps": ["Choose the customer; their open invoices appear.", "Tick the invoices being paid and the amounts.",
               "Click Payment Means (or Ctrl+B) and enter transfer, cheque, cash or card.", "Click Add."],
     "words": ["incoming payment", "receive payment", "customer payment", "record payment", "customer paid", "payment received", "receipt", "terima bayaran",
               "bayaran masuk", "resit", "pelanggan bayar"],
     "source": "ar_invoice", "tips": ["Payment on account (no invoice ticked) can be matched later in Internal Reconciliation."]},
    {"id": "outgoing_payment", "task": "Pay a supplier", "path": "Banking > Outgoing Payments > Outgoing Payments",
     "steps": ["Choose the supplier; their open invoices appear.", "Tick the invoices to pay.",
               "Click Payment Means and enter the bank transfer or cheque.", "Click Add."],
     "words": ["outgoing payment", "!pay", "pay supplier", "pay a supplier", "pay vendor", "!bayar", "payment to supplier", "bayar pembekal", "bayaran keluar"],
     "source": "ap_invoice", "tips": ["Banking > Payment Wizard pays many suppliers in one run."]},
    {"id": "payment_wizard", "task": "Pay many suppliers at once", "path": "Banking > Payment Wizard",
     "steps": ["Start a new run and choose outgoing payments.", "Select suppliers and the due-date range.",
               "Review the recommendation and run it."],
     "words": ["payment wizard", "payment run", "batch payment", "many suppliers", "at once", "bayar beramai", "bayaran pukal"]},
    {"id": "deposit", "task": "Deposit cheques or cash in the bank", "path": "Banking > Deposit > Deposit",
     "steps": ["Choose the Check, Cash or Credit Card tab.", "Tick what is being deposited and the bank account.", "Click Add."],
     "words": ["deposit cheque", "deposit cheques", "bank in", "deposit cash", "in the bank", "masuk bank", "deposit cek"]},
    {"id": "bank_reconciliation", "task": "Reconcile the bank account",
     "path": "Banking > Bank Statements and Reconciliations > Reconciliation (or Bank Statement Processing)",
     "steps": ["Choose the bank G/L account and the statement ending balance and date.",
               "Match the statement lines with the B1 entries.", "Click Reconcile."],
     "words": ["bank reconciliation", "reconcile bank", "bank statement", "penyesuaian bank", "penyata bank", "rekonsiliasi"]},

    # ---------- Business partners ----------
    {"id": "business_partner", "task": "Add or change a customer or supplier", "path": "Business Partners > Business Partner Master Data",
     "steps": ["For a new one switch to Add mode (Ctrl+A); to change one, Find (Ctrl+F) by code or name.",
               "Choose Customer or Vendor and fill code, name, contacts and addresses.",
               "Set payment terms and price list on the Payment Terms tab.", "Click Add or Update."],
     "words": ["new customer", "add customer", "new supplier", "add supplier", "business partner", "bp master", "customer details",
               "change address", "pelanggan baru", "pembekal baru", "tambah pelanggan", "tambah pembekal", "maklumat pelanggan"]},
    {"id": "credit_limit", "task": "Set a customer's credit limit", "path": "Business Partners > Business Partner Master Data > Payment Terms tab",
     "steps": ["Find the customer.", "On the Payment Terms tab enter Credit Limit (and Commitment Limit).", "Click Update."],
     "words": ["credit limit", "had kredit", "limit kredit"],
     "tips": ["Whether B1 warns or blocks above the limit is set in Administration > System Initialization > Document Settings."]},
    {"id": "payment_terms", "task": "Set up payment terms (e.g. Net 30)", "path": "Administration > Setup > Business Partners > Payment Terms",
     "steps": ["Add a row with the code, due-date basis and days.", "Click Update; assign it in the business partner's Payment Terms tab."],
     "words": ["payment terms", "net 30", "terma bayaran", "syarat bayaran"]},
    {"id": "special_prices", "task": "Give a customer special prices", "path": "Inventory > Price Lists > Special Prices > Special Prices for Business Partners",
     "steps": ["Choose the customer.", "Add the items with the special price or discount and validity dates.", "Click Update."],
     "words": ["special price", "customer price", "discount for customer", "harga khas", "diskaun pelanggan"]},
    {"id": "activity", "task": "Log a call, meeting or task with a customer", "path": "Business Partners > Activity",
     "steps": ["Choose the activity type and business partner.", "Enter the date, time and notes.", "Click Add."],
     "words": ["activity", "follow up", "log call", "meeting", "aktiviti", "susulan"]},

    # ---------- Inventory ----------
    {"id": "item_master", "task": "Add or change an item", "path": "Inventory > Item Master Data",
     "steps": ["Add mode (Ctrl+A) for a new item, or Find (Ctrl+F).", "Fill code, description, item group and units.",
               "Set the preferred vendor and planning data (lead time, minimum stock).", "Click Add or Update."],
     "words": ["new item", "add item", "item master", "product code", "barang baru", "tambah barang", "item baru", "kod barang"]},
    {"id": "price_list", "task": "Change item prices (price list)", "path": "Inventory > Price Lists > Price Lists",
     "steps": ["Open the price list.", "Find the item and type the new price (or use Update by base price / factor).", "Click Update."],
     "words": ["price list", "change price", "update price", "selling price", "senarai harga", "tukar harga", "harga jualan"]},
    {"id": "goods_receipt", "task": "Add stock without a PO (goods receipt)", "path": "Inventory > Inventory Transactions > Goods Receipt",
     "steps": ["Choose the warehouse and items.", "Enter quantities and the cost.", "Click Add."],
     "words": ["goods receipt", "add stock", "stock in", "stok masuk", "tambah stok"],
     "tips": ["For goods from a supplier use Goods Receipt PO instead."]},
    {"id": "goods_issue", "task": "Take stock out (write-off, own use, samples)", "path": "Inventory > Inventory Transactions > Goods Issue",
     "steps": ["Choose the warehouse and items.", "Enter quantities and the reason in Remarks.", "Click Add."],
     "words": ["goods issue", "write off", "stock out", "damaged stock", "sample", "stok keluar", "hapus kira", "barang rosak"]},
    {"id": "inventory_transfer", "task": "Move stock between warehouses", "path": "Inventory > Inventory Transactions > Inventory Transfer",
     "steps": ["Set From Warehouse and To Warehouse.", "Add items and quantities.", "Click Add."],
     "words": ["transfer", "move stock", "inventory transfer", "warehouse transfer", "pindah stok", "pindah gudang", "pemindahan stok"],
     "tips": ["Inventory Transfer Request (same menu) asks for a transfer first."]},
    {"id": "stock_count", "task": "Count stock and correct it", "path": "Inventory > Inventory Transactions > Inventory Counting Transactions > Inventory Counting",
     "steps": ["Create a counting document for the warehouse and items.", "Enter the counted quantities.",
               "Copy it to Inventory Posting (same menu) to post the differences."],
     "words": ["stock count", "stock take", "stocktake", "physical count", "inventory counting", "kiraan stok", "kira stok", "audit stok"]},
    {"id": "stock_report", "task": "See stock levels in B1", "path": "Inventory > Inventory Reports > Inventory Status (or Inventory in Warehouse Report)",
     "steps": ["Choose items and warehouses.", "Click OK."],
     "words": ["inventory status", "stock report", "stock level report", "laporan stok", "baki stok"],
     "tips": ["This chat answers 'how much stock of X do we have' directly, with live stock."]},
    {"id": "batch_serial", "task": "Manage batch or serial numbers", "path": "Inventory > Item Management > Batches / Serial Numbers",
     "steps": ["Turn on Manage Batch/Serial Numbers in the item master (General tab) before the first transaction.",
               "B1 then asks for batch/serial numbers on receipts and deliveries."],
     "words": ["batch", "serial number", "lot number", "nombor siri", "nombor kelompok"]},

    # ---------- Production & MRP ----------
    {"id": "bom", "task": "Create a bill of materials", "path": "Production > Bill of Materials",
     "steps": ["Enter the parent item and BOM type (Production).", "Add the components and quantities.", "Click Add."],
     "words": ["bill of materials", "bom", "recipe", "components", "senarai bahan", "resipi"]},
    {"id": "production_order", "task": "Make finished goods (production order)", "path": "Production > Production Order",
     "steps": ["Choose the product, quantity and due date; components come from the BOM.", "Set status to Released and click Add.",
               "Issue components: Production > Issue for Production.", "Report output: Production > Receipt from Production.",
               "Set the order to Closed when finished."],
     "words": ["production order", "work order", "manufacture", "make product", "pesanan pengeluaran", "arahan kerja", "pengeluaran"]},
    {"id": "mrp", "task": "Plan what to buy and make (MRP)", "path": "MRP > MRP Wizard, then MRP > Order Recommendation",
     "steps": ["Run the MRP Wizard for the items and planning horizon.", "Open Order Recommendation and create POs or production orders."],
     "words": ["mrp", "material planning", "order recommendation", "perancangan bahan"],
     "tips": ["This chat's reorder report already suggests what to order, with lead times and forecasts."]},

    # ---------- Finance ----------
    {"id": "journal_entry", "task": "Post a manual journal entry", "path": "Financials > Journal Entry",
     "steps": ["Enter the posting date and remarks.", "Add debit and credit lines (G/L accounts or business partners).",
               "Check debits equal credits.", "Click Add."],
     "words": ["journal entry", "manual journal", "je", "journal", "jurnal", "catatan jurnal"],
     "tips": ["To reverse one, open it and tick Reverse with a date, or use Financials > Journal Entry > Data > Cancel."]},
    {"id": "chart_of_accounts", "task": "Add or change a G/L account", "path": "Financials > Chart of Accounts",
     "steps": ["Select the drawer and the parent title.", "Click Add, enter the code and name, choose Active Account.", "Click Update."],
     "words": ["chart of accounts", "gl account", "g/l account", "new account", "carta akaun", "akaun baru"]},
    {"id": "posting_periods", "task": "Open or lock a posting period", "path": "Administration > System Initialization > Posting Periods",
     "steps": ["Select the period.", "Set Period Status (Unlocked, Unlocked Except Sales, Closing Period, Locked).", "Click Update."],
     "words": ["posting period", "period", "close month", "lock period", "month end", "tempoh pengeposan", "tutup bulan", "kunci tempoh"]},
    {"id": "exchange_rates", "task": "Enter exchange rates", "path": "Administration > Exchange Rates and Indexes",
     "steps": ["Choose the month.", "Type the rate for each currency and date.", "Click Update."],
     "words": ["exchange rate", "currency rate", "forex", "kadar tukaran", "kadar mata wang"],
     "tips": ["Ask this chat for today's market exchange rates."]},
    {"id": "aging_report", "task": "Customer or supplier aging report", "path": "Financials > Financial Reports > Accounting > Aging > Customer Receivables Aging (or Vendor Liabilities Aging)",
     "steps": ["Choose the business partners and the aging date.", "Choose intervals (e.g. 30 days).", "Click OK."],
     "words": ["aging", "ageing", "aging report", "receivables aging", "debtors report", "laporan umur hutang", "laporan penghutang"],
     "tips": ["This chat answers 'who owes us money' directly, and can chart it by age."]},
    {"id": "financial_statements", "task": "Balance sheet, profit and loss, trial balance",
     "path": "Financials > Financial Reports > Financial > Balance Sheet / Profit and Loss Statement / Trial Balance",
     "steps": ["Choose the period (posting date from/to).", "Choose the template if you have more than one.", "Click OK."],
     "words": ["balance sheet", "profit and loss", "p&l", "pnl", "trial balance", "income statement", "kunci kira-kira",
               "untung rugi", "imbangan duga", "penyata kewangan"]},
    {"id": "sales_analysis_report", "task": "Sales analysis report in B1", "path": "Sales - A/R > Sales Reports > Sales Analysis",
     "steps": ["Choose by customers, items or sales employees.", "Set the date range.", "Click OK."],
     "words": ["sales analysis", "sales report", "laporan jualan", "analisis jualan"],
     "tips": ["This chat makes the same analysis with charts, and as Excel."]},
    {"id": "open_items", "task": "List open documents (open items list)", "path": "Sales - A/R > Sales Reports > Open Items List",
     "steps": ["Choose the document type (e.g. Sales Orders, A/R Invoices).", "Filter if needed."],
     "words": ["open items", "open orders", "open documents", "dokumen terbuka", "pesanan terbuka"]},

    # ---------- Admin & tools ----------
    {"id": "users", "task": "Add a user or change permissions", "path": "Administration > Setup > General > Users; Administration > System Initialization > Authorizations > General Authorizations",
     "steps": ["In Users, add the user code, name and licence.", "In General Authorizations, choose the user and set Full, Read Only or No Authorization per screen."],
     "words": ["user", "add user", "permission", "authorization", "access", "pengguna", "tambah pengguna", "kebenaran", "akses"]},
    {"id": "approvals", "task": "Set up or check approvals", "path": "Administration > Approval Process > Approval Stages / Approval Templates (Approval Status Report in the same menu)",
     "steps": ["Create Approval Stages (who approves, how many).", "Create an Approval Template: originators, documents and the condition.",
               "Approvers decide in the Approval Decision Report."],
     "words": ["!approval", "!approvals", "!approve", "approval procedure", "!kelulusan", "lulus", "luluskan"]},
    {"id": "document_numbering", "task": "Change document numbering series", "path": "Administration > System Initialization > Document Numbering",
     "steps": ["Double-click the document type.", "Add a series (name, first and last number) and set it as default if needed.", "Click Update."],
     "words": ["document numbering", "numbering series", "series", "nombor dokumen", "siri nombor"]},
    {"id": "alerts", "task": "Set up B1 alerts", "path": "Administration > Alerts Management",
     "steps": ["Choose a predefined alert or a saved query.", "Set recipients and frequency, tick Active.", "Click Update."],
     "words": ["alert", "notification", "amaran", "pemberitahuan"],
     "tips": ["This chat already sends a morning briefing and stock alerts."]},
    {"id": "query_manager", "task": "Run or save a SQL query in B1", "path": "Tools > Queries > Query Manager (or Query Generator)",
     "steps": ["Open Query Manager, pick a saved query or write a new one in Query Generator.", "Execute; save it to a category."],
     "words": ["query", "query manager", "sql query", "query generator", "pertanyaan sql"],
     "tips": ["You can also just ask this chat the question."]},
    {"id": "relationship_map", "task": "See how documents are linked", "path": "Open the document > right-click > Relationship Map",
     "steps": ["Open the document.", "Right-click > Relationship Map: it shows the quotation, order, delivery, invoice and payment chain."],
     "words": ["relationship map", "document flow", "linked documents", "base document", "target document", "dokumen berkaitan",
               "aliran dokumen"],
     "tips": ["Ask this chat 'show invoice <number>' to see what it was copied from and to."]},
    {"id": "print_email", "task": "Print or email a document", "path": "Open the document > Print (Ctrl+P) or Send > E-Mail",
     "steps": ["Open the document.", "Print Preview to check, then Print; or File > Send > E-Mail."],
     "words": ["!print", "!email", "email invoice", "send invoice", "pdf", "!cetak", "hantar invois", "!emel"],
     "tips": ["Sales - A/R > Document Printing prints many documents at once."]},
    {"id": "duplicate_document", "task": "Copy (duplicate) a document", "path": "Open the document > right-click > Duplicate",
     "steps": ["Open the document to copy.", "Right-click > Duplicate; a new document opens with the same lines.", "Change what differs and click Add."],
     "words": ["!duplicate", "copy document", "same as last", "salin dokumen", "pendua"]},
    {"id": "gross_profit", "task": "Check the profit on a document", "path": "Open the document > Gross Profit button (or Goto > Gross Profit)",
     "steps": ["Open the sales document.", "Click the Gross Profit button in the toolbar."],
     "words": ["gross profit on", "profit on", "check the profit", "margin on invoice", "untung kasar", "margin dokumen"]},
]

_DOC_NUMBER = re.compile(r"\b(supplier invoice|vendor invoice|purchase invoice|supplier credit note|sales invoice|invois pembekal|quotation|quote|sales order|order|so|delivery|do|invoice|inv|credit memo|credit note|cn|po|"
                         r"purchase order|grpo|goods receipt po|ap invoice|a/p invoice|bill|invois|pesanan|nota kredit)\s*(?:no\.?|number|#)?\s*(\d+)\b",
                         re.I)


_LOCAL_KINDS = {"pesanan": "sales_order", "invois": "ar_invoice", "nota kredit": "ar_credit_memo", "vendor invoice": "ap_invoice",
                "invois pembekal": "ap_invoice"}


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9/&]+", " ", (text or "").lower()) + " "


def _score(q: str, entry: dict) -> float:
    s = 0.0
    for w in entry["words"]:
        strong = w.startswith("!")  # an action word that decides the task ("cancel" an invoice)
        w = w.lstrip("!")
        if f" {w} " in q:
            s += 6 if strong else 2 * len(w.split()) + 0.5 * (w.count(" ") > 0)
    for w in re.findall(r"[a-z]{4,}", entry["task"].lower()):
        if f" {w} " in q:
            s += 0.5
    return s


def _document_check(kind: str, number: int) -> dict:
    """Read-only check of the document the user named: status, partner, lines still open to copy on, linked documents."""
    if kind not in DOCS or kind in ("incoming_payment", "outgoing_payment"):
        return {}
    head, lines, _, name, _ = DOCS[kind]
    h = query(f"SELECT DocEntry, DocNum, DocType, DocDate, CardCode, CardName, DocStatus, CANCELED, DocTotal, PaidToDate "
              f"FROM {head} WHERE DocNum = ?", (number,))
    if h.empty:
        return {"document": f"{name} {number}", "error": f"There is no {name} number {number} in B1."}
    r = h.iloc[0].to_dict()
    ln = query(f"SELECT ItemCode, Dscription, Quantity, OpenQty, LineStatus FROM {lines} WHERE DocEntry = ? ORDER BY VisOrder",
               (r["DocEntry"],))
    still = ln[(ln.LineStatus == "O") & (ln.OpenQty > 0)]
    out = {"document": f"{name} {number}", "partner": f"{r['CardName']} ({r['CardCode']})",
           "date": str(r["DocDate"])[:10], "status": _status(r), "total_incl_tax": _num(r["DocTotal"]),
           "lines": len(ln), "lines_still_open_to_copy": [
               {"item": i, "description": d, "open_qty": _num(q)} for i, d, q in zip(still.ItemCode, still.Dscription, still.OpenQty)]}
    if r["DocType"] == "S":  # service document: lines carry amounts, not quantities
        out["lines_still_open_to_copy"] = "service document (amounts only)" if out["status"] == "Open" else []
    if kind in ("ar_invoice", "ap_invoice"):
        out["paid"] = _num(r["PaidToDate"])
    if len(h) > 1:
        out["note"] = f"{len(h)} documents have this number (different series); the first is shown."
    return out


def b1_howto(question: str, document_kind: str = "", number: int = 0) -> dict:
    """Menu path, steps and tips for a B1 task; checks the named document when there is one."""
    # The document the user named ("credit memo for invoice 1049"): from the arguments or the question itself.
    # Its name is left out of the matching, so "deliver sales order 52" means delivery, not creating an order.
    m = _DOC_NUMBER.search(question or "")
    if not number and m:
        document_kind, number = document_kind or _LOCAL_KINDS.get(m.group(1).lower(), m.group(1)), int(m.group(2))
    q = _words(question[:m.start()] + " " + question[m.end():] if m else question)
    ranked = sorted(((_score(q, e), i, e) for i, e in enumerate(GUIDE)), key=lambda t: (-t[0], t[1]))
    hits = [e for s, _, e in ranked if s > 0]
    if not hits:
        return {"note": "No guide entry matches. Say plainly that this task is not in the guide and suggest the B1 Help "
                        "(F1 in B1) or asking the B1 partner; do not guess a menu path.",
                "tasks_in_guide": [e["task"] for e in GUIDE]}
    top = [e for s, _, e in ranked[:3] if s > 0 and s >= ranked[0][0] * 0.6]
    out = {"version_note": "Menu names are from SAP Business One 10.0.", "matches": [
        {k: e[k] for k in ("task", "path", "steps", "tips") if k in e} for e in top]}

    kind = _kind(document_kind) if document_kind else top[0].get("source")
    if number and kind:
        check = _document_check(kind, int(number))
        if check:
            out["named_document"] = check
            if top[0].get("source") == kind and not check.get("error"):
                out["named_document"]["hint"] = (
                    "Nothing left to copy: every line is already copied on or closed." if check["lines_still_open_to_copy"] == []
                    else f"Use Copy From in the new document (or Copy To in {check['document']}) to bring these lines across.")
    return out
