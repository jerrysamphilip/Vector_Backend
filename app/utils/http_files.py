"""
HTTP download helpers (W-12).

content_disposition() builds an RFC 6266 Content-Disposition value that works for any file
name: an ASCII `filename="..."` fallback for old clients plus `filename*=UTF-8''...`
(RFC 8187 / 5987) carrying the real name. Hand-built headers with a non-Latin name used to
fail with a 500 (Starlette encodes header values as latin-1) or reach the browser mangled.
"""

import unicodedata
from urllib.parse import quote

# RFC 8187 attr-char besides ALPHA / DIGIT (which quote() never escapes)
_ATTR_CHAR_SAFE = "!#$&+-.^_`|~"
MAX_FILENAME_CHARS = 200
_DEFAULT_NAME = "download"


def _clean(filename: str) -> str:
    """No control characters (CR/LF would split the header) and no path components."""
    name = "".join(ch for ch in (filename or "") if unicodedata.category(ch)[0] != "C")
    name = name.replace("/", "_").replace("\\", "_").strip().strip(".")
    if len(name) > MAX_FILENAME_CHARS:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and 0 < len(ext) <= 16:
            name = stem[:MAX_FILENAME_CHARS - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:MAX_FILENAME_CHARS].rstrip()
    return name or _DEFAULT_NAME


def ascii_fallback(filename: str) -> str:
    """
    The name in printable ASCII: accents dropped (Café -> Cafe), other non-ASCII letters
    replaced with "_" (one per run), and "%" replaced so no client percent-decodes it.
    """
    out: list = []
    for ch in _clean(filename):
        if " " <= ch <= "~" and ch != "%":
            out.append(ch)
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if " " <= c <= "~" and c != "%")
        if base:
            out.append(base)
        elif not out or out[-1] != "_":
            out.append("_")
    name = "".join(out).strip()
    stem = name.rpartition(".")[0] if "." in name else name
    if not stem.strip("_ "):  # nothing readable left, e.g. "日本語.pdf" -> "download.pdf"
        name = _DEFAULT_NAME + ("." + name.rpartition(".")[2] if "." in name else "")
    return name


def content_disposition(filename: str, inline: bool = False) -> str:
    """`attachment; filename="<ascii>"; filename*=UTF-8''<percent-encoded UTF-8>` (or inline)."""
    name = _clean(filename)
    fallback = ascii_fallback(name).replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(name, safe=_ATTR_CHAR_SAFE, encoding="utf-8")
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
