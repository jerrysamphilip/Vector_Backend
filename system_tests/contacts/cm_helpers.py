"""Extra helpers for the Contact Management suite (CSV/XLSX builders, long-running multipart calls)."""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import API, req, sql  # noqa: E402

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def q(params: dict) -> str:
    """Query string; dict/list values are JSON-encoded (filters)."""
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        out[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    return "?" + urllib.parse.urlencode(out)


def csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def parse_csv(content) -> list:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(content)))


def xlsx_bytes(header, rows) -> bytes:
    """Minimal single-sheet XLSX with inline strings (stdlib only)."""
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def row_xml(n, values):
        cells = "".join(f'<c r="{col(i)}{n}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                        for i, v in enumerate(values) if v not in (None, ""))
        return f'<row r="{n}">{cells}</row>'

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + row_xml(1, header) + "".join(row_xml(i + 2, r) for i, r in enumerate(rows))
             + '</sheetData></worksheet>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Contacts" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def run_import(token, content: bytes, mapping: dict, options: dict = None, fname="contacts.csv", timeout=900):
    """POST /imports with a long timeout (common.req caps at 120 s). Returns (status, body, seconds)."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("mapping", json.dumps(mapping)), ("options", json.dumps(options or {}))):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    data = b"".join(parts) + f"--{boundary}--\r\n".encode()
    r = urllib.request.Request(API + "/imports", method="POST", data=data, headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    t0 = time.perf_counter()
    try:
        with _opener.open(r, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    secs = time.perf_counter() - t0
    try:
        body = json.loads(body)
    except Exception:
        body = body.decode(errors="ignore")
    return status, body, secs


def timed(method, path, token, body=None):
    t0 = time.perf_counter()
    s, b = req(method, path, body, token)
    return s, b, (time.perf_counter() - t0) * 1000.0


def pct(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def count(query: str) -> int:
    out = sql(query)
    try:
        return int(out.splitlines()[-1])
    except Exception:
        return -1
