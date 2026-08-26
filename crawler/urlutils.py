"""URL normalization, origin checks, and pagination-trial capping (design.md §2.1-2.4)."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from .config import TARGET_URL, PAGINATION_TRIALS, PAGINATION_PARAM_NAMES

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
    """Strip fragment; drop decorative/tracking query params; sort what's
    left; collapse index.html — so equivalent URLs dedupe in
    `visited`/`enqueued`.

    Only `PAGINATION_PARAM_NAMES` (`page`, `p`, `pg`, `offset`, ...) survive
    into the dedup key — every other query param (`ref`, `utm_source`,
    `hl`, `v`, ...) is decorative/tracking noise that doesn't change page
    content, confirmed by a real crawl of the target where 25% of the
    500-visit budget (125/500) went to re-visiting already-seen content
    under a decorative-param variant, starving 75 genuinely unvisited
    pages out of the budget entirely. Dropping them here — instead of
    merely rate-limiting them via `PaginationLimiter` — means a decorative
    variant collapses onto the *same* dedup key as the canonical page and
    is rejected by the plain `key in visited/enqueued` check before it can
    consume a visit-budget slot at all, not just after 2 trials."""
    parts = urlsplit(url)
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() in PAGINATION_PARAM_NAMES
    )
    query = urlencode(query_pairs)
    path = _canonical_path(parts.path or "/")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def is_same_origin(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc) == (_ORIGIN.scheme, _ORIGIN.netloc)


class PaginationLimiter:
    """Enforces §2.4: named pagination params (`PAGINATION_PARAM_NAMES` —
    `page`, `p`, `pg`, `offset`, ...) get a PAGINATION_TRIALS (2)-slot
    budget of distinct *values* per (path, param-name). This is §2.4's
    literal example: `?page=1`, `?page=2` allowed, `?page=3`+ rejected.

    Decorative/tracking params (`ref`, `utm_source`, `hl`, `v`, ...) are
    NOT this class's job any more — `normalize()` now drops them from the
    dedup key entirely, so a decorative variant of an already-seen page is
    rejected by the plain `key in visited/enqueued` check in
    `_maybe_enqueue` before it ever reaches `allow()`, at zero visit-budget
    cost. (An earlier version gave decorative params their own 2-trial
    "generic" bucket here instead of stripping them upstream — a real
    crawl showed that cost 125/500, 25%, of the visit budget re-fetching
    already-seen content, starving 75 genuinely new pages out of the
    budget entirely. See `normalize()`'s docstring.)

    The bare (no-query) URL is always allowed unconditionally — it's the
    canonical form of the page, not a "trial".
    """

    def __init__(self) -> None:
        self._seen_pagination_values: dict[tuple[str, str], set[str]] = {}  # (path, param) -> values

    def allow(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.query:
            return True
        path = _canonical_path(parts.path)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        pagination_pairs = [(k, v) for k, v in pairs if k.lower() in PAGINATION_PARAM_NAMES]

        # Decide first, mutate only if the whole URL is allowed — otherwise a
        # rejected URL could still burn a trial slot before being rejected.
        to_add: list[tuple[tuple[str, str], str]] = []
        for key, value in pagination_pairs:
            sig = (path, key.lower())
            values = self._seen_pagination_values.get(sig, set())
            if value in values:
                continue
            if len(values) >= PAGINATION_TRIALS:
                return False
            to_add.append((sig, value))

        for sig, value in to_add:
            self._seen_pagination_values.setdefault(sig, set()).add(value)
        return True
