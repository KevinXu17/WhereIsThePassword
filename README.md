# Where Is The Password — Crawler

An authenticated BFS crawler that clicks its way through the target site
like a real browser (via Playwright) and scans every response it sees for
`VISUALPING{<16 hex chars>}` tokens, per [`design.md`](design.md).

## How it works, in short

**Finding pages** — breadth-first, starting from the homepage, capped at
500 visits total:

- `<a>`/`<area>` links, `<iframe>` `src` (recursed into fully — its own
  DOM/storage/cache, not just raw HTML), redirects (`Location` header +
  `<meta refresh>`)
- Forms — submitted headlessly with dummy field values, trying every
  distinct `<select>` option (not just the first)
- JS click / SPA-router elements — actually clicked via Playwright and
  diffed for URL changes
- "Load more" / infinite-scroll triggers — scrolled and clicked
  repeatedly until nothing new appears
- Background XHR/fetch response bodies — regex-scanned for further
  URL-shaped strings
- Pagination-style query params get only 2 trial values per path; every
  other query param is decorative and gets dropped from the dedup key
  entirely (not fetched as a separate "page")

**Finding passwords** — every response is classified and scanned by
where it lives:

| What | Vector(s) |
|---|---|
| HTTP headers, status line, redirects | 3.1, 3.28 |
| Raw page body (pre-render), incl. error pages | 3.2, 3.18 |
| HTML comments / `data-*` / hidden elements / `<meta>` / JSON-LD | 3.3, 3.9 |
| CSS text, `.map` source maps | 3.4, 3.16 |
| JS source + browser console output | 3.5 |
| `localStorage`/`sessionStorage`/cookies/IndexedDB/Cache Storage/`window.name`/`history.state` | 3.6, 3.7, 3.30 |
| Non-rendered attributes (`alt`/`title`/`aria-*`/`placeholder`) | 3.10 |
| Other XHR/fetch bodies, web-app manifest | 3.11, 3.17 |
| Image metadata (EXIF/tEXt/IPTC/XMP) + OCR of rendered text | 3.14, 3.19 |
| Canvas / SVG-as-paths / custom web-font glyphs — via targeted screenshot+OCR | 3.20, 3.21, 3.22 |
| Strings in unclaimed binary files (favicon/fonts/wasm) | 3.25 |
| URL string itself, at enqueue time | 3.27 |
| Rendered DOM post-JS, incl. open shadow DOM | 3.29 |
| `blob:`/`data:` URL content | 3.31 |
| A specific known GeoIP-gated page (opt-in proxy bypass) | 3.33 |

Every one of those text scans also runs a **decode-then-rescan** pass
(base64/`atob()`, char-code arrays, percent-encoding, CSS/JS hex escapes,
HTML entities) so a password hidden behind any of those doesn't need to
match the literal `VISUALPING{...}` string verbatim in the raw source.

Full rationale for each vector, and what's deliberately *not* automated
(steganography, QR decoding, audio transcription, WebSocket/SSE payloads),
is in [`design.md`](design.md) and the "What it implements" section below.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Copy `.env.example` to `.env` and fill in `TARGET_URL` / `AUTH_USERNAME` /
`AUTH_PASSWORD` (already done in this repo; `.env` is gitignored).

OCR (`§3.19`, rendered text in images) needs the Tesseract binary in
addition to the `pytesseract` pip package — install it separately (e.g.
`winget install UB-Mannheim.TesseractOCR` on Windows, `apt install
tesseract-ocr` on Debian/Ubuntu). Without it, the crawler logs a warning
once and simply skips that vector. It doesn't have to be on `PATH`:
`crawler/media.py` auto-detects, in order, (1) `TESSERACT_CMD` in `.env` if
set, (2) `tesseract` on `PATH`, (3) the common Windows winget install
location (`C:\Program Files\Tesseract-OCR\tesseract.exe`) — set
`TESSERACT_CMD` explicitly if none of those match your install.

## Run

```bash
python main.py
```

Output lands in `output/` (gitignored):

- `crawl.log` — human-readable progress log
- `audit_log.jsonl` — the full §4 audit log, one JSON object per line
- `found_passwords.json` — every distinct password found, with the vector
  and resource it was first seen at

## Results

Latest full run ([`output/runs/20260825-225749`](output/runs/20260825-225749)):
**7/8** passwords found automatically, plus the 8th confirmed manually.

