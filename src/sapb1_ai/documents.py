"""Answers from the company's own documents (Joule-style "document grounding"), entirely on this PC.

Put files in companies/<id>/documents/ (sub-folders are fine): PDF, Word (.docx), Excel (.xlsx), CSV, text or Markdown.
Each company's chat service reads only its own folder, so one company never sees another's documents, and nothing is
uploaded anywhere. The folder is git-ignored.

Search: the documents are cut into short passages (with file name and page / sheet), and passages are ranked by
TF-IDF on words and on character pieces (so "invois" still finds "invoices"). The index is rebuilt automatically
when a file is added, changed or removed.
"""

import re
import subprocess
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .company import CONFIG_DIR

DOC_DIR = CONFIG_DIR / "documents"
TYPES = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md"}
CHUNK = 900          # characters per passage (about a paragraph or two)
MIN_SCORE = 0.08     # below this the documents don't really answer the question
MAX_FILE_MB = 50

# Words that say nothing about the topic: a passage must share at least one OTHER word with the question.
STOP = set("""a an the and or of to in on at for from with by about is are was be do does did can could should would will
what which who whom whose when where why how much many any some our we us you your me my i it its this that these those
please tell show give find there their them they policy policies procedure procedures rule rules company document documents
guideline guidelines say says said need needs required apa siapa bila mana kenapa mengapa bagaimana berapa yang dan atau
untuk dari dengan di ke pada ini itu kita kami anda saya ada tidak polisi prosedur peraturan syarikat dokumen boleh perlu
adakah macam nak""".split())

_lock = threading.Lock()
_index: dict = {"key": None}


# ---------- reading files ----------

def _pdf(path: Path) -> list[tuple[str, str]]:
    out = subprocess.run(["pdftotext", "-layout", "-q", str(path), "-"], capture_output=True, text=True, timeout=120).stdout
    return [(f"page {i}", t) for i, t in enumerate(out.split("\f"), 1) if t.strip()]


def _docx(path: Path) -> list[tuple[str, str]]:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in p.iter(f"{ns}t"))
        style = p.find(f"{ns}pPr/{ns}pStyle")
        if text.strip():
            heading = style is not None and "heading" in (style.get(f"{ns}val") or "").lower()
            paras.append(("## " if heading else "") + text)
    for tbl in root.iter(f"{ns}tbl"):  # tables: one line per row
        for row in tbl.iter(f"{ns}tr"):
            cells = ["".join(t.text or "" for t in c.iter(f"{ns}t")).strip() for c in row.iter(f"{ns}tc")]
            if any(cells):
                paras.append(" | ".join(cells))
    return [("", "\n\n".join(paras))]


def _xlsx(path: Path) -> list[tuple[str, str]]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        rows, header = [], None
        for r in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in r]
            if not any(c.strip() for c in cells):
                continue
            if header is None:
                header = cells
                rows.append(" | ".join(cells))
            else:  # "Item: X | Price: Y" so every row can be understood on its own
                rows.append(" | ".join(f"{h}: {c}" if h else c for h, c in zip(header, cells) if c))
        if rows:
            parts.append((f"sheet {ws.title}", "\n".join(rows)))
    wb.close()
    return parts


