"""Image metadata (§3.14) and OCR (§3.19) extraction."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil

from PIL import Image, ImageOps

from .patterns import find_passwords

logger = logging.getLogger("crawler.media")

OCR_WHITELIST = "VISUALPING{}0123456789abcdefABCDEF"

# Common Windows install location for the UB-Mannheim Tesseract build (e.g.
# `winget install UB-Mannheim.TesseractOCR`), which doesn't add itself to
# PATH. Only used as a fallback when `tesseract` isn't already resolvable.
_WINDOWS_FALLBACK_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

_tesseract_checked = False
_tesseract_available = False


def _tesseract_ok() -> bool:
    global _tesseract_checked, _tesseract_available
    if _tesseract_checked:
        return _tesseract_available
    _tesseract_checked = True
    try:
        import pytesseract

        env_cmd = os.environ.get("TESSERACT_CMD")
        if env_cmd and os.path.isfile(env_cmd):
            pytesseract.pytesseract.tesseract_cmd = env_cmd
        elif shutil.which("tesseract") is None:
            for candidate in _WINDOWS_FALLBACK_PATHS:
                if os.path.isfile(candidate):
                    pytesseract.pytesseract.tesseract_cmd = candidate
                    break

        pytesseract.get_tesseract_version()
        _tesseract_available = True
    except Exception as exc:  # noqa: BLE001 - tesseract binary missing, etc.
        logger.warning("Tesseract OCR unavailable (%s) — §3.19 will be skipped", exc)
        _tesseract_available = False
    return _tesseract_available


def extract_image_text_fields(raw: bytes) -> list[str]:
    """§3.14 — pull every string-valued metadata field out of an image, per format."""
    texts: list[str] = []
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception:
        return texts

    fmt = (img.format or "").upper()

    # EXIF — JPEG/PNG(eXIf)/WebP/TIFF (Pillow >= 6.0 unifies these via getexif()).
    try:
        exif = img.getexif()
        for tag_id, value in exif.items():
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = value.decode("utf-8", "ignore")
                except Exception:
                    continue
            if isinstance(value, str):
                texts.append(value)
    except Exception:
        pass

    # PNG tEXt/zTXt/iTXt chunks + generic .info (also covers GIF comment ext,
    # and JPEG COM marker via img.info.get('comment')).
    try:
        for key, value in (img.text.items() if hasattr(img, "text") else []):
            if isinstance(value, str):
                texts.append(value)
    except Exception:
        pass
    try:
        for key, value in img.info.items():
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, (bytes, bytearray)):
                try:
                    texts.append(value.decode("utf-8", "ignore"))
                except Exception:
                    pass
    except Exception:
        pass

    # SVG is plain XML text, not a raster format — handled by caller via raw
    # bytes regex scan instead of Pillow. XMP (JPEG/PNG/WebP) is also plain
    # text embedded in the file, so the raw-bytes scan below catches it too.
    try:
        raw_text = raw.decode("utf-8", "ignore")
    except Exception:
        raw_text = ""
    if raw_text:
        texts.append(raw_text)

    return texts


def scan_image_metadata(raw: bytes) -> list[str]:
    hits: list[str] = []
    for text in extract_image_text_fields(raw):
        hits.extend(find_passwords(text))
    return list(dict.fromkeys(hits))


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    img = img.convert("L")  # grayscale
    w, h = img.size
    scale = max(1, 1200 // max(w, 1))
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    # Simple threshold to reduce 0/O/1/l/I ambiguity from anti-aliasing.
    img = img.point(lambda p: 255 if p > 140 else 0)
    return img


def ocr_image(raw: bytes) -> list[str]:
    """§3.19 — OCR rendered text in an image, whitelisted to the password alphabet."""
    if not _tesseract_ok():
        return []
    try:
        import pytesseract

        img = Image.open(io.BytesIO(raw))
        processed = _preprocess_for_ocr(img)
        config = f"--psm 6 -c tessedit_char_whitelist={OCR_WHITELIST}"
        text = pytesseract.image_to_string(processed, config=config)
        hits = find_passwords(text)
        if hits:
            return hits
        # Whitelisted pass can merge/garble delimiters; fall back to an
        # unrestricted pass and regex-scan normally.
        text_unrestricted = pytesseract.image_to_string(processed)
        return find_passwords(text_unrestricted)
    except Exception as exc:  # noqa: BLE001
        logger.debug("OCR failed: %s", exc)
        return []


SOURCEMAP_COMMENT_RE = re.compile(r"//[#@]\s*sourceMappingURL=(\S+)")


def find_sourcemap_url(source_text: str, headers: dict) -> str | None:
    for header_name in ("sourcemap", "x-sourcemap"):
        value = headers.get(header_name)
        if value:
            return value
    m = SOURCEMAP_COMMENT_RE.search(source_text or "")
    if m:
        return m.group(1)
    return None


def scan_sourcemap(sourcemap_json: dict) -> list[str]:
    hits: list[str] = []
    for content in sourcemap_json.get("sourcesContent") or []:
        if isinstance(content, str):
            hits.extend(find_passwords(content))
    # Some maps only ship `sources` (paths) without sourcesContent, or embed
    # the password in a source *path* itself — cheap to also check.
    for src in sourcemap_json.get("sources") or []:
        if isinstance(src, str):
            hits.extend(find_passwords(src))
    return list(dict.fromkeys(hits))