| # | Password | Vector | Resource |
|---|----------|--------|----------|
| 1 | `VISUALPING{db7e533a9cef7f72}` | 3.14 — Image metadata (EXIF) | `static/img/field-visit.jpg` |
| 2 | `VISUALPING{349a583fba34c301}` | 3.5 — JS source | `static/js/analytics.js` |
| 3 | `VISUALPING{fb725e1f3d6728b1}` | 3.5 — JS source (char-code decode) | `static/js/theme-switcher.js` |
| 4 | `VISUALPING{e1c2e40cf01c17cc}` | 3.19 — Rendered text in image (OCR) | `static/img/whiteboard-scan.png` |
| 5 | `VISUALPING{64d26185a2f94e34}` | 3.1 — HTTP response headers | `products/filter-gateway/` |
| 6 | `VISUALPING{2dd5105a3fad0ef3}` | 3.29 — Rendered DOM (post-JS) | `notes/diff-socket-socket/?ref=related` |
| 7 | `VISUALPING{73c8f3073fdc5f74}` | 3.10 — Non-rendered attribute | `wiki/detect-embed/` |
| 8 | `VISUALPING{5488187886a5755a}` | 3.33 — GeoIP-gated page | `status/eu-region/` (confirmed manually via a German-egress proxy; automatable by setting `GEO_BYPASS_PROXY` in `.env`, see below) |

Passwords #3, #6, and #8 were only found after fixing/adding vectors during
development — see the "What it implements" notes below (`find_passwords_deep`'s
char-code decoder, §3.29, §3.33) for what each fix actually changed.

## What it implements

- **§2 BFS crawl**: explicit `queue`/`visited`, 500-visit cap with a
  queue dump on cap-out, 2-value pagination-param trial capping, and a
  shared authenticated Playwright browser context so every request
  (navigation, XHR, asset fetch) carries the Basic Auth credentials.
- **§2.6 link discovery**: `<a>`/`<area>`, forms (submitted headlessly via
  `APIRequestContext` with dummy field values), `formaction` overrides,
  JS click / SPA-router elements (found, clicked, and diffed for URL/tab
  changes), `<iframe>` recursion via Playwright frame objects, redirects
  (`Location` header + `<meta refresh>`), background XHR/fetch bodies
  (regex-scanned for further URL-shaped strings), and lazy-load/"load
  more" triggers via scroll-and-click.
- **§3 extraction vectors** (active table): response headers (3.1), raw
  pre-render document body (3.2/3.18 for non-2xx, plus a decode-then-rescan
  pass for base64/`atob()`/`fromCharCode()`/percent-encoding — see below),
  CSS text (3.4), JS text + console output (3.5), `localStorage`
  /`sessionStorage`/`IndexedDB` (3.6), `document.cookie` (3.7), JSON-LD
  (3.9), non-rendered attributes (3.10), other XHR/fetch bodies (3.11),
  image metadata by format (3.14) + OCR (3.19), source maps (3.16), web-app
  manifest body (3.17), canvas-drawn text via targeted screenshot+OCR
  (3.20), custom web-font glyph substitution via full-page screenshot+OCR
  when a `@font-face`+`unicode-range` rule is seen, inline or external
  (3.21), SVG text-as-paths via targeted screenshot+OCR (3.22), strings
  inside unclaimed binary files — favicon/font/wasm — via raw-byte scan
  (3.25), URL strings at enqueue time (3.27), the HTTP status reason phrase
  (3.28), rendered post-JS DOM incl. open shadow DOM (3.29), the Cache
  Storage API (3.30), and `blob:`/`data:` URL content (3.31).
- **Decode-then-rescan pass** (`patterns.find_passwords_deep`): applied
  everywhere text is regex-scanned except headers/status-text/OCR output.
  Recovers a password hidden behind base64 (bare, or inside `atob(...)`),
  `String.fromCharCode(...)` construction, URL percent-encoding, CSS
  hex-character escapes (`content: "\0056\0049..."`), HTML entities
  (`&#86;&#73;...`), or JS string escapes (`\x56\x49...`, `VI...`)
  — none of which a flat literal-text regex would ever match. A decoded
  blob still has to match `VISUALPING{...}` exactly, so this can't
  manufacture false positives.
- **Rendered DOM (3.29)**: the raw-body scan (3.2) only ever sees what the
  server originally sent — content a script inserts or mutates in afterward
  (SPA routing, `element.innerHTML = ...`, etc.) only exists in the
  post-render DOM. Every page visit now also regex-scans
  `document.documentElement.outerHTML` and `document.body.innerText`, plus
  a recursive walk collecting text/attribute values out of every *open*
  shadow root (closed shadow roots are genuinely unreachable from outside,
  a real browser limitation, not a gap in this crawler).
