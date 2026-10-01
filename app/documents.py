"""Turn an uploaded PDF or image into positioned words.

* PDFs with a text layer are read directly (exact text, confidence 1.0).
* Scanned PDFs and photos are cleaned up with OpenCV (grayscale, denoise,
  deskew, contrast) and read with Tesseract, keeping each word's confidence.

Every word keeps a bounding box normalised to 0..1 of its page, so the review
screen can highlight exactly where a value was read from.
"""
from __future__ import annotations

import io
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .config import settings

try:  # PyMuPDF
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None

import pytesseract

if settings.tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

VIEW_DPI = 150          # page images shown in the review screen
OCR_DPI = 300           # resolution used when a PDF page must be OCR'd
MIN_TEXT_LAYER_WORDS = 25


@dataclass
class Word:
    text: str
    conf: float
    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.text, "c": round(self.conf, 3), "p": self.page,
                "b": [round(self.x0, 4), round(self.y0, 4), round(self.x1, 4), round(self.y1, 4)]}


@dataclass
class Page:
    index: int
    image_path: str
    width: int
    height: int
    rotation_corrected: float = 0.0


@dataclass
class DocumentText:
    words: list[Word] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    method: str = "text-layer"
    quality: float = 1.0

    @property
    def text(self) -> str:
        from .layout import build_lines
        return "\n".join(line.text for line in build_lines(self.words))


class UnsupportedDocument(ValueError):
    pass


def read_document(path: Path, stem: str) -> DocumentText:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path, stem)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}:
        image = Image.open(path)
        image = ImageOps.exif_transpose(image).convert("RGB")
        doc = DocumentText(method="ocr")
        _ocr_page(image, 0, stem, doc)
        doc.quality = _mean_conf(doc.words)
        return doc
    raise UnsupportedDocument(f"Unsupported file type: {suffix or 'unknown'}")


# --------------------------------------------------------------------------- PDF

def _horizontal_words(page) -> list[tuple[float, float, float, float, str]]:
    """Words from horizontal text only. Rotated text (stamps, watermarks such as
    "PAID" or "SECOND NOTICE") is skipped so it can't bleed into table rows."""
    out = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1, 0))
            if abs(dy) > 0.05 or dx <= 0:
                continue
            for span in line.get("spans", []):
                word, box = [], None
                for ch in span.get("chars", []) + [{"c": " ", "bbox": None}]:
                    if ch["c"].isspace():
                        if word and box:
                            out.append((*box, "".join(word)))
                        word, box = [], None
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    word.append(ch["c"])
                    box = [x0, y0, x1, y1] if box is None else [min(box[0], x0), min(box[1], y0),
                                                                 max(box[2], x1), max(box[3], y1)]
    return out


def _read_pdf(path: Path, stem: str) -> DocumentText:
    pdf = fitz.open(path)
    doc = DocumentText(method="text-layer")
    text_words: list[Word] = []
    for index, page in enumerate(pdf):
        width, height = page.rect.width, page.rect.height
        for x0, y0, x1, y1, text in _horizontal_words(page):
            if text.strip():
                text_words.append(Word(text, 1.0, index, x0 / width, y0 / height, x1 / width, y1 / height))

    if len(text_words) >= MIN_TEXT_LAYER_WORDS:
        doc.words = text_words
        for index, page in enumerate(pdf):
            pix = page.get_pixmap(dpi=VIEW_DPI)
            image_path = settings.pages_dir / f"{stem}_p{index + 1}.png"
            pix.save(str(image_path))
            doc.pages.append(Page(index, image_path.name, pix.width, pix.height))
        doc.quality = 1.0
        return doc

    # Scanned PDF: rasterise each page and OCR it.
    doc.method = "ocr"
    for index, page in enumerate(pdf):
        pix = page.get_pixmap(dpi=OCR_DPI)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        _ocr_page(image, index, stem, doc)
    doc.quality = _mean_conf(doc.words)
    return doc


# --------------------------------------------------------------------------- OCR

def _mean_conf(words: list[Word]) -> float:
    confs = [w.conf for w in words if len(w.text) > 1]
    return round(statistics.fmean(confs), 3) if confs else 0.0


def _estimate_skew(gray: np.ndarray) -> float:
    """Estimate page skew (degrees) by projection profile: the rotation that makes
    text rows sharpest maximises the variance of the horizontal ink histogram."""
    if cv2 is None:
        return 0.0
    scale = min(1.0, 1100 / gray.shape[1])
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))  # drop speckle
    h, w = ink.shape
    center = (w / 2, h / 2)

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(ink, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        profile = rotated.sum(axis=1, dtype=np.float64)
        return float(np.var(np.diff(profile)))

    coarse = max(np.arange(-6.0, 6.01, 0.5), key=score)
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.05), key=score)
    return round(float(fine), 2) if abs(fine) >= 0.2 else 0.0


def _enhance(image: Image.Image) -> tuple[Image.Image, Image.Image, float]:
    """Return (view image, OCR image, skew angle corrected)."""
    rgb = np.array(image)
    if cv2 is None:
        gray = ImageOps.autocontrast(image.convert("L"))
        return image, gray, 0.0

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Upscale small photos so characters are ~30px tall for Tesseract.
    if gray.shape[1] < 1800:
        factor = 1800 / gray.shape[1]
        gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        rgb = cv2.resize(rgb, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)

    angle = _estimate_skew(gray)
    if angle:
        h, w = gray.shape
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        gray = cv2.warpAffine(gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
        rgb = cv2.warpAffine(rgb, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))

    denoised = cv2.fastNlMeansDenoising(gray, None, h=12, templateWindowSize=7, searchWindowSize=21)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    contrasted = clahe.apply(denoised)
    return Image.fromarray(rgb), Image.fromarray(contrasted), angle


def _ocr_page(image: Image.Image, index: int, stem: str, doc: DocumentText) -> None:
    view, ocr_image, angle = _enhance(image)
    width, height = ocr_image.size

    data = pytesseract.image_to_data(
        ocr_image, lang="eng", config="--oem 1 --psm 3 -c preserve_interword_spaces=1",
        output_type=pytesseract.Output.DICT,
    )
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        doc.words.append(Word(text, max(0.0, min(conf / 100.0, 1.0)), index,
                              x / width, y / height, (x + w) / width, (y + h) / height))

    view_w = 1275  # ~150 dpi letter width
    if view.size[0] > view_w:
        view = view.resize((view_w, int(view.size[1] * view_w / view.size[0])), Image.LANCZOS)
    image_path = settings.pages_dir / f"{stem}_p{index + 1}.png"
    view.save(image_path, optimize=True)
    doc.pages.append(Page(index, image_path.name, view.size[0], view.size[1], round(angle, 2)))
