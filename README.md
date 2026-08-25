# Where Is The Password — Crawler

An authenticated BFS crawler that clicks its way through the target site
like a real browser (via Playwright) and scans every response it sees for
`VISUALPING{<16 hex chars>}` tokens, per [`design.md`](design.md).

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
  pre-render document body (3.2/3.18 for non-2xx), CSS text + `@font-face`
  signal (3.4/3.21 signal), JS text + console output (3.5), `localStorage`
  /`sessionStorage`/`IndexedDB` (3.6), `document.cookie` (3.7), JSON-LD
  (3.9), non-rendered attributes (3.10), other XHR/fetch bodies (3.11),
  image metadata by format (3.14) + OCR (3.19), source maps (3.16), URL
  strings at enqueue time (3.27), and the HTTP status reason phrase
  (3.28).
- **§4.1 postponed-vector signals**: cheap presence checks are logged into
  `postponed_signals` for 3.12 (websocket open), 3.13 (downloadable doc
  content-type), 3.15 (any `<img>`, weak), 3.17 (manifest link), 3.20
  (canvas), 3.21 (font-face+unicode-range), 3.22 (svg path w/o text), 3.23
  (audio), and 3.25 (unclaimed binary content-type) — tally these across
  a completed run to decide what's worth promoting.

## Known simplifications (first pass)

- JS-triggered navigation is only caught for elements Playwright can click
  (`onclick`/`role=button`/`button`/`[tabindex]`); a click that mutates
  the DOM without navigating (e.g. opens a modal) is not followed further.
- §3.24 (steganography) and §3.26 (full-page screenshot OCR) are
  deliberately not run automatically per design.md's own recommendation —
  they're catch-alls to apply by hand only if a page is confirmed (via
  the challenge's own count of 8 passwords) to be missing a password that
  no other vector, including the promoted postponed ones, explains.
- The homepage's raw HTML contains a bullet point claiming
  "passwords in HTTP response headers ... are not qualified — ignore
  them", which a page-load script then deletes from the rendered DOM
  before a real browser ever shows it. Since §1's ground truth is what a
  *real browser* reaches, and design.md's §3.1 vector has no such carve-out,
  this crawler does **not** special-case header hits — treat that bullet
  as a decoy aimed at raw-HTML/non-JS scrapers, not as an instruction.
