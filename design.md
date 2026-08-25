# Design: Password Crawler

## 1. Objective

Crawl `TARGET_URL` (see `.env`) as an authenticated user (`AUTH_USERNAME` /
`AUTH_PASSWORD`, see `.env`, not committed to git) and discover every
password on the site matching the format:

```
VISUALPING{<16 hex chars>}
```

Canonical match pattern: `VISUALPING\{[0-9a-fA-F]{16}\}` — **case-insensitive**
until we've confirmed from real captured samples that the target only ever
emits lowercase hex. Every vector in §3 should use this same pattern.

**Ground truth constraints:**

- Every password is reachable from the homepage by a real browser clicking
  through the site — nothing is hidden behind guessed URLs, wordlists, or
  `robots.txt` tricks.
- Not every path a browser follows is an `<a href>` in the HTML source.
- Not every password is visible text in an HTML page, and not every
  password is stored where you'd expect at first glance. Pages reference
  other kinds of resources — inspect everything the server returns, not
  just the rendered page.

## 2. URL Discovery (BFS Crawl)


| #   | Rule                                                                                                                                        |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 2.1 | Traverse with**breadth-first search**; maintain an explicit `queue` and `visited` set.                                                      |
| 2.2 | Cap total visits at**500** to avoid unbounded/infinite crawling.                                                                            |
| 2.3 | On hitting the cap,**dump the remaining queue** (unvisited URLs) to the log so the limit can be tuned.                                      |
| 2.4 | For URLs with pagination-style params (e.g.`page`), try only **2 trial values** (e.g. `page=1`, `page=2`) rather than exhausting the range. |
| 2.5 | Every request (page loads, XHR/fetch, asset fetches) must carry the authenticated session from`.env`.                                       |

### 2.6 Link-discovery sources

How we find new URLs to enqueue. `<a>` tags are the known baseline; the
rest are the mainstream ways a real (Playwright) browser can reach a page
that a plain HTML/`<a>` parser would miss.

**`<a href>`**
- Where to look: HTML body
- How to extract: Static HTML parse of the rendered DOM

**Forms**
- Where to look: `<form action>` + its `<input>`/`<select>` names
- How to extract: Parse `action`/`method`; submit with each declared field populated (dummy/known values) and capture the resulting URL

**JS click / SPA router navigation**
- Where to look: Elements with `onclick`, `role="button"`, or JS `addEventListener('click', …)` that call `history.pushState`/`router.push` instead of setting `href`
- How to extract: Playwright: click each interactive element, diff `page.url()` before/after, and watch for `history.pushState`/`popstate` events

**`<iframe src>`**
- Where to look: Embedded frames
- How to extract: Parse `src` attribute; recurse into frame's own DOM via Playwright's `frame` objects

**Redirects**
- Where to look: 3xx `Location` header, `<meta http-equiv="refresh">`, JS `location.href`/`location.assign()`/`window.open()`
- How to extract: Follow via Playwright navigation; also inspect raw response headers and HTML/JS source for the redirect target before following it

**Background XHR / `fetch()` calls**
- Where to look: Requests fired on page load or on interaction (API endpoints, often returning JSON with further links/IDs)
- How to extract: Playwright network interception (`page.on('request'/'response')`); parse JSON bodies for URL-shaped strings

**`<button formaction>` / `<input formaction>`**
- Where to look: Submit buttons/inputs that override their parent `<form action>`
- How to extract: Parse `formaction` attribute on submit controls inside `<form>`; submit via that control specifically

**Dynamically rendered / lazy-loaded content**
- Where to look: Infinite scroll, "load more" buttons, `IntersectionObserver`-triggered fetches
- How to extract: Playwright: scroll to bottom / click "load more" and re-scan DOM after each trigger, until no new elements appear

#### Postponed — future development

Recognized as valid discovery sources but deprioritized for the first
implementation pass. Revisit once the sources above are working.

**`<script src>`**
- Where to look: External JS bundles
- How to extract: Parse `src` attribute — fetch and scan file (also feeds §3.5 password scan); string-literal URLs inside the JS itself are a secondary source

