"""Image metadata (§3.14) and OCR (§3.19) extraction."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil

from PIL import Image, ImageOps

from .patterns import find_passwords, find_passwords_deep

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


def _add_text(texts: list[str], value: str) -> None:
    """Append a decoded metadata value, plus a null-stripped variant.

    EXIF UserComment (tag 0x9286) is stored per-spec as an 8-byte charset
    prefix (`UNICODE\\x00`, `ASCII\\x00\\x00\\x00`, ...) followed by the
    comment in *that* charset — Pillow hands it back as raw bytes and
    doesn't decode per that convention. Blindly decoding UTF-16 bytes as
    UTF-8 doesn't raise (every byte, including the interleaved 0x00s, is a
    valid single-byte UTF-8 codepoint), so `errors="ignore"` doesn't drop
    anything — it comes back as e.g. `V\\x00I\\x00S\\x00U\\x00A\\x00L...`,
    a real password sliced apart by literal NUL characters, which
    PASSWORD_RE (contiguous match) can never match. Stripping NULs
    reconstructs it as a plain substring (`UNICODEVISUALPING{...}` — the
    regex finds it fine as a substring of that). No-op on fields that
    never had embedded NULs, so this is safe to do unconditionally."""
    texts.append(value)
    if "\x00" in value:
        texts.append(value.replace("\x00", ""))


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
                _add_text(texts, value)
    except Exception:
        pass

    # PNG tEXt/zTXt/iTXt chunks + generic .info (also covers GIF comment ext,
    # and JPEG COM marker via img.info.get('comment')).
    try:
        for key, value in (img.text.items() if hasattr(img, "text") else []):
            if isinstance(value, str):
                _add_text(texts, value)
    except Exception:
        pass
    try:
        for key, value in img.info.items():
            if isinstance(value, str):
                _add_text(texts, value)
            elif isinstance(value, (bytes, bytearray)):
                try:
                    _add_text(texts, value.decode("utf-8", "ignore"))
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
    """§3.14. Only matches the wrapped VISUALPING{...} form — see
    find_passwords()/PASSWORD_RE. A bare 16-hex-char string turns up
    reliably in a JPEG COM marker (0xFFFE) right after EXIF UserComment on
    *every* sampled image, real password present or not (confirmed via a
    live byte-level dump of field-visit.jpg, which has a genuine wrapped
    password in UserComment *and* a decoy bare-hex COM marker, vs.
    office-plants.jpg, which has no wrapped string anywhere in the file at
    all, only that same decoy-shaped COM marker) — it's a deliberate trap
    for a scanner that treats any 16-hex string as a hit, not a second
    storage convention. Do not add a bare-hex fallback here."""
    hits: list[str] = []
    for text in extract_image_text_fields(raw):
        hits.extend(find_passwords_deep(text))
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
            hits.extend(find_passwords_deep(content))
    # Some maps only ship `sources` (paths) without sourcesContent, or embed
    # the password in a source *path* itself — cheap to also check.
    for src in sourcemap_json.get("sources") or []:
        if isinstance(src, str):
            hits.extend(find_passwords_deep(src))
    return list(dict.fromkeys(hits))
