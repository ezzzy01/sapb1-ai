"""Internet access for the assistant: web/news search, reading a public page, and exchange rates.

Search goes through the self-hosted SearXNG on this desktop (no API keys, no tracking accounts).
Page reading is restricted to public internet addresses so web content can never be used to reach
internal machines (the B1 laptop, Ollama, SQL Server). Everything returned here is untrusted text.
"""

import ipaddress
import os
import re
import socket
from html import unescape
from urllib.parse import urlparse

import httpx

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
FX_URL = "https://api.frankfurter.dev/v1/latest"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) sapb1-ai/1.0"}
MAX_PAGE_CHARS = 6000


def web_search(query: str, news: bool = False, recent_days: int = 0, limit: int = 8) -> dict:
    params = {"q": query, "format": "json", "categories": "news" if news else "general"}
    if recent_days:
        params["time_range"] = "day" if recent_days <= 1 else "week" if recent_days <= 7 else "month" if recent_days <= 31 else "year"
    r = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
    r.raise_for_status()
    results = []
    for x in r.json().get("results", [])[: max(1, min(int(limit), 15))]:
        results.append({"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:300],
                        "published": (x.get("publishedDate") or "")[:10]})
    return {"query": query, "source": "internet (untrusted)", "results": results}


def _is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10"):  # also blocks Tailscale addresses
            return False
    return True


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", html)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return ((unescape(title.group(1)).strip() + "\n\n") if title else "") + text


def read_web_page(url: str) -> dict:
    current = url
    for _ in range(5):  # follow redirects manually so every hop is checked
        u = urlparse(current)
        if u.scheme not in ("http", "https") or not u.hostname or not _is_public(u.hostname):
            return {"error": "Only public http(s) web pages can be read."}
        r = httpx.get(current, headers=UA, timeout=25, follow_redirects=False)
        if r.is_redirect and "location" in r.headers:
            current = str(httpx.URL(current).join(r.headers["location"]))
            continue
        break
    else:
        return {"error": "Too many redirects."}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}"}
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return {"error": f"Not a text page ({ctype})."}
    text = _html_to_text(r.text) if "html" in ctype else r.text
    return {"url": current, "source": "internet (untrusted)", "text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS}


def fx_rates(base: str = "USD", symbols: str = "") -> dict:
    params = {"base": base.upper()}
    if symbols:
        params["symbols"] = ",".join(s.strip().upper() for s in symbols.split(","))
    r = httpx.get(FX_URL, params=params, timeout=20)
    r.raise_for_status()
    d = r.json()
    return {"source": "European Central Bank reference rates via frankfurter.dev", "date": d.get("date"),
            "base": d.get("base"), "rates": d.get("rates")}
