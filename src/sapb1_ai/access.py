"""Who may see and do what in the chat: a role per person per company, enforced by the chat service (not the AI).

The sign-in page (portal.py) knows who someone is; their company's chat site (Open WebUI) passes their email to this
service with every question (X-OpenWebUI-User-Email). Their role for this company comes from site/users.json:
    "access": {"companya": "sales", "demo": "manager"}
set with  ./company.sh user access <email> <company> <role>.  Sign-in admins are always "manager". Someone without a
role for a company gets DEFAULT_ROLE (manager, i.e. everything, as before roles existed).

A role is a set of AREAS. Each tool belongs to one area (or is open to everyone); tools outside the role are not offered
to the AI, and are refused if it asks anyway. The background figures added to every question follow the same areas.
Drafts in B1 (b1drafts.py) are allowed per area too: a sales role may draft sales documents only, and so on.
"""

import contextvars
import json
import os

from .company import COMPANY, ROOT

USERS_FILE = ROOT / "site" / "users.json"
DEFAULT_ROLE = os.getenv("ACCESS_DEFAULT_ROLE", "manager")

AREAS = {
    "stock": {"inventory_health", "stock_summary", "critical_alerts", "reorder_report", "search_items", "item_details", "stock_levels_chart",
              "inventory_status_chart", "stock_projection_chart"},
    "sales": {"sales_analysis", "sales_trend_chart", "top_chart", "sales_history", "customer_summary", "compare_periods",
              "quotation_pipeline", "customer_insights", "product_profitability", "salesperson_performance", "delivery_performance", "sales_forecast"},
    "collections": {"receivables", "receivables_chart", "collections_plan", "payment_reminder"},
    "purchasing": {"supplier_summary", "supplier_analytics", "purchasing_plan", "supplier_order_email"},
    "finance": {"profit_and_loss", "finance_analysis", "cash_forecast_chart", "transaction_anomalies", "executive_summary", "daily_briefing"},
    "sql": {"run_sql"},
}
# Open to every role: they show no company figures by themselves, or they check the area inside (see allowed()).
GENERAL = {"b1_howto", "company_documents", "market_watch", "web_search", "read_web_page", "fx_rates", "custom_chart",
           "export_excel", "document", "cover_note", "prepare_b1_draft", "create_b1_draft", "prepare_b1_stock_document",
           "prepare_b1_payment", "prepare_b1_production_order", "prepare_b1_journal_entry",
           "management_pack"}  # (kinds checked by can_draft)

AREA_NAMES = {"stock": "stock and items", "sales": "sales and customers", "collections": "what customers owe and collections",
              "purchasing": "suppliers and purchasing", "finance": "cash, finance, management overview and transaction checks",
              "sql": "free database questions"}

ROLES = {  # role -> (areas it may see, areas it may create B1 drafts in)
    "manager": (set(AREAS), set(AREAS)),
    "sales": ({"stock", "sales", "collections"}, {"sales"}),
    "purchasing": ({"stock", "purchasing"}, {"purchasing"}),
    "finance": ({"sales", "collections", "purchasing", "finance"}, {"finance"}),
    "warehouse": ({"stock"}, {"stock"}),
    "none": (set(), set()),  # signed in but unknown here: general help only
}

# B1 documents by area: for looking one up (document, cover_note) and for drafts.
DOC_AREA = {
    "sales_quotation": "sales", "sales_order": "sales", "delivery": "sales", "return": "sales", "ar_down_payment": "sales",
    "ar_invoice": "sales", "ar_credit_memo": "sales",
    "purchase_quotation": "purchasing", "purchase_order": "purchasing", "goods_receipt_po": "purchasing",
    "goods_return": "purchasing", "ap_invoice": "purchasing", "ap_credit_memo": "purchasing", "purchase_request": "purchasing",
    "incoming_payment": "collections", "outgoing_payment": "finance",
    "inventory_transfer": "stock", "transfer_request": "stock", "goods_receipt": "stock", "goods_issue": "stock",
    "return_request": "sales", "goods_return_request": "purchasing", "ar_reserve_invoice": "sales",
    "ap_down_payment": "purchasing", "inventory_counting": "stock", "inventory_posting": "stock",
    "production_order": "stock", "journal_entry": "finance",
}
# Drafting: some documents belong to more than one job (a delivery is sales and warehouse work, an invoice also finance).
DRAFT_AREAS = {"delivery": {"sales", "stock"}, "return": {"sales", "stock"}, "ar_invoice": {"sales", "finance"},
               "ar_credit_memo": {"sales", "finance"}, "goods_receipt_po": {"purchasing", "stock"},
               "goods_return": {"purchasing", "stock"}, "ap_invoice": {"purchasing", "finance"},
               "ap_credit_memo": {"purchasing", "finance"},
               "purchase_request": {"purchasing", "sales", "stock", "finance"},  # anyone may ask for something to be bought
               "ar_down_payment": {"sales", "finance"}, "ar_reserve_invoice": {"sales", "finance"},
               "ap_down_payment": {"purchasing", "finance"}, "return_request": {"sales", "stock"},
               "goods_return_request": {"purchasing", "stock"}, "inventory_posting": {"stock", "finance"},
               "incoming_payment": {"collections", "finance"}, "outgoing_payment": {"finance"}}
