"""Minimal, dependency-free PDF report writer for the Land Record
Verification Report.

Produces a real single-file PDF 1.4 document with vector text (standard
Helvetica base fonts, WinAnsi encoding) and the verification QR embedded as a
Flate-compressed RGB image XObject. Only Pillow (already a project dependency)
is used, to decode the QR PNG into raw pixels. The PDF is verified in tests by
parsing it with pypdfium2.

Non-Latin-1 characters (e.g. Devanagari, ₹) are transliterated/sanitised so
the standard font can render them; nothing sensitive is lost from the JSON
report, which carries the exact values.
"""
from __future__ import annotations

import re
import time
import unicodedata
import zlib
from typing import Any, Dict, List, Optional, Tuple


def _pdf_safe(text: Any, limit: int = 96) -> str:
    """Sanitise a value for WinAnsi text drawing."""
    raw = unicodedata.normalize("NFKD", str(text if text is not None else "—"))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = (raw.replace("₹", "Rs. ").replace("—", "-").replace("–", "-")
              .replace("•", "-").replace("·", "-").replace("→", "->"))
    out = []
    for ch in raw:
        code = ord(ch)
        if ch in "\\()":
            out.append("\\" + ch)
        elif 32 <= code <= 126 or 160 <= code <= 255:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)[:limit]