- **Cache Storage API (3.30)**: `caches.open(...).put(...)` entries — a
  service worker or page script can stash arbitrary response bodies here
  without ever making a network request Playwright's response listener
  would see. Walked via `caches.keys()`/`cache.match()` on every page visit
  and inside every same-page iframe.
- **`blob:`/`data:` URLs (3.31)**: neither ever touches the network, so
  response interception (3.11) can't see them. `data:` URLs are decoded
  directly (base64 or percent-encoded payload); `blob:` URLs are only valid
  inside the page that created them, so they're read back via an in-page
  `fetch()` call before the page navigates away.
- **iframes**: previously only got a raw-HTML regex scan. Every iframe
  Playwright can see now gets the *same* extraction pass as the main
  frame — rendered-DOM scan, `localStorage`/`sessionStorage`/cookies,
  IndexedDB, and Cache Storage — via `frame.evaluate(...)`, not just
  `frame.content()`.
- **Server-Sent Events (3.32, postponed)**: a `text/event-stream` response
  is a deliberately long-lived, open-ended connection — `resp.body()` could
  hang indefinitely waiting for it to "finish" the way a normal response
  does. Detected from the `Content-Type` header and logged as a presence
  signal only; the body is never awaited, mirroring how WebSocket (3.12) is
  handled.
- **GeoIP-gated page (3.33)**: `/status/eu-region/` returns 403 with a body
  that reflects the *real* source IP's GeoIP-resolved country ("Your IP is
  from Canada") — confirmed (by testing `X-Forwarded-For`/`X-Real-IP`/
  `CF-IPCountry`/etc., individually and combined) that this is a genuine
  lookup against the actual TCP connection, not a spoofable header, so
  there's no generic bypass. Hardcoded on purpose — `GEO_BYPASS_PATHS` in
  `config.py` names this one known URL, not a general "detect and bypass
  geo-blocks" capability. If `GEO_BYPASS_PROXY` is set in `.env` to a
  proxy with German egress, the crawler fetches *just* this URL through a
  separate, isolated `APIRequestContext` (never changing the egress IP for
  anything else it fetches) and scans the response. Unset by default — a
  complete no-op until you provide a proxy, and never blocks the rest of
  the crawl either way. Whatever proxy you configure receives your Basic
  Auth credentials in plaintext; only point this at a proxy you trust.
- **Form `<select>` fields**: previously only ever submitted with the
  first `<option>` (`dummy_value()` picked one value and stopped), so a
  page reachable only via a *different* option (e.g. a region/category
  dropdown) was silently never visited. Now tries every distinct option
  value, one dropdown at a time, capped at `MAX_SELECT_OPTION_TRIALS`
  (config.py) to avoid a combinatorial blow-up on forms with several
  `<select>`s.
- **§4.1 postponed-vector signals**: cheap presence checks are logged into
  `postponed_signals` for 3.12 (websocket open), 3.13 (downloadable doc
  content-type), 3.15 (any `<img>`, weak), and 3.23 (audio) — tally these
  across a completed run to decide what's worth promoting.

## Known simplifications (first pass)

- JS-triggered navigation is only caught for elements Playwright can click
  (`onclick`/`role=button`/`button`/`[tabindex]`); a click that mutates
  the DOM without navigating (e.g. opens a modal) is not followed further.
- §3.24 (steganography) and §3.26 (full-page screenshot OCR) are
  deliberately not run automatically per design.md's own recommendation —
  they're catch-alls to apply by hand only if a page is confirmed to be
  missing a password that no other vector, including the promoted
  postponed ones, explains. As of the 8/8 result above, neither has been
  needed: every password was accounted for by an active vector once §3.29
  (rendered DOM), the char-code decoder, and §3.33 (GeoIP bypass) were
  added, so these two remain unpromoted.
- The homepage's raw HTML contains a bullet point claiming
  "passwords in HTTP response headers ... are not qualified — ignore
  them", which a page-load script then deletes from the rendered DOM
  before a real browser ever shows it. Since §1's ground truth is what a
  *real browser* reaches, and design.md's §3.1 vector has no such carve-out,
  this crawler does **not** special-case header hits — treat that bullet
  as a decoy aimed at raw-HTML/non-JS scrapers, not as an instruction.