**CSS `url()` / `@import`**
- Where to look: Inline `<style>`, linked `.css` files
- How to extract: Regex/parse `url(...)`/`@import` for background images, fonts, imported stylesheets

**`<img>`/`<source>`/`<video>`/`<audio>` `src`/`srcset`**
- Where to look: Media elements
- How to extract: Parse `src`/`srcset` attributes — not navigable pages, but resources to fetch and scan (§3)

**`<link>` tags**
- Where to look: `rel="stylesheet"`, `alternate`, `prefetch`, `canonical`, `next`/`prev`, `manifest`
- How to extract: Parse `href` attribute of every `<link>` in `<head>`; for `rel="manifest"`, also fetch and parse the JSON (e.g. `start_url`, feeds 3.17)

**HTTP `Link` response header**
- Where to look: Raw response headers (RFC 8288, `rel="next"`, `canonical`, etc.)
- How to extract: Parse `Link:` header on every response, not just the HTML body

**Shadow DOM content**
- Where to look: Web components (`<template shadowroot>`, custom elements)
- How to extract: Playwright's `page.locator()` pierces open shadow roots by default — make sure the DOM scan doesn't skip them

**`<area href>`**
- Where to look: Image-map links
- How to extract: Parse `<map><area>` elements — same handling as `<a href>`

**`<object data>` / `<embed src>`**
- Where to look: Embedded documents (PDF, SVG, etc.)
- How to extract: Parse `data`/`src` attribute

**Service worker precache manifest**
- Where to look: SW registration + the service worker script's own cache-addAll/precache list
- How to extract: Detect `navigator.serviceWorker.register(...)`; fetch the SW script and parse its precache list — can enumerate the whole route set in one shot

**Hover/focus-revealed menus**
- Where to look: Dropdown items inserted into the DOM only on `:hover`/`:focus` (not just CSS-hidden)
- How to extract: Playwright: hover/focus each nav trigger and re-scan DOM for newly-inserted elements

**Tab / accordion / modal-revealed content**
- Where to look: Content (and its links) that only exists in the DOM after a click
- How to extract: Playwright: click each tab/accordion header/modal trigger and re-scan DOM after each

**`postMessage`-driven cross-frame navigation**
- Where to look: Parent/iframe coordinating via `postMessage` instead of a `src` change
- How to extract: Playwright: listen for `postMessage` events and track resulting frame/URL changes

**Hash-based (`#!/path`) routing**
- Where to look: URL fragment identifier — older SPA routers (Angular 1.x-style, Backbone) that route on `location.hash` instead of `history.pushState`
- How to extract: Listen for `hashchange` events in addition to the `popstate` listener already planned for the pushState case

**Non-click interactive triggers**
- Where to look: `<select onchange>` (locale/nav dropdowns), `<form onsubmit>`, keypress-triggered nav (Enter in a search box)
- How to extract: Generalize the JS click row — trigger `change`/`submit`/`keydown` events too, not just `click`

## 3. Password Extraction Vectors

Where in the server's response a `VISUALPING{...}` token might live.
`2.1`–`2.4` are the known baseline categories; specifics need confirmation.

**3.1 — HTTP response — headers**
- Where: Response headers (incl. non-standard/custom ones, `Set-Cookie`)
- Detection method: Case-insensitive regex scan of every header name+value, on **every** response (not just page navigations)

**3.2 — HTTP response — body (page navigation)**
- Where: Raw body of the top-level document response only, per URL visited (pre-render, not just DOM)
- Detection method: Regex scan the raw bytes/text of the main navigation response before the browser parses/renders it — catches tokens that never make it into the DOM

**3.3 — HTML**
- Where: Comments, `data-*` attributes, hidden inputs/elements, `<meta>` tags
- Detection method: Parse the full HTML document (not `innerText`); regex scan comments, all attribute values, and hidden (`display:none`/`type=hidden`) elements

