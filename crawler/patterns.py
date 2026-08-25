"""The one thing this whole crawler exists to find (design.md §1)."""
from __future__ import annotations

import re

# Case-insensitive until we've confirmed from real captured samples that the
# target only ever emits lowercase hex (design.md §1).
PASSWORD_RE = re.compile(r"VISUALPING\{[0-9a-fA-F]{16}\}", re.IGNORECASE)

# The challenge homepage shows this exact string as its own "worked example"
# of the format and explicitly says it "is not one of the eight" — it isn't
# a real password no matter where else it turns up, so never report it as a
# find (design.md §1's pattern is otherwise unconditional).
KNOWN_PLACEHOLDER = "visualping{0000deadbeef0000}"


def find_passwords(text: str | None) -> list[str]:
    """Return every VISUALPING{...} match in text, de-duplicated, order preserved."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in PASSWORD_RE.finditer(text):
        if m.group(0).lower() == KNOWN_PLACEHOLDER:
            continue
        seen.setdefault(m.group(0), None)
    return list(seen.keys())


# §3.3 ("Comments, data-* attributes, hidden inputs/elements, <meta> tags")
# and §3.9 ("<script type=application/ld+json>") are explicitly *not*
# independent scanning passes in design.md — the doc says both exist only to
# label, in the §4 audit log, *where within the document* a §3.2 raw-body
# hit actually came from. This does that labeling: a lightweight structural
# lookback from the match position, not a real HTML parse (consistent with
# the raw-text-regex approach used everywhere else in this codebase).
_TAG_OPEN_RE = re.compile(r"<([a-zA-Z][\w-]*)((?:\s+[^<>]*)?)>", re.DOTALL)


def classify_html_context(text: str, start: int) -> str:
    """Return the §3.x vector id that best labels where `text[start]` sits
    structurally within an HTML document — the base §3.2 raw-body vector
    unless it's inside one of the specific containers §3.3/§3.9 call out."""
    before = text[:start]

    if before.rfind("<!--") > before.rfind("-->"):
        return "3.3"  # HTML comment

    script_open = before.rfind("<script")
    if script_open > before.rfind("</script>"):
        tag_end = text.find(">", script_open)
        tag = text[script_open: tag_end + 1] if tag_end != -1 else ""
        if re.search(r'type\s*=\s*["\']application/(ld\+json|json)["\']', tag, re.IGNORECASE):
            return "3.9"  # JSON-LD / embedded JSON state
        return "3.2"  # plain inline <script> — that's §3.5's territory, not ours; leave as baseline

    meta_open = before.rfind("<meta")
    if meta_open != -1:
        tag_end = text.find(">", meta_open)
        if tag_end != -1 and meta_open < start <= tag_end:
            return "3.3"  # <meta> tag content/value attribute

    # Is `start` inside the attribute list of *some* tag (i.e. between an
    # unclosed `<` and its `>`)? If so, check whether that tag looks like a
    # data-* attribute or a hidden input/element.
    tag_open = before.rfind("<")
    if tag_open > before.rfind(">"):
        tag_end = text.find(">", tag_open)
        tag_text = text[tag_open: tag_end + 1] if tag_end != -1 else text[tag_open: start + 40]
        if re.search(r"\bdata-[\w-]+\s*=", tag_text, re.IGNORECASE):
            return "3.3"  # data-* attribute
        if re.search(r'type\s*=\s*["\']hidden["\']', tag_text, re.IGNORECASE) or re.search(
            r'\bhidden\b', tag_text, re.IGNORECASE
        ):
            return "3.3"  # hidden input/element
        return "3.2"  # some other tag attribute — not one §3.3 specifically names

    return "3.2"  # plain visible text


def find_html_passwords_labeled(text: str | None) -> list[tuple[str, str]]:
    """Like `find_passwords`, but for raw HTML specifically: returns
    (vector_label, password) pairs so the §4 audit log can attribute each
    hit to §3.2 (visible text), §3.3 (comment/data-*/hidden/meta), or §3.9
    (JSON-LD), per design.md's explicit instruction for those rows."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in PASSWORD_RE.finditer(text):
        pw = m.group(0)
        if pw.lower() == KNOWN_PLACEHOLDER or pw in seen:
            continue
        seen.add(pw)
        out.append((classify_html_context(text, m.start()), pw))
    return out