LIST_AREA = {"customers": "sales", "suppliers": "purchasing", "items": "stock"}  # export_excel ready-made lists

_current: contextvars.ContextVar[dict] = contextvars.ContextVar("access_user", default={})


def user_for(email: str | None, name: str | None = None) -> dict:
    """The person asking and their role for this company. No email = a local script on this PC (manager)."""
    email = (email or "").strip().lower()
    if not email:
        return {"email": "", "name": "", "role": "manager", "source": "no user (local)"}
    try:
        users = json.loads(USERS_FILE.read_text())["users"]
    except (OSError, ValueError, KeyError):
        users = {}
    u = users.get(email)
    if u is None:
        role = "none"
    elif u.get("role") == "admin":
        role = "manager"
    else:
        role = (u.get("access") or {}).get(COMPANY) or DEFAULT_ROLE
    if role not in ROLES:
        role = "none"
    return {"email": email, "name": (u or {}).get("name") or name or email, "role": role}


def set_user(user: dict) -> None:
    _current.set(user)


def current() -> dict:
    return _current.get() or {"email": "", "name": "", "role": "manager"}


def areas(user: dict | None = None) -> set[str]:
    return ROLES[(user or current())["role"]][0]


def draft_areas(user: dict | None = None) -> set[str]:
    return ROLES[(user or current())["role"]][1]


def can_draft(kind: str, user: dict | None = None) -> bool:
    return bool(DRAFT_AREAS.get(kind, {DOC_AREA.get(kind)}) & draft_areas(user))


def _doc_kind(text: str) -> str | None:
    from .summaries import _kind
    return _kind(text)


def allowed(tool: str, args: dict | None = None, user: dict | None = None) -> str | None:
    """None if the tool (with these arguments) is allowed, else the reason for the user."""
    user = user or current()
    mine, args = areas(user), args or {}
    role = user["role"]
    if tool in GENERAL:
        if tool in ("document", "cover_note"):
            kind = _doc_kind(str(args.get("kind", "")))
            if kind and DOC_AREA.get(kind) not in mine:
                return f"{kind.replace('_', ' ')} documents are not part of the {role} role"
        if tool == "export_excel":
            inner = str(args.get("tool") or "")
            if inner in LIST_AREA:
                return None if LIST_AREA[inner] in mine else f"the {inner} list is not part of the {role} role"
            if inner:
                return allowed(inner, args.get("arguments") or {}, user)
            if args.get("sql") and "sql" not in mine:
                return f"free database questions are not part of the {role} role"
        return None
    area = next((a for a, tools in AREAS.items() if tool in tools), None)
    if area is None:
        return None  # a tool not listed here: new tools must be added to AREAS or GENERAL
    return None if area in mine else f"{AREA_NAMES[area]} is not part of the {role} role"


def tool_names(user: dict | None = None) -> set[str]:
    mine = areas(user)
    return set(GENERAL) | {t for a in mine for t in AREAS[a]}


def prompt_line(user: dict | None = None) -> str:
    """One paragraph for the assistant's instructions about who is asking and what their role covers."""
    user = user or current()
    if user["role"] == "manager":
        return f"You are talking to {user['name']} (role: manager, may see everything)." if user["email"] else ""
    mine = areas(user)
    sees = ", ".join(AREA_NAMES[a] for a in AREAS if a in mine) or "only general help (how to use B1, company documents)"
    not_ = ", ".join(AREA_NAMES[a] for a in AREAS if a not in mine)
    return (f"You are talking to {user['name']} (role: {user['role']}). Their role covers: {sees}. It does NOT cover: "
            f"{not_}. If they ask for something outside their role, answer in one or two sentences: their role does not include "
            "it and an administrator can change their access. Nothing else about it: no figures, table names, guesses or "
            "workarounds, and never answer it from other data, memory or earlier messages.")
