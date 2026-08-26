"""The one thing this whole crawler exists to find (design.md §1)."""
from __future__ import annotations

import base64
import html
import re
from urllib.parse import unquote

# Case-insensitive until we've confirmed from real captured samples that the
# target only ever emits lowercase hex (design.md §1).
PASSWORD_RE = re.compile(r"VISUALPING\{[0-9a-fA-F]{16}\}", re.IGNORECASE)

# The challenge homepage shows this exact string as its own "worked example"
# of the format and explicitly says it "is not one of the eight" — it isn't
# a real password no matter where else it turns up, so never report it as a
# find (design.md §1's pattern is otherwise unconditional).
KNOWN_PLACEHOLDER = "visualping{0000deadbeef0000}"

# A bare, unwrapped 16-hex-char string turns up in a JPEG COM marker
# (0xFFFE) right after EXIF UserComment on every sampled image, real
# password present or not (confirmed via a live byte-level dump:
# field-visit.jpg has a genuine wrapped password in UserComment *and* this
# same decoy-shaped COM marker; office-plants.jpg has the COM marker but no
# VISUALPING{...} wrapper anywhere in the file in any encoding). It's a
# deliberate trap for a scanner that treats any 16-hex string as a hit —
# the real storage convention is consistently the wrapped form below.
# Do NOT add a bare-hex fallback matcher; see media.py's scan_image_metadata.


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


# §3.5/§3.4 caveats: a password can reach the page as something other than
# the literal `VISUALPING{...}` string — base64 (bare, or wrapped in a JS
# `atob(...)` call), `String.fromCharCode(...)` construction, URL
# percent-encoding, or CSS hex-character escapes (`content: "\0056..."`).
# These are all cheap, static, best-effort decode-then-rescan passes — no
# runtime JS execution, just text transforms — so false "decode" attempts
# that produce garbage are harmless: the *decoded* text still has to match
# PASSWORD_RE exactly, which random garbage practically never does.
BASE64_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
ATOB_CALL_RE = re.compile(r"atob\(\s*['\"]([A-Za-z0-9+/=]+)['\"]\s*\)")
# Deliberately NOT anchored to `String.fromCharCode(...)` specifically — that
# only matches the one call shape where the codes are inlined directly in
# the call. Real code just as often does
# `String.fromCharCode.apply(null, [86,73,...])`, `String.fromCharCode(...arr)`
# (spread of a variable defined elsewhere), or builds the array once and
# reuses it via `.map(c => String.fromCharCode(c)).join('')` — none of which
# have the digits sitting directly inside a `fromCharCode(` call at all. A
# bare numeric array/paren-list of plausible char codes, wherever it shows up
# in the text, covers all of those shapes without needing to parse which API
# call eventually consumes it. Decimal and `0x..` hex tokens both accepted.
NUMERIC_CHAR_ARRAY_RE = re.compile(
    r"[\[\(]\s*((?:0x[0-9a-fA-F]+|\d{1,3})(?:\s*,\s*(?:0x[0-9a-fA-F]+|\d{1,3})){4,})\s*[\]\)]"
)
CSS_HEX_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{4,6})\s?")
JS_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
JS_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _try_base64_decode(candidate: str) -> str | None:
    padded = candidate + "=" * ((-len(candidate)) % 4)
    try:
        return base64.b64decode(padded, validate=False).decode("utf-8", "ignore")
    except Exception:
        return None


def _decode_css_hex_escapes(text: str) -> str:
    def repl(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)

    return CSS_HEX_ESCAPE_RE.sub(repl, text)


def _decode_js_escapes(text: str) -> str:
    text = JS_UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = JS_HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return text


def _parse_int_token(token: str) -> int | None:
    token = token.strip()
    try:
        return int(token, 16) if token.lower().startswith("0x") else int(token)
    except Exception:
        return None


def _decode_numeric_char_arrays(text: str) -> list[str]:
    """Every bracketed/parenthesized run of 5+ small integers, decoded as
    char codes — independent of whatever call (if any) actually consumes it.
    See NUMERIC_CHAR_ARRAY_RE's comment for why this is deliberately not
    tied to `String.fromCharCode(...)`'s literal call syntax."""
    decoded: list[str] = []
    for m in NUMERIC_CHAR_ARRAY_RE.finditer(text):
        codes: list[int] = []
        ok = True
        for token in m.group(1).split(","):
            value = _parse_int_token(token)
            if value is None or not (0 <= value <= 0x10FFFF):
                ok = False
                break
            codes.append(value)
        if ok and codes:
            try:
                decoded.append("".join(chr(c) for c in codes))
            except Exception:
                pass
    return decoded


def find_passwords_deep(text: str | None) -> list[str]:
    """Like `find_passwords`, plus a handful of cheap decode-then-rescan
    passes for the ways design.md warns a password can be hidden from a
    plain literal-text regex without ever being executed as real code:

    - bare base64 runs, and base64 specifically passed to `atob(...)`
    - char-code construction — `String.fromCharCode(...)` and friends
      (`.apply(null, [...])`, spread of an array defined elsewhere, a bare
      array later `.map`'d) — matched as "any bracketed/paren'd run of
      plausible char codes", not tied to one specific call shape
    - URL percent-encoding (`%56%49...`)
    - CSS hex-character escapes (`content: "\\0056\\0049..."`)
    - HTML entities (`&#86;&#73;...` / `&amp;` etc.)
    - JS string escapes (`\\x56\\x49...`, `\\u0056\\u0049...`)

    Every decode attempt is best-effort: failures are swallowed, and a
    decoded blob still has to match PASSWORD_RE verbatim to count, so this
    can't manufacture false positives — only recover matches a flat regex
    scan of the raw text would miss."""
    if not text:
        return []
    hits = list(find_passwords(text))

    for m in ATOB_CALL_RE.finditer(text):
        decoded = _try_base64_decode(m.group(1))
        if decoded:
            hits.extend(find_passwords(decoded))

    for decoded_str in _decode_numeric_char_arrays(text):
        hits.extend(find_passwords(decoded_str))

    for m in BASE64_CANDIDATE_RE.finditer(text):
        decoded = _try_base64_decode(m.group(0))
        if decoded:
            hits.extend(find_passwords(decoded))

    try:
        hits.extend(find_passwords(unquote(text)))
    except Exception:
        pass

    if "&" in text:
        try:
            hits.extend(find_passwords(html.unescape(text)))
        except Exception:
            pass

    if "\\" in text:
        hits.extend(find_passwords(_decode_css_hex_escapes(text)))
        hits.extend(find_passwords(_decode_js_escapes(text)))

    return list(dict.fromkeys(hits))


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