def _text(path: Path) -> list[tuple[str, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return [("", raw.decode(enc))]
        except UnicodeDecodeError:
            continue
    return []


READERS = {".pdf": _pdf, ".docx": _docx, ".xlsx": _xlsx, ".csv": _text, ".txt": _text, ".md": _text}


def _passages(where: str, text: str) -> list[tuple[str, str]]:
    """Cut text into passages of about CHUNK characters at paragraph (or line) breaks; keep the latest heading."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) <= 1:  # tables and CSVs: split by lines
        blocks = [b for b in text.splitlines() if b.strip()]
    out, buf, heading = [], "", ""
    for b in blocks:
        if re.match(r"^#{1,4} |^[A-Z0-9][A-Z0-9 .&/-]{3,60}$", b) and len(b) < 80:
            heading = b.lstrip("# ").strip()
        if buf and len(buf) + len(b) > CHUNK:
            out.append((where, buf))
            buf = (heading + "\n" if heading and not buf.startswith(heading) else "")
        while len(b) > CHUNK * 1.5:  # one very long paragraph
            out.append((where, (buf + b[:CHUNK]).strip()))
            b, buf = b[CHUNK:], ""
        buf += b + "\n\n"
    if buf.strip():
        out.append((where, buf.strip()))
    return out


# ---------- index ----------

def _files() -> list[Path]:
    if not DOC_DIR.exists():
        return []
    return sorted(p for p in DOC_DIR.rglob("*") if p.is_file() and p.suffix.lower() in TYPES and not p.name.startswith((".", "~$")))


def _build() -> dict:
    files = _files()
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _index.get("key") == key:
        return _index
    from sklearn.feature_extraction.text import TfidfVectorizer
    from scipy.sparse import hstack
    docs, problems = [], []
    for p in files:
        name = str(p.relative_to(DOC_DIR))
        if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            problems.append(f"{name}: larger than {MAX_FILE_MB} MB, skipped")
            continue
        try:
            parts = READERS[p.suffix.lower()](p)
        except Exception as e:
            problems.append(f"{name}: could not be read ({type(e).__name__})")
            continue
        found = False
        for where, text in parts:
            for w, passage in _passages(where, text):
                docs.append({"file": name, "where": w, "text": passage})
                found = True
        if not found:
            problems.append(f"{name}: no text found (a scanned PDF needs text recognition first)")
    index = {"key": key, "docs": docs, "files": [str(p.relative_to(DOC_DIR)) for p in files], "problems": problems}
    if docs:
        texts = [f"{d['file']} {d['text']}" for d in docs]
        words = TfidfVectorizer(lowercase=True, sublinear_tf=True, ngram_range=(1, 2), min_df=1, token_pattern=r"(?u)\b\w+\b")
        chars = TfidfVectorizer(lowercase=True, sublinear_tf=True, analyzer="char_wb", ngram_range=(3, 5), min_df=1)
        index.update(words=words, chars=chars, matrix=hstack([words.fit_transform(texts) * 0.6, chars.fit_transform(texts) * 0.4]).tocsr())
    _index.clear()
    _index.update(index)
    return _index


def company_documents(query: str = "", top: int = 4) -> dict:
    """Passages from the company's own documents that answer the question, each with its file and page."""
    with _lock:
        idx = _build()
    if not idx["files"]:
        return {"note": "This company has no documents yet. Say so plainly: an administrator can add policies, procedures, "
                        f"price lists or terms (PDF, Word, Excel, CSV, text) to the folder {DOC_DIR} on the AI server.",
                "documents": []}
    if not (query or "").strip():
        return {"documents": idx["files"], "problems": idx["problems"]}
    if not idx["docs"]:
        return {"note": "None of the documents contain readable text.", "problems": idx["problems"]}
    from scipy.sparse import hstack
    q = hstack([idx["words"].transform([query]) * 0.6, idx["chars"].transform([query]) * 0.4]).tocsr()
    scores = (idx["matrix"] @ q.T).toarray().ravel()
    norm = (q.multiply(q).sum() ** 0.5) * (idx["matrix"].multiply(idx["matrix"]).sum(axis=1).A.ravel() ** 0.5)
    scores = scores / (norm + 1e-9)
    terms = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2 and w not in STOP]
    def on_topic(text: str) -> bool:  # shares a real word (or its first 5 letters: invois/invoices) with the question
        low = text.lower()
        return any(w in low or (len(w) > 5 and w[:5] in low) for w in terms)
    order = [i for i in scores.argsort()[::-1] if scores[i] >= MIN_SCORE and on_topic(idx["docs"][i]["file"] + " " + idx["docs"][i]["text"])]
    order = order[: max(1, min(int(top or 4), 8))]
    hits = [{"source": idx["docs"][i]["file"] + (f", {idx['docs'][i]['where']}" if idx["docs"][i]["where"] else ""),
             "score": round(float(scores[i]), 3), "text": idx["docs"][i]["text"]} for i in order]
    if not hits:
        return {"answer_found": False, "documents_searched": len(idx["files"]),
                "note": "The company's documents do not cover this. Say plainly that it is not in the company's documents; "
                        "do not answer from general knowledge as if it were company policy."}
    return {"answer_found": True, "passages": hits,
            "instruction": "Answer only from these passages, in the user's language. After each fact name its source in "
                           "brackets, e.g. [Credit policy.pdf, page 2]. If the passages only partly answer, say what is missing."}


def catalog() -> str:
    """One line for the assistant's instructions naming this company's documents (empty when there are none)."""
    names = [Path(f).stem for f in (str(p.relative_to(DOC_DIR)) for p in _files())]
    if not names:
        return ""
    shown = ", ".join(names[:40]) + (f" and {len(names) - 40} more" if len(names) > 40 else "")
    return (f"The company's own documents (search them with company_documents for any question about company rules, "
            f"policies, procedures, approvals, terms or price lists, in English or Malay): {shown}.")
