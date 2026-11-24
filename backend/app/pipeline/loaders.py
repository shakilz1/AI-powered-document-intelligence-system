"""Task 1 — Document processing.

Turns any supported file into a `LoadedDocument`: per-page text (digital text layer or OCR)
plus, for spreadsheets/CSV, the parsed tables. File type is decided from magic bytes, not
the extension, so renamed or corrupted files are caught early.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
from PIL import Image, ImageSequence, UnidentifiedImageError

from ..config import settings
from . import ocr as ocr_mod

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"}
Image.MAX_IMAGE_PIXELS = 200_000_000  # guard against decompression bombs (default is ~89MP)

ProgressFn = Callable[[float, str], None]


class DocumentLoadError(Exception):
    """Raised for unsupported, corrupted, encrypted or empty files. Message is user-facing."""


@dataclass
class PageText:
    page_number: int
    source: str  # text|ocr|sheet|csv
    text: str
    ocr_confidence: float | None = None
    image: bytes | None = field(default=None, repr=False)  # JPEG of OCR'd pages, kept for Claude vision


@dataclass
class LoadedDocument:
    file_type: str
    pages: list[PageText] = field(default_factory=list)
    tables: list[tuple[str, pd.DataFrame]] = field(default_factory=list)  # (sheet name, frame)
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_used(self) -> bool:
        return any(p.source == "ocr" for p in self.pages)

    @property
    def ocr_confidence(self) -> float | None:
        vals = [p.ocr_confidence for p in self.pages if p.source == "ocr" and p.ocr_confidence is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"--- page {p.page_number} ---\n{p.text}" for p in self.pages)


def _keep_image() -> bool:
    return settings.llm_available and settings.llm_vision


def _to_jpeg(img: Image.Image, max_side: int = 1600) -> bytes:
    """Downscale + JPEG-compress a page image for the vision model (keeps memory and tokens low)."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ------------------------------------------------------------------ type sniffing

def sniff_type(path: Path) -> str:
    """Return pdf|image|xlsx|xls|csv based on content. Raises DocumentLoadError."""
    size = path.stat().st_size
    if size == 0:
        raise DocumentLoadError("File is empty (0 bytes)")
    with open(path, "rb") as fh:
        head = fh.read(8192)
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile as exc:
            raise DocumentLoadError("Corrupted XLSX (invalid zip container)") from exc
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        raise DocumentLoadError("Zip file is not an Excel workbook")
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if b"\x00" not in head:
        for enc in ("utf-8-sig", "cp1252"):
            try:
                txt = head.decode(enc)
            except UnicodeDecodeError:
                continue
            printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in txt) / max(1, len(txt))
            if printable > 0.97 and ("\n" in txt or len(head) < 8192) and any(d in txt for d in ",;\t|"):
                return "csv"
    raise DocumentLoadError("Unsupported or corrupted file (content does not match any supported format)")


# ------------------------------------------------------------------ PDF

def _pdf_page_text(page) -> str:
    """Rebuild lines from word boxes so table rows stay on one line (better than raw get_text)."""
    words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wordno
    if not words:
        return ""
    words.sort(key=lambda w: (round((w[1] + w[3]) / 2, 0), w[0]))
    lines: list[list] = []
    for w in words:
        cy, h = (w[1] + w[3]) / 2, max(1.0, w[3] - w[1])
        if lines:
            last = lines[-1]
            ly = sum((x[1] + x[3]) / 2 for x in last) / len(last)
            if abs(cy - ly) < 0.45 * h:
                last.append(w)
                continue
        lines.append([w])
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w[0])
        parts, prev_x1 = [], None
        for w in ln:
            if prev_x1 is not None and w[0] - prev_x1 > 12:  # visible column gap
                parts.append("   ")
            elif prev_x1 is not None:
                parts.append(" ")
            parts.append(w[4])
            prev_x1 = w[2]
        out.append("".join(parts))
    return "\n".join(out)


