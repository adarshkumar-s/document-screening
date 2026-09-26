#!/usr/bin/env python3
"""Render the DEMO-LI synthetic sample documents (PNG images + PDFs).

Every file is rendered from the specs in ``land_demo_docs.DOCUMENT_SPECS`` so
the document text, the seeded database rows and the demo index always agree.

The documents are unmistakably fictional:
  * every page carries the banner "SYNTHETIC DEMO DOCUMENT — NOT A REAL
    GOVERNMENT RECORD" plus a large diagonal watermark of the same warning;
  * the footer repeats the warning and notes the fictional seal;
  * the seal is an invented two-ring "DEMO" stamp — no real government seal,
    emblem, QR code or signature is copied.

Usage:
    python tools/generate_demo_documents.py            # render into samples/demo-land-intel
    python tools/generate_demo_documents.py --force    # re-render even if files exist
    python tools/generate_demo_documents.py --verify   # list files and check presence
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import zlib
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from land_demo_docs import DOCUMENT_SPECS, document_plain_text  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # pragma: no cover
    print("Pillow is required: pip install pillow", file=sys.stderr)
    raise SystemExit(1)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE_DIR, "samples", "demo-land-intel")
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
PAGE_W, PAGE_H = 1240, 1754  # A4 at ~150 dpi
MARGIN = 90
WARNING = "SYNTHETIC DEMO DOCUMENT — NOT A REAL GOVERNMENT RECORD"

SERIF = os.path.join(FONT_DIR, "DejaVuSerif.ttf")
SERIF_BOLD = os.path.join(FONT_DIR, "DejaVuSerif-Bold.ttf")
SANS = os.path.join(FONT_DIR, "DejaVuSans.ttf")
SANS_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
MONO = os.path.join(FONT_DIR, "DejaVuSansMono.ttf")


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    lines: List[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        lines.append(current)
    return lines


def _draw_watermark(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(SANS_BOLD, 110)
    text = "SYNTHETIC\nDEMO\nNOT A REAL\nGOVT RECORD"
    lines = text.split("\n")
    total_h = sum(draw.textlength(line, font=font) and font.size for line in lines) + 40 * len(lines)
    y = (image.height - total_h) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((image.width - w) / 2, y), line, font=font, fill=(140, 140, 160, 38))
        y += font.size + 40
    layer = layer.rotate(28, resample=Image.Resampling.BICUBIC, center=(image.width / 2, image.height / 2))
    image.alpha_composite(layer)


def _header_footer(draw: ImageDraw.ImageDraw, spec: Dict[str, Any]) -> None:
    # banner
    draw.rectangle([0, 0, PAGE_W, 64], fill=(153, 27, 27))
    font = _font(SANS_BOLD, 24)
    w = draw.textlength(WARNING, font=font)
    draw.text(((PAGE_W - w) / 2, 18), WARNING, font=font, fill=(255, 255, 255))
    # footer
    draw.line([MARGIN, PAGE_H - 78, PAGE_W - MARGIN, PAGE_H - 78], fill=(120, 120, 130), width=2)
    f_small = _font(SANS, 17)
    draw.text((MARGIN, PAGE_H - 66), WARNING, font=f_small, fill=(153, 27, 27))
    draw.text((MARGIN, PAGE_H - 42), "Fictional demo data · no real person, parcel, case or identifier · " + spec["filename"],
              font=f_small, fill=(110, 110, 120))


def _fictional_seal(draw: ImageDraw.ImageDraw, cx: int, cy: int, caption: str) -> None:
    r = 92
    for width, color in ((5, (30, 58, 138)), (2, (30, 58, 138))):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)
        r -= 10
    font = _font(SANS_BOLD, 22)
    for text, dy in (("DEMO", -26), ("FICTIONAL", 4), ("SEAL", 34)):
        w = draw.textlength(text, font=font)
        draw.text((cx - w / 2, cy + dy), text, font=font, fill=(30, 58, 138, 220))
    draw.regular_polygon((cx, cy - 64, 10), 5, rotation=0, fill=(30, 58, 138))
    small = _font(SANS, 14)
    w = draw.textlength(caption, font=small)
    draw.text((cx - w / 2, cy + r + 10), caption, font=small, fill=(110, 110, 120))


def _signature(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    # deterministic synthetic scribble (no real signature)
    points = [(0, 0), (18, -26), (34, 8), (52, -34), (70, 6), (92, -20), (112, 2), (128, -14)]
    draw.line([(x + px, y + py) for px, py in points], fill=(25, 25, 40), width=3)
    draw.line([(x - 10, y + 16), (x + 210, y + 16)], fill=(60, 60, 70), width=2)
    draw.text((x - 10, y + 24), "/sd/-(Demo Authorised Signatory)", font=_font(SANS, 16), fill=(60, 60, 70))


def _title_block(draw: ImageDraw.ImageDraw, spec: Dict[str, Any], serif: bool) -> int:
    y = MARGIN + 34
    dept_font = _font(SERIF_BOLD if serif else SANS_BOLD, 26)
    for line in _wrap(draw, spec.get("department", ""), dept_font, PAGE_W - 2 * MARGIN):
        w = draw.textlength(line, font=dept_font)
        draw.text(((PAGE_W - w) / 2, y), line, font=dept_font, fill=(30, 41, 59))
        y += 34
    y += 8
    draw.line([MARGIN, y, PAGE_W - MARGIN, y], fill=(30, 41, 59), width=3)
    y += 18
    title_font = _font(SERIF_BOLD if serif else SANS_BOLD, 34)
    for line in _wrap(draw, spec.get("title", ""), title_font, PAGE_W - 2 * MARGIN):
        w = draw.textlength(line, font=title_font)
        draw.text(((PAGE_W - w) / 2, y), line, font=title_font, fill=(15, 23, 42))
        y += 44
    if spec.get("ref_line"):
        y += 4
        ref_font = _font(MONO, 19)
        for line in _wrap(draw, spec["ref_line"], ref_font, PAGE_W - 2 * MARGIN):
            w = draw.textlength(line, font=ref_font)
            draw.text(((PAGE_W - w) / 2, y), line, font=ref_font, fill=(120, 53, 15))
            y += 26
    y += 10
    draw.line([MARGIN, y, PAGE_W - MARGIN, y], fill=(148, 163, 184), width=2)
    return y + 26


def _field_lines(spec: Dict[str, Any]) -> List[str]:
    return [f"{label}: {value}" for label, value in spec.get("fields", {}).items()]


def _render(image: Image.Image, spec: Dict[str, Any]) -> None:  # noqa: C901
    draw = ImageDraw.Draw(image)
    kind = spec.get("kind", "memo")
    serif = kind in ("deed", "court")
    body_font_path = SERIF if serif else SANS
    body_font = _font(body_font_path, 24)
    field_font = _font(SANS_BOLD if kind in ("land_record", "form", "certificate") else MONO, 23)
    _header_footer(draw, spec)

    if kind == "deed":
        draw.rectangle([46, 84, PAGE_W - 46, PAGE_H - 96], outline=(30, 58, 138), width=3)
        draw.rectangle([56, 94, PAGE_W - 56, PAGE_H - 106], outline=(30, 58, 138), width=1)
    elif kind == "certificate":
        for offset in (46, 54):
            draw.rectangle([offset, 84 + offset - 46, PAGE_W - offset, PAGE_H - 96 - 8], outline=(180, 83, 9), width=2)

    y = _title_block(draw, spec, serif)
    max_w = PAGE_W - 2 * MARGIN

    # key/value block
    for line in _field_lines(spec):
        label, _, value = line.partition(": ")
        if kind in ("land_record", "form", "certificate"):
            draw.rectangle([MARGIN, y - 4, MARGIN + 380, y + 30], fill=(241, 245, 249), outline=(100, 116, 139))
            draw.text((MARGIN + 12, y + 2), label, font=field_font, fill=(30, 41, 59))
            draw.rectangle([MARGIN + 380, y - 4, PAGE_W - MARGIN, y + 30], outline=(100, 116, 139))
            draw.text((MARGIN + 396, y + 2), value, font=_font(SANS_BOLD, 23), fill=(15, 23, 42))
            y += 40
        else:
            draw.text((MARGIN, y), label + ":", font=field_font, fill=(71, 85, 105))
            draw.text((MARGIN + 400, y), value, font=_font(SANS_BOLD, 24), fill=(15, 23, 42))
            y += 34
    y += 12

    for paragraph in spec.get("body", []):
        if paragraph.startswith("  ("):
            draw.text((MARGIN + 16, y), paragraph.strip(), font=_font(body_font_path, 22), fill=(30, 41, 59))
            y += 32
            continue
        for line in _wrap(draw, paragraph, body_font, max_w):
            draw.text((MARGIN, y), line, font=body_font, fill=(28, 32, 40))
            y += 33
        y += 10

    y = max(y + 24, PAGE_H - 430)
    if kind == "court":
        for line in ("Given under the hand and seal of the (fictional) demo court", "on this demo record."):
            w = draw.textlength(line, font=body_font)
            draw.text(((PAGE_W - w) / 2, y), line, font=body_font, fill=(30, 41, 59))
            y += 34
        _fictional_seal(draw, PAGE_W // 2, y + 120, "Not a real court · not a real seal")
    else:
        _fictional_seal(draw, PAGE_W - MARGIN - 100, y + 110, "Fictional demo seal — no authority implied")
        _signature(draw, MARGIN, y + 60)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf_with_text_layer(page_image, text: str, dpi: int = 150) -> bytes:
    """Build a PDF that keeps the synthetic rendered page as a full-bleed
    background image *and* carries a real, extractable text layer.

    A plain rasterized PDF (what Pillow produces) has no text layer, so
    ``pypdfium2``/``pypdf`` extraction returns nothing and the Land
    Intelligence document bridge cannot read the case number, survey number or
    village. Here the visual is embedded as a DCT (JPEG) image exactly as
    before, and an *invisible* text layer (PDF render mode 3) reproduces the
    document's plain text on top. The appearance is unchanged and unmistakably
    synthetic; only the machine-readable text is added — no real seals,
    identifiers or QR codes are introduced.
    """
    width, height = page_image.size
    page_w_pt = round(width / dpi * 72, 2)
    page_h_pt = round(height / dpi * 72, 2)

    image_buffer = io.BytesIO()
    page_image.save(image_buffer, "JPEG", quality=82, optimize=True)
    image_bytes = image_buffer.getvalue()

    text_lines = [line for line in text.split("\n")]
    line_count = max(len(text_lines), 1)
    top = page_h_pt - 15.0
    bottom = 15.0
    leading = max(6.0, min(14.0, (top - bottom) / line_count))

    parts = [
        "q",
        f"{page_w_pt} 0 0 {page_h_pt} 0 0 cm",
        "/Im0 Do",
        "Q",
        "BT",
        "3 Tr",                 # invisible text: keeps the visual identical
        "/F1 11 Tf",
        f"{leading:.2f} TL",
        f"40 {top:.2f} Td",
    ]
    for line in text_lines:
        safe = _pdf_escape(line.encode("cp1252", "replace").decode("cp1252"))
        parts.append(f"({safe}) Tj")
        parts.append("T*")
    parts.append("ET")
    content = "\n".join(parts).encode("cp1252", "replace")
    content_bytes = zlib.compress(content, 9)

    image_obj = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
        f"/Length {len(image_bytes)} >>\nstream\n".encode()
        + image_bytes
        + b"\nendstream"
    )
    content_obj = (
        f"<< /Length {len(content_bytes)} /Filter /FlateDecode >>\nstream\n".encode()
        + content_bytes
        + b"\nendstream"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w_pt} {page_h_pt}] "
            f"/Resources << /XObject << /Im0 5 0 R >> /Font << /F1 4 0 R >> >> "
            f"/Contents 6 0 R >>"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        image_obj,
        content_obj,
    ]

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    count = len(objects) + 1
    out.write(f"xref\n0 {count}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out.getvalue()


def render_spec(spec: Dict[str, Any], out_dir: str, force: bool = False) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, spec["filename"])
    if os.path.isfile(path) and not force:
        return path
    image = Image.new("RGBA", (PAGE_W, PAGE_H), (250, 250, 247, 255))
    # faint aged-paper tint bands for OCR variety
    for band_y, alpha in ((300, 6), (900, 5), (1400, 7)):
        overlay = Image.new("RGBA", (PAGE_W, 220), (203, 199, 188, alpha))
        image.alpha_composite(overlay, (0, band_y))
    _draw_watermark(image)
    page = image.convert("RGB")
    _render(page, spec)
    if spec["filename"].lower().endswith(".pdf"):
        # Keep the synthetic visual as the page background but add a real,
        # extractable text layer so the Land Intelligence bridge can read the
        # document identity (case no / survey / village) without raster OCR.
        pdf_bytes = build_pdf_with_text_layer(page, document_plain_text(spec), dpi=150)
        with open(path, "wb") as handle:
            handle.write(pdf_bytes)
    else:
        page.save(path, "PNG", optimize=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="re-render existing files")
    parser.add_argument("--verify", action="store_true", help="only verify presence")
    args = parser.parse_args()

    if args.verify:
        missing = [spec["filename"] for spec in DOCUMENT_SPECS
                   if not os.path.isfile(os.path.join(args.out, spec["filename"]))]
        print(f"{len(DOCUMENT_SPECS) - len(missing)}/{len(DOCUMENT_SPECS)} files present in {args.out}")
        for name in missing:
            print("MISSING:", name)
        raise SystemExit(1 if missing else 0)

    rendered = []
    for spec in DOCUMENT_SPECS:
        path = render_spec(spec, args.out, force=args.force)
        rendered.append(os.path.basename(path))
    print(f"Rendered {len(rendered)} synthetic demo documents into {args.out}:")
    for name in rendered:
        size = os.path.getsize(os.path.join(args.out, name))
        print(f"  {name}  ({size // 1024} KB)")


if __name__ == "__main__":
    main()
