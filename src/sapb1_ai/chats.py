"""Saved conversations for our own chat page (/sapb1/chat): one small SQLite file per company on this PC,
site/chats/<company>.sqlite (private, not in git). Every read and change is limited to the signed-in person's own chats.

The server keeps each chat's messages itself; the page only sends the new question. So a browser cannot hand the AI a
made-up earlier conversation (for example a fake draft preview to "approve").
"""

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .company import COMPANY

DB_PATH = Path(__file__).resolve().parents[2] / "site" / "chats" / f"{COMPANY or 'default'}.sqlite"
TITLE_MAX = 80
LIST_MAX = 300
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    new = not DB_PATH.exists()
    if new:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(DB_PATH.parent, 0o700)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    if new:
        os.chmod(DB_PATH, 0o600)
    con.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS chat (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS chat_by_person ON chat (email, updated DESC);
        CREATE TABLE IF NOT EXISTS message (
            chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE, n INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (chat_id, n));
        -- files attached on the page: only the text read from them (files.py), never the file itself
        CREATE TABLE IF NOT EXISTS attachment (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, chat_id TEXT REFERENCES chat(id) ON DELETE CASCADE,
            name TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, cut INTEGER NOT NULL, created REAL NOT NULL);
    """)
    if "files" not in [r[1] for r in con.execute("PRAGMA table_info(message)")]:  # the files a question came with
        con.execute("ALTER TABLE message ADD COLUMN files TEXT NOT NULL DEFAULT ''")
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def _conn():
    """One connection per call: committed on success, always closed."""
    con = _db()
    try:
        with con:
            yield con
    finally:
        con.close()


def _title(question: str) -> str:
    t = " ".join(question.split())
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


def list_chats(email: str) -> list[dict]:
    with _lock, _conn() as con:
        rows = con.execute("SELECT id, title, updated FROM chat WHERE email = ? ORDER BY updated DESC LIMIT ?",
                           (email, LIST_MAX)).fetchall()
    return [dict(r) for r in rows]


def get(email: str, chat_id: str) -> dict | None:
    with _lock, _conn() as con:
        c = con.execute("SELECT id, title, updated FROM chat WHERE id = ? AND email = ?", (chat_id, email)).fetchone()
        if not c:
            return None
        msgs = con.execute("SELECT role, content, files FROM message WHERE chat_id = ? ORDER BY n", (chat_id,)).fetchall()
    out = []
    for m in msgs:
        m = dict(m)
        files = m.pop("files")
        if files:
            m["files"] = json.loads(files)  # [{"id", "name", "kind"}]
        out.append(m)
    return {**dict(c), "messages": out}


def start(email: str, question: str) -> str:
    chat_id = secrets.token_urlsafe(12)
    now = time.time()
    with _lock, _conn() as con:
        con.execute("INSERT INTO chat (id, email, title, created, updated) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, email, _title(question), now, now))
    return chat_id


def add(email: str, chat_id: str, role: str, content: str, files: list[dict] | None = None) -> bool:
    now = time.time()
    with _lock, _conn() as con:
        if not con.execute("SELECT 1 FROM chat WHERE id = ? AND email = ?", (chat_id, email)).fetchone():
            return False
        n = con.execute("SELECT COALESCE(MAX(n), -1) + 1 FROM message WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        con.execute("INSERT INTO message (chat_id, n, role, content, at, files) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, n, role, content, now, json.dumps(files) if files else ""))
        if files:  # the files now belong to this chat (and go when it is deleted)
            con.executemany("UPDATE attachment SET chat_id = ? WHERE id = ? AND email = ?",
                            [(chat_id, f["id"], email) for f in files])
        con.execute("UPDATE chat SET updated = ? WHERE id = ?", (now, chat_id))
    return True


def rename(email: str, chat_id: str, title: str) -> bool:
    title = _title(title)
    if not title:
        return False
    with _lock, _conn() as con:
        return con.execute("UPDATE chat SET title = ? WHERE id = ? AND email = ?", (title, chat_id, email)).rowcount == 1


def delete(email: str, chat_id: str) -> bool:
    with _lock, _conn() as con:
        return con.execute("DELETE FROM chat WHERE id = ? AND email = ?", (chat_id, email)).rowcount == 1


UNSENT_HOURS = 24


def attach(email: str, name: str, kind: str, text: str, cut: bool) -> str:
    """Keeps the text read from an attached file until its question is sent; files never sent go after a day."""
    file_id = secrets.token_urlsafe(12)
    now = time.time()
    with _lock, _conn() as con:
        con.execute("DELETE FROM attachment WHERE chat_id IS NULL AND created < ?", (now - UNSENT_HOURS * 3600,))
        con.execute("INSERT INTO attachment (id, email, chat_id, name, kind, text, cut, created) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                    (file_id, email, name, kind, text, int(cut), now))
    return file_id


def attachments(email: str, ids: list[str], chat_id: str | None = None) -> list[dict]:
    """This person's files by id: not sent yet, or already in this chat. Unknown ids are left out."""
    if not ids:
        return []
    with _lock, _conn() as con:
        rows = con.execute(f"SELECT id, name, kind, text, cut FROM attachment WHERE email = ? AND id IN ({','.join('?' * len(ids))})"
                           " AND (chat_id IS NULL OR chat_id = ?)", (email, *ids, chat_id)).fetchall()
    found = {r["id"]: dict(r) for r in rows}
    return [found[i] for i in ids if i in found]