def load_pdf(path: Path, progress: ProgressFn | None = None) -> LoadedDocument:
    import pymupdf as fitz  # PyMuPDF

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise DocumentLoadError(f"Corrupted PDF: {exc}") from exc
    if doc.needs_pass and not doc.authenticate(""):
        raise DocumentLoadError("PDF is password-protected")
    if doc.page_count == 0:
        raise DocumentLoadError("PDF has no pages")

    out = LoadedDocument("pdf")
    n = doc.page_count
    failed_pages = 0
    for i in range(n):
        try:
            page = doc.load_page(i)
            text = _pdf_page_text(page)
            if len(text.strip()) >= settings.ocr_min_text_chars:
                out.pages.append(PageText(i + 1, "text", text))
            else:
                # Scanned page (no/little text layer) -> render and OCR.
                pix = page.get_pixmap(dpi=settings.ocr_dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                res = ocr_mod.ocr_image(img, settings.ocr_low_confidence)
                merged = res.text if len(res.text) >= len(text) else text
                out.pages.append(PageText(i + 1, "ocr", merged, round(res.confidence, 4),
                                          _to_jpeg(img) if _keep_image() else None))
        except ocr_mod.OCRUnavailable:
            raise
        except Exception as exc:  # one broken page must not kill a 1000-page file
            failed_pages += 1
            out.warnings.append(f"Page {i + 1} could not be read: {exc}")
            out.pages.append(PageText(i + 1, "text", ""))
        if progress:
            progress((i + 1) / n, f"page {i + 1}/{n}")
    doc.close()
    if failed_pages == n:
        raise DocumentLoadError("No page of the PDF could be read")
    return out


# ------------------------------------------------------------------ images

def load_image(path: Path, progress: ProgressFn | None = None) -> LoadedDocument:
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise DocumentLoadError(f"Corrupted or unreadable image: {exc}") from exc
    out = LoadedDocument("image")
    frames = [f.copy() for f in ImageSequence.Iterator(img)]  # multi-page TIFF
    for i, frame in enumerate(frames):
        res = ocr_mod.ocr_image(frame, settings.ocr_low_confidence)
        out.pages.append(PageText(i + 1, "ocr", res.text, round(res.confidence, 4),
                                  _to_jpeg(frame) if _keep_image() else None))
        if res.confidence and res.confidence < settings.ocr_low_confidence:
            out.warnings.append(f"Low OCR confidence on frame {i + 1}: {res.confidence:.2f}")
        if progress:
            progress((i + 1) / len(frames), f"frame {i + 1}/{len(frames)}")
    if not any(p.text.strip() for p in out.pages):
        out.warnings.append("OCR found no text in the image")
    return out


# ------------------------------------------------------------------ spreadsheets

def _frame_text(name: str, df: pd.DataFrame) -> str:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    lines = [f"[sheet: {name}]"]
    for row in df.itertuples(index=False):
        cells = ["" if pd.isna(v) else str(v) for v in row]
        lines.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(lines)


def load_excel(path: Path, kind: str, progress: ProgressFn | None = None) -> LoadedDocument:
    engine = "openpyxl" if kind == "xlsx" else "xlrd"
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, engine=engine, dtype=object)
    except Exception as exc:
        raise DocumentLoadError(f"Corrupted or unreadable {kind.upper()} workbook: {exc}") from exc
    out = LoadedDocument(kind)
    for i, (name, df) in enumerate(sheets.items()):
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            out.warnings.append(f"Sheet '{name}' is empty")
            continue
        out.tables.append((str(name), df.reset_index(drop=True)))
        out.pages.append(PageText(i + 1, "sheet", _frame_text(str(name), df)))
        if progress:
            progress((i + 1) / len(sheets), f"sheet {name}")
    if not out.tables:
        raise DocumentLoadError("Workbook contains no data")
    return out


def load_csv(path: Path, progress: ProgressFn | None = None) -> LoadedDocument:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None or not text.strip():
        raise DocumentLoadError("CSV is empty or not decodable")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    try:
        df = pd.read_csv(io.StringIO(text), sep=sep, header=None, dtype=object, skip_blank_lines=True, on_bad_lines="warn")
    except Exception as exc:
        raise DocumentLoadError(f"Malformed CSV: {exc}") from exc
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        raise DocumentLoadError("CSV contains no data")
    out = LoadedDocument("csv")
    out.tables.append(("csv", df.reset_index(drop=True)))
    out.pages.append(PageText(1, "csv", _frame_text("csv", df)))
    if progress:
        progress(1.0, "csv parsed")
    return out


# ------------------------------------------------------------------ entry point

def load_document(path: Path, progress: ProgressFn | None = None) -> LoadedDocument:
    kind = sniff_type(path)
    ext = path.suffix.lower()
    expected = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image", ".tif": "image", ".tiff": "image",
                ".xlsx": "xlsx", ".xls": "xls", ".csv": "csv"}.get(ext)
    if expected and expected != "csv" and kind == "csv":
        # A binary format whose bytes look like plain text is corrupted, not a CSV.
        raise DocumentLoadError(f"File has extension {ext} but its content is not a valid {expected.upper()} file")
    if kind == "pdf":
        doc = load_pdf(path, progress)
    elif kind == "image":
        doc = load_image(path, progress)
    elif kind in ("xlsx", "xls"):
        doc = load_excel(path, kind, progress)
    else:
        doc = load_csv(path, progress)
    if expected and expected != kind:
        doc.warnings.append(f"Extension {ext} does not match detected content type '{kind}'")
    return doc