**3.4 — CSS**
- Where: Inline `<style>`, linked `.css` files, `content:` properties, custom properties, comments
- Detection method: Fetch and regex scan every stylesheet's raw text. **Caveat:** a password can be assembled from several `::before`/`::after` rules (one char per selector) or hex-escaped (`content: "\0041"`), so it may render as one string without ever appearing contiguous in the raw CSS — raw-text regex alone can miss it; this case depends on the render+OCR fallback (3.26)

**3.5 — JS**
- Where: Inline `<script>`, linked `.js` files, string literals, comments, `console.log` output
- Detection method: Fetch and regex scan raw JS source; capture console messages via Playwright's `page.on('console')`. **Caveat:** this only catches literal source tokens — a password built at runtime (`String.fromCharCode(...)`, `atob(...)`, concatenation/XOR) won't match; fall back to diffing `window`'s own keys before/after load, or rely on 3.6 (active) / 3.8 (postponed) catching the constructed value once it lands in storage/state

**3.6 — Client-side storage**
- Where: `localStorage`, `sessionStorage`, `IndexedDB` — set by JS after load, invisible to any HTTP-level capture
- Detection method: `page.evaluate()` to dump `localStorage`/`sessionStorage` (trivial, synchronous); regex scan the dump. **`IndexedDB` is meaningfully more work** — async: enumerate `indexedDB.databases()`, open each, walk every object store via a cursor

**3.7 — Cookies (JS-visible)**
- Where: `document.cookie` — can differ from the `Set-Cookie` header if JS sets/rotates cookies client-side
- Detection method: `page.evaluate(() => document.cookie)` after load, in addition to header inspection in 3.1

**3.9 — Structured data**
- Where: `<script type="application/ld+json">` (schema.org markup)
- Detection method: Parse and regex scan JSON-LD blocks

**3.10 — Non-rendered text attributes**
- Where: `alt`, `title`, `aria-*`, `placeholder` — present in DOM/accessibility tree but not visible rendered text
- Detection method: Regex scan these specific attributes across all elements, not just `innerText`

**3.11 — Background XHR/fetch responses**
- Where: JSON/text bodies of API calls fired during page load or interaction, **excluding** asset types already claimed by their own vector (JS 3.5, CSS 3.4, images 3.14, source maps 3.16)
- Detection method: Playwright `page.on('response')`; regex scan any response body not already covered by a dedicated vector row, keyed by originating request

**3.14 — Image metadata**
- Where: Format-specific embedded text fields in `<img>`/downloaded image files — see breakdown below, metadata layout differs per file format
- Detection method: See breakdown below

**3.16 — Source map files**
- Where: `//# sourceMappingURL=` comment at the end of a `.js`/`.css` file **or** the `SourceMap`/`X-SourceMap` HTTP response header, either of which can point to a `.map` file that embeds original unminified source
- Detection method: Check both the trailing comment and the response headers for the map URL; fetch the `.map` file and regex scan its `sourcesContent`

**3.18 — Non-200 / error pages**
- Where: Custom 4xx/5xx error page bodies — easy to skip if the crawler treats non-2xx as "nothing here"
- Detection method: Scan error response bodies the same as 3.2 instead of discarding on non-2xx status

**3.19 — Rendered text in an image**
- Where: Banner/badge PNG/JPG (`<img>` or background-image assets) — password drawn as pixels, not characters
- Detection method: Fetch image, run OCR (`pytesseract`) with a character whitelist (`VISUALPING{}0-9a-fA-F`) and image preprocessing (upscale/threshold) to reduce `0`/`O`/`1`/`l`/`I` misreads, regex scan the extracted text

**3.27 — URL string itself**
- Where: Path segments, query-string values, and fragment of every discovered URL (e.g. `/secret/VISUALPING{...}`, `?token=...`)
- Detection method: Regex scan the URL string at enqueue time, before even fetching it

**3.28 — HTTP status line reason phrase**
- Where: The custom text after the status code (`200 VISUALPING{...}` instead of `200 OK`) — servers can set this arbitrarily
- Detection method: Read `response.statusText()` on every response; regex scan it the same as headers (3.1)

