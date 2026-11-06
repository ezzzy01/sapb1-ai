"""Reading files attached on the chat page (/sapb1/chat): Excel, CSV, PDF, Word and text files become plain text for
the AI. Only that text is kept (in the chat's SQLite file, chats.py); the file itself is not saved.

A file's text is the user's data, never instructions: agent.py hands it to the AI as its own message, marked as such.
"""

import csv
import datetime as dt
import html
import io
import re
import zipfile
from pathlib import PurePath

MAX_BYTES = 10 * 1024 * 1024      # one file
MAX_UNZIPPED = 80 * 1024 * 1024   # .xlsx/.docx are zip files: refuse "zip bombs"
MAX_CHARS = 40_000                # text kept per file
KINDS = {".xlsx": "Excel", ".xlsm": "Excel", ".csv": "CSV", ".tsv": "CSV", ".txt": "Text", ".md": "Text",
         ".pdf": "PDF", ".docx": "Word"}
ACCEPT = ",".join(KINDS)


class FileError(ValueError):
    """A message for the person: why this file can't be read."""


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00")
    if isinstance(v, dt.date):
        return v.isoformat()
    return " ".join(str(v).split())


class _Text:
    """Collects text up to MAX_CHARS and notes whether some was left out."""
    def __init__(self):
        self.parts, self.size, self.cut = [], 0, False

    def add(self, line: str) -> bool:
        if self.size + len(line) + 1 > MAX_CHARS:
            self.cut = True
            return False
        self.parts.append(line)
        self.size += len(line) + 1
        return True

    def text(self) -> str:
        return "\n".join(self.parts).strip()


def _check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FileError("This file is damaged or not a real Office file.")
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise FileError("This file is too big inside to read.")
    return z


def _excel(data: bytes, out: _Text):
    import openpyxl
    _check_zip(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise FileError("This Excel file could not be opened. Try saving it again as .xlsx.")
    try:
        for ws in wb.worksheets:
            if not out.add(f"## Sheet: {ws.title}"):
                return
            for row in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if cells and not out.add("\t".join(cells)):
                    return
    finally:
        wb.close()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def _csv(data: bytes, out: _Text, tab: bool):
    text = _decode(data)
    try:
        dialect = csv.excel_tab if tab else csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    for row in csv.reader(io.StringIO(text), dialect):
        if any(c.strip() for c in row) and not out.add("\t".join(" ".join(c.split()) for c in row)):
            return


def _pdf(data: bytes, out: _Text):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise FileError("This PDF is password protected. Save a copy without the password and attach that.")
        for n, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t and not out.add(f"--- page {n} ---\n{t}"):
                return
    except FileError:
        raise
    except Exception:
        raise FileError("This PDF could not be read.")


def _docx(data: bytes, out: _Text):
    z = _check_zip(data)
    try:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    except KeyError:
        raise FileError("This Word file could not be read. Try saving it again as .docx.")
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"</w:tc>", "\t", xml)
    xml = re.sub(r"</w:p>|<w:br/>", "\n", xml)
    for line in html.unescape(re.sub(r"<[^>]+>", "", xml)).splitlines():
        if line.strip() and not out.add(line.rstrip()):
            return


def read(name: str, data: bytes) -> dict:
    """{"name", "kind", "text", "cut": some text was left out} or FileError."""
    name = PurePath(str(name or "file").replace("\\", "/")).name[:120] or "file"
    ext = PurePath(name).suffix.lower()
    if ext == ".xls":
        raise FileError("Old .xls files can't be read. Open it in Excel and save it as .xlsx, then attach that.")
    if ext not in KINDS:
        raise FileError("This kind of file can't be read. Attach Excel (.xlsx), CSV, PDF, Word (.docx) or text files.")
    if len(data) > MAX_BYTES:
        raise FileError("This file is bigger than 10 MB.")
    if not data:
        raise FileError("This file is empty.")
    out = _Text()
    if KINDS[ext] == "Excel":
        _excel(data, out)
    elif KINDS[ext] == "CSV":
        _csv(data, out, ext == ".tsv")
    elif ext == ".pdf":
        _pdf(data, out)
    elif ext == ".docx":
        _docx(data, out)
    else:
        for line in _decode(data).splitlines():
            if not out.add(line.rstrip()):
                break
    text = out.text()
    if not text:
        raise FileError("No text was found in this file." + (" It may be a scanned picture." if ext == ".pdf" else ""))
    return {"name": name, "kind": KINDS[ext], "text": text, "cut": out.cut}
