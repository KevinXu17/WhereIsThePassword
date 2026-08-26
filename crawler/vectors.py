"""Human-readable names for the design.md §3 vector ids.

Bare ids like "3.19" mean nothing without design.md open next to you — this
gives every id used anywhere in the codebase a short title, so the audit
log and end-of-run summary are self-describing on their own.
"""
from __future__ import annotations

VECTOR_LABELS: dict[str, str] = {
    "3.1": "HTTP response headers",
    "3.2": "HTTP response body (page navigation)",
    "3.3": "HTML comment / data-* attr / hidden element / <meta> tag",
    "3.4": "CSS",
    "3.5": "JS (inline/external source, console output)",
    "3.6": "Client-side storage (localStorage/sessionStorage/IndexedDB)",
    "3.6/3.7": "Client-side storage + cookies",
    "3.7": "Cookies (JS-visible, document.cookie)",
    "3.9": "Structured data (JSON-LD / embedded JSON)",
    "3.10": "Non-rendered text attributes (alt/title/aria-*/placeholder)",
    "3.11": "Background XHR/fetch response body",
    "3.12": "WebSocket messages [postponed]",
    "3.13": "Downloadable files (PDF/TXT/CSV/DOCX/ZIP) [postponed]",
    "3.14": "Image metadata (EXIF/tEXt/IPTC/XMP/...)",
    "3.15": "QR codes / encoded images [postponed]",
    "3.16": "Source map files",
    "3.17": "Web app manifest",
    "3.18": "Non-200 / error page body",
    "3.19": "Rendered text in an image (OCR)",
    "3.20": "Canvas-drawn text (screenshot + OCR)",
    "3.21": "Custom web-font glyph substitution (screenshot + OCR)",
    "3.22": "SVG text-as-paths (screenshot + OCR)",
    "3.23": "Audio-encoded password [postponed]",
    "3.24": "Steganography in image pixel data [postponed]",
    "3.25": "Strings inside binary files",
    "3.26": "Full-page screenshot + OCR [postponed]",
    "3.27": "URL string itself",
    "3.28": "HTTP status line reason phrase",
    # Extensions beyond design.md's original numbering, added after a
    # follow-up code review identified gaps: rendered/SPA content, Cache
    # Storage, and blob:/data: URLs that no vector above could ever reach.
    "3.29": "Rendered DOM (post-JS) full-text scan, incl. open shadow DOM",
    "3.30": "Cache Storage API",
    "3.31": "blob:/data: URL content",
    "3.32": "Server-Sent Events (SSE) [postponed]",
    "3.33": "GeoIP-gated page (proxy bypass)",
}


def label_for(vector: str) -> str:
    """Best-effort label for a vector id, including combined ids like
    "3.6/3.7" — falls back to the raw id (or "" for the no-hit sentinel)
    rather than raising, since callers use this for display, not logic."""
    if not vector:
        return ""
    if vector in VECTOR_LABELS:
        return VECTOR_LABELS[vector]
    # Combined/compound ids not listed verbatim (e.g. a future "3.x/3.y"):
    # join whatever parts we do recognize.
    parts = vector.split("/")
    labels = [VECTOR_LABELS.get(p.strip()) for p in parts]
    if all(labels):
        return " + ".join(labels)  # type: ignore[arg-type]
    return vector