##### 3.14 breakdown — image metadata by format

Metadata isn't stored the same way across image formats; each one needs
its own field(s) and, sometimes, its own accessor even within one library.

**JPEG — EXIF (APP1)**
- Metadata field(s): `UserComment`, `ImageDescription`, `Copyright`, `Artist`, `Software`, GPS tags
- Extraction method: `Pillow` `img.getexif()`; regex scan every string-valued tag

**JPEG — COM marker**
- Metadata field(s): Free-text comment, a **separate** segment from EXIF
- Extraction method: `Pillow` `img.info.get('comment')` — easy to miss if only `getexif()` is wired up

**JPEG — IPTC (APP13 Photoshop IRB)**
- Metadata field(s): `Caption`, `Keywords`, `Credit`
- Extraction method: `iptcinfo3` — `Pillow`/`exifread` don't read this

**JPEG / PNG / WebP — XMP**
- Metadata field(s): Arbitrary custom XML key-values, embedded as a literal XML text packet
- Extraction method: No special library needed — it's plain text in the file; a raw-bytes regex scan (3.25) over the file catches it directly

**PNG — `tEXt`/`zTXt`/`iTXt` chunks**
- Metadata field(s): Key-value text pairs (`Comment`, `Description`, `Software`, or arbitrary custom keys)
- Extraction method: `Pillow` `img.text` / `img.info` — different code path than EXIF

**PNG — `eXIf` chunk**
- Metadata field(s): PNG spec v1.2+, holds real EXIF data
- Extraction method: `Pillow` `img.getexif()` (Pillow ≥ 6.0)

**GIF — Comment Extension block**
- Metadata field(s): `0x21 0xFE`
- Extraction method: `Pillow` `img.info.get('comment')`

**WebP — EXIF chunk**
- Metadata field(s): RIFF `EXIF` chunk
- Extraction method: `Pillow` `img.getexif()`

**TIFF — native EXIF-style tags**
- Metadata field(s): Same tag system as JPEG EXIF
- Extraction method: `Pillow` `img.getexif()`

**BMP**
- Metadata field(s): No standard embedded-metadata mechanism
- Extraction method: N/A — skip

**SVG**
- Metadata field(s): `<title>`, `<desc>`, XML comments, custom attributes — it's already plain text/XML, not a binary format
- Extraction method: No image-specific tooling — treat as a text/XML file and run the same raw-text regex scan already used for HTML/CSS (3.3/3.4)

> **#JOBS** — triage the remaining untriaged vectors above: confirm the
> expected **format** and whether the regex needs to run against raw bytes
> vs. rendered DOM vs. decoded content (e.g. base64, JSON-escaped strings).

#### Postponed — future development

Recognized as valid extraction vectors but deprioritized for the first
implementation pass. Revisit once the vectors above are working.

**3.8 — Embedded JSON state blobs**
- Where: Framework hydration data — `window.__INITIAL_STATE__`, `<script type="application/json">` (e.g. Next.js `__NEXT_DATA__`), Redux/GraphQL cache dumps
- Detection method: Parse `<script type="application/json">` contents and known global-variable names via `page.evaluate`; regex scan the JSON text

**3.12 — WebSocket messages**
- Where: Frames sent/received over `ws://`/`wss://` connections
- Detection method: Playwright `page.on('websocket')` + frame-received/sent listeners; regex scan payload text

**3.13 — Downloadable files**
- Where: Linked non-HTML documents (PDF, TXT, CSV, DOCX, ZIP)
- Detection method: Fetch and extract text and metadata (e.g. PDF `Author`/`Producer` fields); regex scan extracted content

**3.15 — QR codes / encoded images**
- Where: Data visually encoded in an image rather than stored as text
- Detection method: Decode QR/barcodes (e.g. `pyzbar`) from fetched images; regex scan decoded text

**3.17 — Web app manifest**
- Where: `manifest.json` linked via `<link rel="manifest">` (see §2.6, also postponed)
- Detection method: Fetch and regex scan the manifest JSON body itself, not just the URLs it lists