class _SimplePDF:
    """Tiny paginating PDF builder (Helvetica, A4)."""

    WIDTH, HEIGHT = 595, 842
    MARGIN = 48

    def __init__(self) -> None:
        self._pages: List[List[str]] = []
        self._current: List[str] = []
        self.y = self.HEIGHT - self.MARGIN

    # -- pages -------------------------------------------------------------
    def _flush_page(self) -> None:
        self._pages.append(self._current)
        self._current = []

    def ensure_space(self, needed: float) -> None:
        if self.y - needed < self.MARGIN + 24:
            self._flush_page()
            self.y = self.HEIGHT - self.MARGIN

    def finish(self) -> List[List[str]]:
        if self._current or not self._pages:
            self._flush_page()
        return self._pages

    # -- drawing -----------------------------------------------------------
    def _esc(self, text: str) -> str:
        # _pdf_safe already escaped ( ) \ for the literal string; do not
        # double-escape here or the backslashes become visible glyphs.
        return text

    def text(self, x: float, size: float, value: str, bold: bool = False,
             gray: float = 0, font_override: Optional[str] = None) -> None:
        font = font_override or ("F2" if bold else "F1")
        self._current.append(
            f"BT {gray:g} g /{font} {size:g} Tf 1 0 0 1 {x:.2f} {self.y:.2f} Tm ({self._esc(value)}) Tj ET"
        )

    def line(self, x0: float, y0: float, x1: float, y1: float, gray: float = 0.75, width: float = 0.7) -> None:
        self._current.append(
            f"{gray:g} G {width:g} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S"
        )

    def rect_fill(self, x: float, y: float, w: float, h: float, gray: float) -> None:
        self._current.append(f"{gray:g} g {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def draw_image(self, name: str, x: float, y: float, w: float, h: float) -> None:
        self._current.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")


def _build_content(pages: List[List[str]]) -> str:
    streams = []
    for ops in pages:
        streams.append("\n".join(ops))
    return "\n".join(f"{idx + 1} 0 obj\n<< /Length {len(chunk.encode('latin-1', 'replace'))} >>\nstream\n{chunk}\nendstream\nendobj"
                     for idx, chunk in enumerate(streams))


def render_verification_report_pdf(report: Dict[str, Any], qr_png: Optional[bytes]) -> bytes:
    """Render the verification report payload as a PDF document."""
    pdf = _SimplePDF()
    objects: List[bytes] = []  # object streams for image XObject added later

    # ---- QR image XObject -------------------------------------------------
    image_obj = None
    if qr_png:
        try:
            import io as _io

            from PIL import Image as PILImage

            image = PILImage.open(_io.BytesIO(qr_png)).convert("RGB")
            raw = image.tobytes()
            compressed = zlib.compress(raw, 6)
            image_obj = (
                f"<< /Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                f"/Length {len(compressed)} >>".encode("latin-1")
                + b"\nstream\n" + compressed + b"\nendstream"
            )
        except Exception:
            image_obj = None

    # ---- page 1 header -----------------------------------------------------
    left = _SimplePDF.MARGIN
    pdf.text(left, 9, "DOCUMENT SCREENING - LAND INTELLIGENCE", bold=True, gray=0.45)
    pdf.y -= 18
    pdf.text(left, 18, "LAND RECORD VERIFICATION REPORT", bold=True, gray=0.1)
    pdf.y -= 16
    reference = _pdf_safe(report.get("reference_no"))
    pdf.text(left, 10, f"Verification reference: {reference}", bold=True)
    qr_size = 84
    if qr_png and image_obj is not None:
        # QR sits right-aligned inside the reserved header band and encodes
        # this report's verification reference only.
        pdf.draw_image("Im0", _SimplePDF.WIDTH - _SimplePDF.MARGIN - qr_size,
                       _SimplePDF.HEIGHT - 150, qr_size, qr_size)
    pdf.y -= 6
    pdf.line(left, pdf.y, _SimplePDF.WIDTH - _SimplePDF.MARGIN, pdf.y)
    if qr_png and image_obj is not None:
        pdf.y = min(pdf.y, _SimplePDF.HEIGHT - 150 - 8)  # stay clear of the QR band
    pdf.y -= 16

    def kv_rows(rows: List[Tuple[str, Any]]) -> None:
        nonlocal pdf
        for label, value in rows:
            pdf.ensure_space(15)
            pdf.rect_fill(left, pdf.y - 3.5, _SimplePDF.WIDTH - 2 * left, 14.5, 0.955)
            pdf.text(left + 3, 8.5, _pdf_safe(label, 40), bold=True, gray=0.25)
            pdf.text(left + 190, 8.5, _pdf_safe(value))
            pdf.y -= 15

    kv_rows([
        ("Document ID", report.get("document_id") or "-"),
        ("Land record ID", report.get("land_record_id")),
        ("Owner", report.get("owner") or "-"),
        ("Father / guardian", report.get("father") or "-"),
        ("Survey / Khasra", " / ".join(str(part) for part in (report.get("survey"), report.get("khasra")) if part)),
        ("Area", report.get("area") or "-"),
        ("Village", report.get("village") or "-"),
        ("Tehsil", report.get("tehsil") or "-"),
        ("District", report.get("district") or "-"),
        ("Verification status", report.get("verification_status") or "-"),
        ("Risk status", report.get("risk_status") or "-"),
        ("Encumbrance status", report.get("encumbrance_status") or "-"),
        ("Mutation status", report.get("mutation_status") or "-"),
        ("Generated (UTC)", time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(float(report.get("generated_at") or time.time())))),
        ("Reviewer", report.get("reviewer") or "-"),
        ("Audit / reference ID", reference),
    ])

    pdf.ensure_space(24)
    pdf.y -= 8
    pdf.text(left, 12, "RISK SIGNALS (deterministic review signals - not a legal determination)", bold=True, gray=0.2)
    pdf.y -= 14
    flags: List[Dict[str, Any]] = list(report.get("risk_flags") or [])
    if not flags:
        pdf.text(left + 6, 9, "No risk signals recorded.", gray=0.35)
        pdf.y -= 13
    for flag in flags[:14]:
        pdf.ensure_space(13)
        marker = flag.get("severity")
        pdf.text(left + 6, 8.5, f"[{_pdf_safe(marker, 8)}]", bold=True, gray=0.3)
        pdf.text(left + 56, 8.5, _pdf_safe(f"{flag.get('title')} ({flag.get('code')})"))
        pdf.y -= 13
    if len(flags) > 14:
        pdf.text(left + 6, 8, f"... and {len(flags) - 14} more signals (see the HTML/JSON report).", gray=0.4)
        pdf.y -= 12

    pdf.ensure_space(24)
    pdf.y -= 8
    pdf.text(left, 12, "SUPPORTING DOCUMENTS", bold=True, gray=0.2)
    pdf.y -= 14
    documents: List[Dict[str, Any]] = list(report.get("supporting_documents") or [])
    if not documents:
        pdf.text(left + 6, 9, "No documents linked.", gray=0.35)
        pdf.y -= 13
    for doc in documents[:16]:
        pdf.ensure_space(13)
        pdf.text(left + 6, 8.5, "- " + _pdf_safe(f"{doc.get('id')}  {doc.get('filename')}  ({doc.get('status')})"))
        pdf.y -= 13
    if len(documents) > 16:
        pdf.text(left + 6, 8, f"... and {len(documents) - 16} more documents.", gray=0.4)
        pdf.y -= 12

    pdf.ensure_space(40)
    pdf.y -= 10
    pdf.line(left, pdf.y, _SimplePDF.WIDTH - _SimplePDF.MARGIN, pdf.y, gray=0.6)
    pdf.y -= 12
    disclaimer = _pdf_safe(report.get("disclaimer") or
                           "Internal verification workflow report. NOT an official government land title certificate.",
                           limit=600)
    # wrap the disclaimer at ~95 chars
    wrapped = [disclaimer[i:i + 118] for i in range(0, len(disclaimer), 118)]
    for chunk in wrapped[:4]:
        pdf.text(left, 8, chunk, gray=0.35)
        pdf.y -= 11
    pdf.y -= 2
    qr_note = "The QR code encodes this report's verification reference only."
    pdf.text(left, 7.5, _pdf_safe(qr_note), gray=0.45)

    pages = pdf.finish()

    # ---- assemble PDF objects (explicit numbering, appended in order) ------
    content_streams = ["\n".join(ops) for ops in pages]
    page_count = len(pages)

    catalog_no = 1
    pages_tree_no = 2
    font_regular_no = 3
    font_bold_no = 4
    next_no = 5
    image_obj_no = None
    if image_obj is not None:
        image_obj_no = next_no
        next_no += 1
    page_nos = [next_no + idx for idx in range(page_count)]
    next_no += page_count
    content_nos = [next_no + idx for idx in range(page_count)]
    next_no += page_count

    object_chunks: List[bytes] = []

    def add_object(body: bytes) -> None:
        object_chunks.append(body)

    add_object(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{number} 0 R" for number in page_nos)
    add_object(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1"))
    add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    if image_obj is not None:
        add_object(image_obj)
    image_ref = f"/XObject << /Im0 {image_obj_no} 0 R >>" if image_obj_no else ""
    for idx, stream in enumerate(content_streams):
        resources = f"<< /Font << /F1 {font_regular_no} 0 R /F2 {font_bold_no} 0 R >> {image_ref} >>"
        add_object(
            (f"<< /Type /Page /Parent {pages_tree_no} 0 R /MediaBox [0 0 {_SimplePDF.WIDTH} {_SimplePDF.HEIGHT}] "
             f"/Resources {resources} /Contents {content_nos[idx]} 0 R >>").encode("latin-1")
        )
    for stream in content_streams:
        encoded = stream.encode("latin-1", "replace")
        add_object(f"<< /Length {len(encoded)} >>\nstream\n".encode("latin-1") + encoded + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(object_chunks) + 1)
    for number, body in enumerate(object_chunks, start=1):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(object_chunks) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, len(object_chunks) + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(object_chunks) + 1} /Root {catalog_no} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF").encode("latin-1")
    return bytes(out)
