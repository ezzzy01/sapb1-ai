"""One login for every company's chat site.

People sign in once, at http://<pc>:3100, choosing the company. The proxy (Caddy, site/Caddyfile) then asks
/portal/verify on every request and sends the person only to that company's own Open WebUI (own database, chats,
accounts and files). Open WebUI trusts the X-Sapb1-* headers the proxy adds and has no login of its own; the proxy
removes any such header a browser sends. Another company means: sign out, choose it at login.

Accounts live in site/users.json (private, git-ignored): name, bcrypt password hash, role, and the companies the
person may use (admins: all). Manage them with ./company.sh user ...
  Run:  uv run uvicorn sapb1_ai.portal:app --host 0.0.0.0 --port 8090   (systemd: sapb1-ai-portal.service)
"""

import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import string
import sys
import time
import urllib.parse
from pathlib import Path

import bcrypt
from dotenv import dotenv_values
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

ROOT = Path(__file__).resolve().parents[2]
SITE_DIR = ROOT / "site"
USERS_FILE = SITE_DIR / "users.json"
SECRET_FILE = SITE_DIR / "portal_secret"
COOKIE = "sapb1_session"
SESSION_HOURS = float(os.getenv("PORTAL_SESSION_HOURS", "168"))
MAX_FAILURES, FAILURE_WINDOW_S = 5, 900  # per account and per address


# ---------------------------------------------------------------- companies and accounts

def companies() -> dict[str, str]:
    """Started companies: id -> name. Only these are on the site."""
    out = {}
    for env in sorted((ROOT / "companies").glob("*/company.env")):
        cid = env.parent.name
        if (Path.home() / ".config/systemd/user/default.target.wants" / f"sapb1-ai-agent@{cid}.service").exists():
            out[cid] = dotenv_values(env).get("COMPANY_NAME") or cid
    return out


def _load_users() -> dict:
    return json.loads(USERS_FILE.read_text())["users"] if USERS_FILE.exists() else {}


def _save_users(users: dict) -> None:
    SITE_DIR.mkdir(exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps({"users": users}, indent=1, sort_keys=True))
    tmp.replace(USERS_FILE)


def allowed(user: dict, cid: str) -> bool:
    return user.get("role") == "admin" or cid in user.get("companies", [])


def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SITE_DIR.mkdir(exist_ok=True)
        SECRET_FILE.touch(mode=0o600)
        SECRET_FILE.write_text(secrets.token_urlsafe(48))
    return SECRET_FILE.read_text().strip().encode()


# ---------------------------------------------------------------- session cookie

def _pw_tag(user: dict) -> str:
    """Changes when the password changes, so old sessions stop working."""
    return hashlib.sha256(user["password"].encode()).hexdigest()[:16]


