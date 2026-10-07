"""
A minimal PDF writer for quotes (BRD v2.0 BR-SF-15): text in Helvetica /
Helvetica-Bold, lines and filled boxes on A4 pages. No external dependency.

Text is encoded as WinAnsi (Latin-1); characters outside it print as "?".
Coordinates are in points from the bottom-left corner.
"""
from typing import List, Tuple

A4 = (595.28, 841.89)

# Average Helvetica glyph widths (per 1000 units) for rough text measuring / right alignment
_NARROW = set("iIl.,:;'|!")
_WIDE = set("MWmw@%")


def text_width(text: str, size: float, bold: bool = False) -> float:
    units = 0
    for ch in text:
        if ch in _NARROW:
            units += 278
        elif ch in _WIDE:
            units += 833
        elif ch == " ":
            units += 278
        elif ch.isupper() or ch.isdigit():
            units += 667 if ch.isupper() else 556
        else:
            units += 520
    return units * size / 1000 * (1.05 if bold else 1.0)


def _escape(text: str) -> str:
    raw = text.encode("latin-1", "replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", "").replace("\n", " ")


class Page:
    def __init__(self):
        self.ops: List[str] = []

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False,
             color: Tuple[float, float, float] = (0.1, 0.12, 0.16), align: str = "left"):
        if align == "right":
            x -= text_width(text, size, bold)
        elif align == "center":
            x -= text_width(text, size, bold) / 2
        r, g, b = color
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size:.1f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
                        f"{x:.2f} {y:.2f} Td ({_escape(text)}) Tj ET")

    def line(self, x1, y1, x2, y2, width=0.6, color=(0.85, 0.87, 0.9)):
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def rect(self, x, y, w, h, fill=(0.95, 0.96, 0.98)):
        r, g, b = fill
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def wrap(self, x: float, y: float, text: str, width: float, size: float = 10, leading: float = 14,
             bold: bool = False, color=(0.25, 0.28, 0.33)) -> float:
        """Write text wrapped to a width; returns the y below the last line."""
        for paragraph in (text or "").split("\n"):
            line = ""
            for word in paragraph.split(" "):
                trial = f"{line} {word}".strip()
                if text_width(trial, size, bold) > width and line:
                    self.text(x, y, line, size, bold, color)
                    y -= leading
                    line = word
                else:
                    line = trial
            self.text(x, y, line, size, bold, color)
            y -= leading
        return y


class Document:
    def __init__(self, size=A4, title: str = "Document"):
        self.size = size
        self.title = title
        self.pages: List[Page] = []

    def add_page(self) -> Page:
        page = Page()
        self.pages.append(page)
        return page

    def render(self) -> bytes:
        objects: List[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")          # 1, filled in below
        pages_obj = add(b"")        # 2
        font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        w, h = self.size
        for page in self.pages:
            stream = "\n".join(page.ops).encode("latin-1", "replace")
            content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            kids.append(add((f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] "
                             f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> "
                             f"/Contents {content} 0 R >>").encode()))
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
        objects[pages_obj - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                                  f"/Count {len(kids)} >>").encode()
        info = add(f"<< /Title ({_escape(self.title)}) /Producer (Vector) >>".encode("latin-1", "replace"))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
