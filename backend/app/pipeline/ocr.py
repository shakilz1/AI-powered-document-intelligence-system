"""OCR engine wrapper with image pre-processing for poor-quality scans.

Primary engine: RapidOCR (PaddleOCR models on ONNX Runtime) — pip-installable, no system
binary, good accuracy on printed documents. Fallback: Tesseract via pytesseract if installed.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

_local = threading.local()


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class OCRBox:
    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class OCRResult:
    text: str
    confidence: float  # mean box confidence, 0..1 (0 when nothing recognised)
    boxes: list[OCRBox]
    variant: str  # which pre-processing variant won


def _engine():
    eng = getattr(_local, "engine", None)
    if eng is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on install
            raise OCRUnavailable("rapidocr-onnxruntime is not installed") from exc
        # Cap ONNX threads per engine: N worker processes x all-cores-each oversubscribes the CPU
        # (measured: 5 parallel OCR docs went from ~4 s to ~35 s each without this cap).
        import os

        from ..config import settings

        cpus = os.cpu_count() or 2
        parallel_jobs = 1 if settings.process_inline else max(1, settings.worker_threads)
        threads = settings.ocr_threads or max(1, cpus // parallel_jobs)
        eng = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1)
        _local.engine = eng
    return eng


def engine_name() -> str:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return "rapidocr"
    except Exception:  # pragma: no cover
        try:
            import pytesseract  # noqa: F401

            return "tesseract"
        except Exception:
            return "none"


# ---------------------------------------------------------------- pre-processing

def preprocess(img: Image.Image, variant: str = "standard") -> Image.Image:
    """Normalise an image for OCR. Variants are tried in order when confidence is low."""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    g = ImageOps.grayscale(img)
    # Upscale small / low-resolution images: OCR models want ~20-40px tall glyphs.
    w, h = g.size
    if max(w, h) < 1400:
        scale = 1400 / max(w, h)
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if variant == "standard":
        g = ImageOps.autocontrast(g, cutoff=1)
    elif variant == "denoise":
        g = g.filter(ImageFilter.MedianFilter(3))
        g = ImageOps.autocontrast(g, cutoff=2)
        g = g.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    elif variant == "binarize":
        g = g.filter(ImageFilter.MedianFilter(3))
        arr = np.asarray(g, dtype=np.uint8)
        thr = _otsu(arr)
        g = Image.fromarray(((arr > thr) * 255).astype(np.uint8))
    return g.convert("RGB")


def _otsu(arr: np.ndarray) -> int:
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)
    total = arr.size
    sum_total = np.dot(np.arange(256), hist)
    sum_b = w_b = 0.0
    best, thr = 0.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_total - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, thr = between, t
    return thr


# ---------------------------------------------------------------- recognition

def _run_rapid(img: Image.Image) -> list[OCRBox]:
    eng = _engine()
    result, _ = eng(np.asarray(img))
    boxes: list[OCRBox] = []
    for item in result or []:
        pts, text, conf = item
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        boxes.append(OCRBox(str(text), float(conf), min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _run_tesseract(img: Image.Image) -> list[OCRBox]:  # pragma: no cover - optional
    import pytesseract  # type: ignore

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    boxes = []
    for i, txt in enumerate(data["text"]):
        if txt.strip() and float(data["conf"][i]) >= 0:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            boxes.append(OCRBox(txt, float(data["conf"][i]) / 100.0, x, y, x + w, y + h))
    return boxes


def boxes_to_lines(boxes: list[OCRBox]) -> str:
    """Rebuild reading-order lines: group boxes whose vertical centres overlap, sort by x."""
    if not boxes:
        return ""
    boxes = sorted(boxes, key=lambda b: ((b.y0 + b.y1) / 2, b.x0))
    lines: list[list[OCRBox]] = []
    for b in boxes:
        cy = (b.y0 + b.y1) / 2
        h = max(1.0, b.y1 - b.y0)
        if lines:
            last = lines[-1]
            ly = sum((x.y0 + x.y1) / 2 for x in last) / len(last)
            lh = sum(x.y1 - x.y0 for x in last) / len(last)
            if abs(cy - ly) < 0.5 * max(h, lh):
                last.append(b)
                continue
        lines.append([b])
    out = []
    for ln in lines:
        ln.sort(key=lambda b: b.x0)
        out.append("   ".join(b.text.strip() for b in ln))
    return "\n".join(out)


def ocr_image(img: Image.Image, low_confidence: float = 0.80) -> OCRResult:
    """OCR with automatic retry on alternative pre-processing when confidence is low."""
    name = engine_name()
    if name == "none":
        raise OCRUnavailable("No OCR engine installed (pip install rapidocr-onnxruntime)")
    run = _run_rapid if name == "rapidocr" else _run_tesseract

    best: OCRResult | None = None
    for variant in ("standard", "denoise", "binarize"):
        try:
            boxes = run(preprocess(img, variant))
        except OCRUnavailable:
            raise
        except Exception as exc:  # engine failure on a weird image — try next variant
            log.warning("OCR variant %s failed: %s", variant, exc)
            continue
        conf = float(np.mean([b.confidence for b in boxes])) if boxes else 0.0
        chars = sum(len(b.text) for b in boxes)
        res = OCRResult(boxes_to_lines(boxes), conf, boxes, variant)
        # Score: confidence weighted by how much text was recovered.
        if best is None or (conf * np.log1p(chars)) > (best.confidence * np.log1p(len(best.text))):
            best = res
        if conf >= low_confidence and chars > 20:
            break
    return best or OCRResult("", 0.0, [], "none")