def _sign(data: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _session(request: Request) -> tuple[str, dict, str] | None:
    """(email, account, company id) of a valid session, or None."""
    raw = request.cookies.get(COOKIE, "")
    body, _, mac = raw.partition(".")
    if not body or not hmac.compare_digest(mac, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        return None
    user = _load_users().get(data.get("e", ""))
    cid = data.get("c", "")
    if (not user or data.get("x", 0) < time.time() or data.get("p") != _pw_tag(user)
            or cid not in companies() or not allowed(user, cid)):
        return None
    return data["e"], user, cid


# ---------------------------------------------------------------- failed sign-ins

_failures: dict[str, list[float]] = {}


def _blocked(*keys: str) -> bool:
    now = time.time()
    for k in keys:
        _failures[k] = [t for t in _failures.get(k, []) if now - t < FAILURE_WINDOW_S]
    return any(len(_failures[k]) >= MAX_FAILURES for k in keys)


def _failed(*keys: str) -> None:
    for k in keys:
        _failures.setdefault(k, []).append(time.time())


# ---------------------------------------------------------------- pages

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_TEMPLATE = string.Template((Path(__file__).with_name("portal_page.html")).read_text())

_ICONS = {
    "company": '<path d="M3 21h18M5 21V5a2 2 0 012-2h6a2 2 0 012 2v16M15 9h2a2 2 0 012 2v10M9 7h2M9 11h2M9 15h2"/>',
    "email": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/>',
    "eye": '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',
    "chev": '<path d="M6 9l6 6 6-6"/>',
    "err": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
    "ok": '<circle cx="12" cy="12" r="10"/><path d="M8 12l3 3 5-6"/>',
}


def _svg(name: str, cls: str = "") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>')


def _messages(error: str, note: str) -> str:
    out = ""
    if error:
        out += f'<div class="msg err" role="alert">{_svg("err")}<span>{html.escape(error)}</span></div>'
    if note:
        out += f'<div class="msg ok" role="status">{_svg("ok")}<span>{html.escape(note)}</span></div>'
    return out


def _input(name: str, label: str, icon: str, kind: str = "text", value: str = "", autocomplete: str = "",
           placeholder: str = "", hint: str = "", extra: str = "") -> str:
    reveal = kind == "password"
    button = (f'<button type="button" class="reveal" aria-label="Show password" aria-pressed="false">{_svg("eye")}</button>'
              if reveal else "")
    return (f'<div class="field"><label for="{name}">{label}</label>'
            f'<div class="control{" has-reveal" if reveal else ""}">{_svg(icon, "lead")}'
            f'<input id="{name}" name="{name}" type="{kind}" required value="{html.escape(value)}"'
            f' autocomplete="{autocomplete}" placeholder="{html.escape(placeholder)}" {extra}>{button}</div>'
            + (f'<div class="hint">{hint}</div>' if hint else "") + "</div>")


def _button(label: str, busy: str) -> str:
    return (f'<button type="submit" class="btn" data-busy="{busy}"><span class="spinner"></span>'
            f'<span class="label">{label}</span></button>')


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    r = HTMLResponse(_TEMPLATE.substitute(title=html.escape(title), content=body), status_code=status)
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Frame-Options"] = "DENY"
    return r


def _login_page(error: str = "", email: str = "", company: str = "", note: str = "", status: int = 200) -> HTMLResponse:
    cos = companies()
    opts = "".join(f'<option value="{html.escape(c)}"{" selected" if c == company else ""}>{html.escape(n)}</option>'
                   for c, n in sorted(cos.items(), key=lambda kv: kv[1].lower()))
    body = f"""<h2>Welcome back</h2><p class="sub">Sign in to your company's AI assistant.</p>
{_messages(error, note)}
<form method="post" action="/portal/login">
<div class="field"><label for="company">Company</label>
<div class="control">{_svg("company", "lead")}<select id="company" name="company" required>{opts}</select>{_svg("chev", "chev")}</div></div>
{_input("email", "Email", "email", "email", email, "username", "name@company.com", extra="" if email else "autofocus")}
{_input("password", "Password", "lock", "password", autocomplete="current-password", placeholder="Your password",
        extra="autofocus" if email else "")}
{_button("Sign in", "Signing in…")}
</form>
<div class="links"><a href="/portal/password">Change password</a></div>"""
    return _page("Sign in", body, status)


def _clear_webui(resp: Response) -> None:
    """Drop any Open WebUI session left in the browser (another person or company)."""
    resp.delete_cookie("token", path="/")


@app.get("/portal/login")
def login_form(request: Request, company: str = ""):
    return _login_page(company=company or request.cookies.get("sapb1_last", ""))


@app.post("/portal/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), company: str = Form(...)):
    email = email.strip().lower()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    if _blocked(f"e:{email}", f"i:{ip}"):
        return _login_page("Too many failed sign-ins. Wait 15 minutes and try again.", email, company, status=429)
    user = _load_users().get(email)
    if not user or not bcrypt.checkpw(password.encode(), user["password"].encode()):
        _failed(f"e:{email}", f"i:{ip}")
        return _login_page("Wrong email or password.", email, company, status=401)
    cos = companies()
    if company not in cos or not allowed(user, company):
        return _login_page(f"Your account may not use {cos.get(company, 'that company')}.", email, company, status=403)
    resp = RedirectResponse("/", status_code=303)
    _clear_webui(resp)
    resp.set_cookie(COOKIE, _sign({"e": email, "c": company, "p": _pw_tag(user), "x": int(time.time() + SESSION_HOURS * 3600)}),
                    max_age=int(SESSION_HOURS * 3600), httponly=True, samesite="lax", path="/")
    resp.set_cookie("sapb1_last", company, max_age=365 * 86400, samesite="lax", path="/")
    return resp


@app.get("/portal/verify")
def verify(request: Request):
    """Asked by the proxy before every request to a chat site."""
    s = _session(request)
    if s:
        email, user, cid = s
        r = Response(status_code=200)
        r.headers["X-Sapb1-Email"] = email
        r.headers["X-Sapb1-Name"] = urllib.parse.quote(user.get("name") or email)
        r.headers["X-Sapb1-Role"] = "admin" if user.get("role") == "admin" else "user"
        r.headers["X-Sapb1-Company"] = cid
        r.headers["X-Sapb1-Groups"] = companies()[cid]
        return r
    uri = request.headers.get("x-forwarded-uri", "/")
    if uri.startswith(("/api", "/ws", "/ollama", "/openai", "/static", "/_app")) or request.headers.get("x-forwarded-method", "GET") != "GET":
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return RedirectResponse("/portal/login", status_code=302)


@app.get("/portal/session")
def session(request: Request):
    """For the page script: is the shared sign-in still valid? (No details: just yes or no.)"""
    return JSONResponse({"signed_in": _session(request) is not None}, headers={"Cache-Control": "no-store"})


@app.api_route("/portal/logout", methods=["GET", "POST"])
def logout(request: Request):
    """Also answers Open WebUI's own sign-out button (the proxy sends /api/v1/auths/signout here)."""
    api = request.query_params.get("api") == "1" or request.method == "POST"
    resp = JSONResponse({"status": True}) if api else RedirectResponse("/portal/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    _clear_webui(resp)
    return resp


@app.get("/portal/password")
def password_form():
    return _password_page()


def _password_page(error: str = "", note: str = "", status: int = 200) -> HTMLResponse:
    body = f"""<h2>Change password</h2><p class="sub">Your new password works for every company you use.</p>
{_messages(error, note)}
<form method="post" action="/portal/password">
{_input("email", "Email", "email", "email", autocomplete="username", placeholder="name@company.com", extra="autofocus")}
{_input("old", "Current password", "lock", "password", autocomplete="current-password", placeholder="Your current password")}
{_input("new", "New password", "key", "password", autocomplete="new-password", placeholder="At least 10 characters",
        hint="Use at least 10 characters. A short sentence is easy to remember and hard to guess.", extra='minlength="10"')}
{_button("Change password", "Saving…")}
</form>
<div class="links"><a href="/portal/login">Back to sign in</a></div>"""
    return _page("Change password", body, status)


@app.post("/portal/password")
def change_password(request: Request, email: str = Form(...), old: str = Form(...), new: str = Form(...)):
    email = email.strip().lower()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    if _blocked(f"e:{email}", f"i:{ip}"):
        return _password_page("Too many failed attempts. Wait 15 minutes and try again.", status=429)
    users = _load_users()
    user = users.get(email)
    if not user or not bcrypt.checkpw(old.encode(), user["password"].encode()):
        _failed(f"e:{email}", f"i:{ip}")
        return _password_page("Wrong email or password.", status=401)
    if len(new) < 10:
        return _password_page("The new password must have at least 10 characters.", status=400)
    user["password"] = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    _save_users(users)
    return _login_page(note="Password changed. Sign in with the new password.")


# ---------------------------------------------------------------- ./company.sh user ...

def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Password (at least 10 characters): ")
        if len(pw) >= 10 and pw == getpass.getpass("Again: "):
            return pw
        print("Too short or not the same, try again.")


def cli(args: list[str]) -> None:
    users = _load_users()
    cos = companies()
    all_ids = sorted(p.parent.name for p in (ROOT / "companies").glob("*/company.env"))

    def ids(text: str) -> list[str]:
        out = [c.strip() for c in text.split(",") if c.strip()]
        if bad := [c for c in out if c not in all_ids]:
            raise SystemExit(f"Unknown company: {', '.join(bad)} (known: {', '.join(all_ids)})")
        return out

    match args:
        case ["list"]:
            print(f"{'EMAIL':34} {'ROLE':6} {'NAME':24} COMPANIES")
            for e, u in sorted(users.items()):
                print(f"{e:34} {u['role']:6} {u.get('name', ''):24} {'all' if u['role'] == 'admin' else ','.join(u.get('companies', [])) or '-'}")
        case ["add", email, name, companies_csv, *rest]:
            email = email.strip().lower()
            if email in users:
                raise SystemExit(f"{email} already exists (use: ./company.sh user companies / passwd)")
            users[email] = {"name": name, "role": "admin" if "--admin" in rest else "user", "companies": ids(companies_csv),
                            "password": bcrypt.hashpw(_ask_password().encode(), bcrypt.gensalt()).decode()}
            _save_users(users)
            print(f"Added {email}")
        case ["passwd", email]:
            u = users.get(email.lower()) or sys.exit(f"No account {email}")
            u["password"] = bcrypt.hashpw(_ask_password().encode(), bcrypt.gensalt()).decode()
            _save_users(users)
            print(f"Password changed for {email} (their open sessions end)")
        case ["companies", email, companies_csv]:
            u = users.get(email.lower()) or sys.exit(f"No account {email}")
            u["companies"] = ids(companies_csv)
            _save_users(users)
            print(f"{email} may now use: {', '.join(u['companies']) or 'nothing'}")
        case ["role", email, ("admin" | "user") as role]:
            u = users.get(email.lower()) or sys.exit(f"No account {email}")
            u["role"] = role
            _save_users(users)
            print(f"{email} is now {role}")
        case ["remove", email]:
            users.pop(email.lower(), None) or sys.exit(f"No account {email}")
            _save_users(users)
            print(f"Removed {email} (their sessions end now; their chats stay in each company's site)")
        case _:
            print("""./company.sh user list
./company.sh user add <email> "<name>" <company,company> [--admin]   (asks for the password)
./company.sh user passwd <email>
./company.sh user companies <email> <company,company>
./company.sh user role <email> admin|user
./company.sh user remove <email>
Companies: """ + ", ".join(f"{c} ({cos.get(c, 'not started')})" for c in all_ids))


if __name__ == "__main__":
    cli(sys.argv[1:])
