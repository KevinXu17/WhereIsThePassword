"""Per-response classification + regex scanning for design.md §3 (active vectors)
and the cheap §4.1 presence signals for postponed vectors."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .patterns import find_passwords

DOC_EXTENSIONS = (".pdf", ".txt", ".csv", ".docx", ".zip")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif")
SVG_EXTENSIONS = (".svg",)
BINARY_HINT_EXTENSIONS = (".ico", ".wasm", ".woff", ".woff2", ".ttf", ".otf", ".eot")

FONT_FACE_UNICODE_RANGE_RE = re.compile(
    r"@font-face\s*{[^}]*unicode-range\s*:", re.IGNORECASE | re.DOTALL
)
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)


def url_ext(url: str) -> str:
    path = urlsplit(url).path.lower()
    for ext in (
        DOC_EXTENSIONS + AUDIO_EXTENSIONS + IMAGE_EXTENSIONS
        + SVG_EXTENSIONS + BINARY_HINT_EXTENSIONS + (".js", ".css", ".map")
    ):
        if path.endswith(ext):
            return ext
    return ""


def is_downloadable_doc(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in ("pdf", "csv", "wordprocessingml", "zip")) or ct == "text/plain":
        return True
    return url_ext(url) in DOC_EXTENSIONS


def is_audio(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if ct.startswith("audio/"):
        return True
    return url_ext(url) in AUDIO_EXTENSIONS


def is_image(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if ct.startswith("image/") and "svg" not in ct:
        return True
    return url_ext(url) in IMAGE_EXTENSIONS


def is_svg(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    return "svg" in ct or url_ext(url) in SVG_EXTENSIONS


def is_css(content_type: str, url: str) -> bool:
    return "css" in (content_type or "").lower() or url_ext(url) == ".css"


def is_js(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    return (
        "javascript" in ct or "ecmascript" in ct or ct == "application/x-javascript"
        or url_ext(url) == ".js"
    )


def is_json_ld_or_manifest(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "ld+json" in ct or "manifest+json" in ct


def is_binary_unclaimed(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if any(h in ct for h in ("font", "wasm", "octet-stream", "x-icon", "vnd.microsoft.icon")):
        return True
    return url_ext(url) in BINARY_HINT_EXTENSIONS


def font_face_has_unicode_range(css_text: str) -> bool:
    return bool(FONT_FACE_UNICODE_RANGE_RE.search(css_text or ""))


def extract_css_urls(css_text: str) -> list[str]:
    return CSS_URL_RE.findall(css_text or "")


def scan_headers(headers: dict) -> list[str]:
    blob = "\n".join(f"{k}: {v}" for k, v in headers.items())
    return find_passwords(blob)


def scan_status_text(status_text: str) -> list[str]:
    return find_passwords(status_text)


def scan_attr_texts(attr_texts: list[str]) -> list[str]:
    hits: list[str] = []
    for t in attr_texts:
        hits.extend(find_passwords(t))
    return list(dict.fromkeys(hits))


def scan_storage(storage: dict) -> list[str]:
    hits: list[str] = []
    for bucket in ("localStorage", "sessionStorage"):
        for k, v in (storage.get(bucket) or {}).items():
            hits.extend(find_passwords(k))
            hits.extend(find_passwords(v))
    hits.extend(find_passwords(storage.get("cookie")))
    return list(dict.fromkeys(hits))


def scan_indexeddb(records: list[str]) -> list[str]:
    hits: list[str] = []
    for r in records:
        hits.extend(find_passwords(r))
    return list(dict.fromkeys(hits))