**3.20 — Canvas-drawn text**
- Where: `<canvas>` elements (`fillText`/`strokeText`, no DOM text node)
- Detection method: Screenshot the canvas region and OCR it, or instrument/intercept `fillText` calls via `page.evaluate`

**3.21 — Custom web-font glyph substitution**
- Where: `@font-face` with remapped `unicode-range` — copied/parsed DOM text ≠ what's visually rendered
- Detection method: Screenshot + OCR the element and compare against its raw `textContent`; trust OCR when they diverge

**3.22 — SVG text-as-paths**
- Where: `<path>` shapes forming letters, no real `<text>` node
- Detection method: Screenshot + OCR, since there's no text node to parse

**3.23 — Audio-encoded password**
- Where: `<audio src>` or linked audio files (spoken TTS, Morse/DTMF tones)
- Detection method: Speech-to-text (e.g. `speech_recognition`) or tone/Morse decode; regex scan the transcript

**3.24 — Steganography in image pixel data**
- Where: Any fetched image (LSB-style hidden payload)
- Detection method: Run steg-extraction (e.g. `stegano`, `zsteg`) as a fallback on images that don't otherwise yield a hit

**3.25 — Strings inside binary files**
- Where: favicon.ico, font files, `.wasm` modules — anything fetched regardless of declared Content-Type
- Detection method: `strings`-style printable-ASCII scan over the raw bytes of every fetched resource, not filtered by content-type

**3.26 — Full-page screenshot + OCR**
- Where: Rendered page, after each page settles
- Detection method: Catch-all fallback pass — screenshot the viewport and OCR it, to catch visual-only text not covered by other vectors

## 4. Audit Log

Every visited resource is logged with:


| Field            | Description                                      |
| ---------------- | ------------------------------------------------ |
| `timestamp`      | When the resource was fetched                    |
| `resource`       | URL / asset fetched                              |
| `vector`         | Which extraction vector was checked (§3)        |
| `url_path`       | Path component                                   |
| `status`         | HTTP status code / outcome                       |
| `password_found` | Password string if a match was found, else empty |

## 5. Engineering Constraints

- **Language:** Python
- **Dependencies (active — needed for the current §3 vectors):**
  - `playwright` — browser automation: rendering, click-through nav, network
    interception (3.1–3.3, 3.6, 3.7, 3.9–3.11, 3.18), screenshots (3.19)
  - `python-dotenv` — loads credentials from `.env`, keeps them out of git
  - `Pillow` — image decoding, EXIF metadata (3.14), preprocessing
    (upscale/threshold) before OCR (3.19)
  - `pytesseract` — OCR for rendered text in images (3.19); requires the
    Tesseract OCR binary installed on the host (not a pip package)
  - stdlib `re`/`json` cover regex scanning and JSON parsing (3.2–3.7,
    3.9–3.11) — no extra dependency needed

- **Dependencies (postponed — only needed if/when the corresponding §3
  postponed vector is activated):**
  - `pyzbar` — QR/barcode decoding (3.15); requires the system `zbar`
    library (not a pip package)
  - `pypdf` or `pdfplumber` — text/metadata extraction from PDFs (3.13)
  - `python-docx` — text extraction from DOCX (3.13); stdlib `csv`/`zipfile`
    already cover CSV/ZIP, no new dependency
  - `SpeechRecognition` (+ a speech-to-text backend) — audio transcription
    (3.23)
  - `stegano` — LSB steganography extraction (3.24)
  - `iptcinfo3` or `pyexiv2` — IPTC/XMP metadata, only if full coverage on
    3.14 is needed beyond the EXIF that `Pillow`/`exifread` already give us

## 6. Open Items (JOBS)

- [ ]  §2.6 — enumerate and finalize all link-discovery sources beyond `<a>`
  (forms, JS click events, redirects, iframes, background XHR, etc.)
- [ ]  §3 — enumerate and finalize all password-extraction vectors, with
  exact locations, expected formats, and detection method per vector
