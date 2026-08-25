"""URL normalization, origin checks, and pagination-trial capping (design.md §2.1-2.4)."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from .config import TARGET_URL, PAGINATION_TRIALS

_ORIGIN = urlsplit(TARGET_URL)
NON_PAGE_SCHEMES = {"mailto", "tel", "javascript", "data"}


def resolve(base: str, link: str) -> str | None:
    """Resolve a possibly-relative link against base. None if not http(s)."""
    if not link:
        return None
    link = link.strip()
    if not link or link.startswith("#"):
        return None
    scheme = urlsplit(link).scheme
    if scheme and scheme in NON_PAGE_SCHEMES:
        return None
    try:
        absolute = urljoin(base, link)
    except ValueError:
        return None
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    return absolute


_INDEX_SUFFIXES = ("index.html", "index.htm")


def _canonical_path(path: str) -> str:
    """Collapse `/x/index.html` onto `/x/` — a real crawl showed both forms
    discovered separately (nav `<a>`s use `/blog/index.html`, other links on
    the same site use `/blog/`) and static webservers serve the same file
    for both, so treating them as distinct wasted ~7% of the §2.2 visit
    budget re-fetching identical content."""
    for suffix in _INDEX_SUFFIXES:
        if path.endswith("/" + suffix):
            return path[: -len(suffix)]
    return path


def normalize(url: str) -> str:
    """Strip fragment; sort query params; collapse index.html so equivalent
    URLs dedupe in `visited`/`enqueued`."""
    parts = urlsplit(url)
    query_pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
    query = urlencode(query_pairs)
    path = _canonical_path(parts.path or "/")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def is_same_origin(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc) == (_ORIGIN.scheme, _ORIGIN.netloc)


class PaginationLimiter:
    """Enforces §2.4: at most PAGINATION_TRIALS (2) distinct query-string
    variants of the same path, TOTAL — shared across every param name, not
    tracked separately per name. So `?page=1`/`?page=2`/`?page=3`/... and
    `?p=1`/`?p=2`/... and `?list=A`/`?list=B`/... and `?v=1`/`?v=2`/`?v=3`/...
    and `?ref=nav`/`?utm_source=x`/`?hl=en` are all just "another query
    string" competing for the *same* 2-slot budget on that path — whichever
    2 variants (in any mix of param names) get discovered first are the ones
    tried; every one after that is rejected regardless of which param it
    uses. This matters because a real crawl of the target showed the
    URL-space-multiplying problem isn't limited to literal pagination —
    decorative/tracking params (`ref`, `utm_source`, `hl`, `v`, ...) hit the
    same underlying page just as `page=N` would. An earlier version of this
    class capped trials per (path, param-name) instead of per path, which
    let each *different* param name burn its own separate budget on the same
    path (measured: only 15 of 500 visits saved, vs. 45+ once scoped to the
    whole path). The bare (no-query) URL is always allowed since it's the
    canonical form of the page, not a "trial".
    """

    def __init__(self) -> None:
        self._seen_query_variants: dict[str, set[str]] = {}  # path -> {normalized query string}

    def allow(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.query:
            return True
        variant = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        variants = self._seen_query_variants.setdefault(_canonical_path(parts.path), set())
        if variant in variants:
            return True
        if len(variants) >= PAGINATION_TRIALS:
            return False
        variants.add(variant)
        return True
