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


| Source                                       | Where to look                                                                                                                                        | How to extract                                                                                                                     |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `<a href>`                                   | HTML body                                                                                                                                            | Static HTML parse of the rendered DOM                                                                                              |
| Forms                                        | `<form action>` + its `<input>`/`<select>` names                                                                                                     | Parse`action`/`method`; submit with each declared field populated (dummy/known values) and capture the resulting URL               |
| JS click / SPA router navigation             | Elements with`onclick`, `role="button"`, or JS `addEventListener('click', …)` that call `history.pushState`/`router.push` instead of setting `href` | Playwright: click each interactive element, diff`page.url()` before/after, and watch for `history.pushState`/`popstate` events     |
| `<iframe src>`                               | Embedded frames                                                                                                                                      | Parse`src` attribute; recurse into frame's own DOM via Playwright's `frame` objects                                                |
| Redirects                                    | 3xx`Location` header, `<meta http-equiv="refresh">`, JS `location.href`/`location.assign()`/`window.open()`                                          | Follow via Playwright navigation; also inspect raw response headers and HTML/JS source for the redirect target before following it |
| Background XHR /`fetch()` calls              | Requests fired on page load or on interaction (API endpoints, often returning JSON with further links/IDs)                                           | Playwright network interception (`page.on('request'/'response')`); parse JSON bodies for URL-shaped strings                        |
| `<link>` tags                                | `rel="stylesheet"`, `alternate`, `prefetch`, `canonical`, `next`/`prev`, `manifest`                                                                  | Parse`href` attribute of every `<link>` in `<head>`; for `rel="manifest"`, also fetch and parse the JSON (e.g. `start_url`)        |
| `<button formaction>` / `<input formaction>` | Submit buttons/inputs that override their parent`<form action>`                                                                                      | Parse`formaction` attribute on submit controls inside `<form>`; submit via that control specifically                               |
| Dynamically rendered / lazy-loaded content   | Infinite scroll, "load more" buttons,`IntersectionObserver`-triggered fetches                                                                        | Playwright: scroll to bottom / click "load more" and re-scan DOM after each trigger, until no new elements appear                  |

#### Postponed — future development

Recognized as valid discovery sources but deprioritized for the first
implementation pass. Revisit once the sources above are working.


| Source                                                | Where to look                                                                       | How to extract                                                                                                                                  |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `<script src>`                                        | External JS bundles                                                                 | Parse`src` attribute — fetch and scan file (also feeds §3.5 password scan); string-literal URLs inside the JS itself are a secondary source   |
| CSS`url()` / `@import`                                | Inline`<style>`, linked `.css` files                                                | Regex/parse`url(...)`/`@import` for background images, fonts, imported stylesheets                                                              |
| `<img>`/`<source>`/`<video>`/`<audio>` `src`/`srcset` | Media elements                                                                      | Parse`src`/`srcset` attributes — not navigable pages, but resources to fetch and scan (§3)                                                    |
| HTTP`Link` response header                            | Raw response headers (RFC 8288,`rel="next"`, `canonical`, etc.)                     | Parse`Link:` header on every response, not just the HTML body                                                                                   |
| Shadow DOM content                                    | Web components (`<template shadowroot>`, custom elements)                           | Playwright's`page.locator()` pierces open shadow roots by default — make sure the DOM scan doesn't skip them                                   |
| `<area href>`                                         | Image-map links                                                                     | Parse`<map><area>` elements — same handling as `<a href>`                                                                                      |
| `<object data>` / `<embed src>`                       | Embedded documents (PDF, SVG, etc.)                                                 | Parse`data`/`src` attribute                                                                                                                     |
| Service worker precache manifest                      | SW registration + the service worker script's own cache-addAll/precache list        | Detect`navigator.serviceWorker.register(...)`; fetch the SW script and parse its precache list — can enumerate the whole route set in one shot |
| Hover/focus-revealed menus                            | Dropdown items inserted into the DOM only on`:hover`/`:focus` (not just CSS-hidden) | Playwright: hover/focus each nav trigger and re-scan DOM for newly-inserted elements                                                            |
| Tab / accordion / modal-revealed content              | Content (and its links) that only exists in the DOM after a click                   | Playwright: click each tab/accordion header/modal trigger and re-scan DOM after each                                                            |
| `postMessage`-driven cross-frame navigation           | Parent/iframe coordinating via`postMessage` instead of a `src` change               | Playwright: listen for`postMessage` events and track resulting frame/URL changes                                                                |

## 3. Password Extraction Vectors

Where in the server's response a `VISUALPING{...}` token might live.
`2.1`–`2.4` are the known baseline categories; specifics need confirmation.


| # | Vector | Where | Detection method |
|---|---|---|---|
| 3.1 | HTTP response — headers | Response headers (incl. non-standard/custom ones, `Set-Cookie`) | Case-insensitive regex scan of every header name+value, on **every** response (not just page navigations) |
| 3.2 | HTTP response — body (page navigation) | Raw body of the top-level document response only, per URL visited (pre-render, not just DOM) | Regex scan the raw bytes/text of the main navigation response before the browser parses/renders it — catches tokens that never make it into the DOM |
| 3.3 | HTML | Comments, `data-*` attributes, hidden inputs/elements, `<meta>` tags | Parse the full HTML document (not `innerText`); regex scan comments, all attribute values, and hidden (`display:none`/`type=hidden`) elements |
| 3.4 | CSS | Inline `<style>`, linked `.css` files, `content:` properties, custom properties, comments | Fetch and regex scan every stylesheet's raw text. **Caveat:** a password can be assembled from several `::before`/`::after` rules (one char per selector) or hex-escaped (`content: "\0041"`), so it may render as one string without ever appearing contiguous in the raw CSS — raw-text regex alone can miss it; this case depends on the render+OCR fallback (3.26) |
| 3.5 | JS | Inline `<script>`, linked `.js` files, string literals, comments, `console.log` output | Fetch and regex scan raw JS source; capture console messages via Playwright's `page.on('console')`. **Caveat:** this only catches literal source tokens — a password built at runtime (`String.fromCharCode(...)`, `atob(...)`, concatenation/XOR) won't match; fall back to diffing `window`'s own keys before/after load, or rely on 3.6/3.8 catching the constructed value once it lands in storage/state |
| 3.6 | Client-side storage | `localStorage`, `sessionStorage`, `IndexedDB` — set by JS after load, invisible to any HTTP-level capture | `page.evaluate()` to dump `localStorage`/`sessionStorage` (trivial, synchronous); regex scan the dump. **`IndexedDB` is meaningfully more work** — async: enumerate `indexedDB.databases()`, open each, walk every object store via a cursor |
| 3.7 | Cookies (JS-visible) | `document.cookie` — can differ from the `Set-Cookie` header if JS sets/rotates cookies client-side | `page.evaluate(() => document.cookie)` after load, in addition to header inspection in 3.1 |
| 3.8 | Embedded JSON state blobs | Framework hydration data — `window.__INITIAL_STATE__`, `<script type="application/json">` (e.g. Next.js `__NEXT_DATA__`), Redux/GraphQL cache dumps | Parse `<script type="application/json">` contents and known global-variable names via `page.evaluate`; regex scan the JSON text |
| 3.9 | Structured data | `<script type="application/ld+json">` (schema.org markup) | Parse and regex scan JSON-LD blocks |
| 3.10 | Non-rendered text attributes | `alt`, `title`, `aria-*`, `placeholder` — present in DOM/accessibility tree but not visible rendered text | Regex scan these specific attributes across all elements, not just `innerText` |
| 3.11 | Background XHR/fetch responses | JSON/text bodies of API calls fired during page load or interaction, **excluding** asset types already claimed by their own vector (JS 3.5, CSS 3.4, images 3.14, source maps 3.16, manifest 3.17) | Playwright `page.on('response')`; regex scan any response body not already covered by a dedicated vector row, keyed by originating request |
| 3.14 | Image metadata | EXIF fields in `<img>`/downloaded image files | Fetch images, parse EXIF via `Pillow`/`exifread`, regex scan text fields. **IPTC/XMP fields need a different library** (e.g. `iptcinfo3`, `pyexiv2`) — `Pillow`/`exifread` don't read them; add that dependency if we want full coverage, otherwise scope this row to EXIF only |
| 3.16 | Source map files | `//# sourceMappingURL=` comment at the end of a `.js`/`.css` file **or** the `SourceMap`/`X-SourceMap` HTTP response header, either of which can point to a `.map` file that embeds original unminified source | Check both the trailing comment and the response headers for the map URL; fetch the `.map` file and regex scan its `sourcesContent` |
| 3.17 | Web app manifest | `manifest.json` linked via `<link rel="manifest">` (see §2.6) | Fetch and regex scan the manifest JSON body itself, not just the URLs it lists |
| 3.18 | Non-200 / error pages | Custom 4xx/5xx error page bodies — easy to skip if the crawler treats non-2xx as "nothing here" | Scan error response bodies the same as 3.2 instead of discarding on non-2xx status |
| 3.19 | Rendered text in an image | Banner/badge PNG/JPG (`<img>` or background-image assets) — password drawn as pixels, not characters | Fetch image, run OCR (`pytesseract`) with a character whitelist (`VISUALPING{}0-9a-fA-F`) and image preprocessing (upscale/threshold) to reduce `0`/`O`/`1`/`l`/`I` misreads, regex scan the extracted text |

> **#JOBS** — triage the remaining untriaged vectors above: confirm the
> expected **format** and whether the regex needs to run against raw bytes
> vs. rendered DOM vs. decoded content (e.g. base64, JSON-escaped strings).

#### Postponed — future development

Recognized as valid extraction vectors but deprioritized for the first
implementation pass. Revisit once the vectors above are working.

| # | Vector | Where | Detection method |
|---|---|---|---|
| 3.12 | WebSocket messages | Frames sent/received over `ws://`/`wss://` connections | Playwright `page.on('websocket')` + frame-received/sent listeners; regex scan payload text |
| 3.13 | Downloadable files | Linked non-HTML documents (PDF, TXT, CSV, DOCX, ZIP) | Fetch and extract text and metadata (e.g. PDF `Author`/`Producer` fields); regex scan extracted content |
| 3.15 | QR codes / encoded images | Data visually encoded in an image rather than stored as text | Decode QR/barcodes (e.g. `pyzbar`) from fetched images; regex scan decoded text |
| 3.20 | Canvas-drawn text | `<canvas>` elements (`fillText`/`strokeText`, no DOM text node) | Screenshot the canvas region and OCR it, or instrument/intercept `fillText` calls via `page.evaluate` |
| 3.21 | Custom web-font glyph substitution | `@font-face` with remapped `unicode-range` — copied/parsed DOM text ≠ what's visually rendered | Screenshot + OCR the element and compare against its raw `textContent`; trust OCR when they diverge |
| 3.22 | SVG text-as-paths | `<path>` shapes forming letters, no real `<text>` node | Screenshot + OCR, since there's no text node to parse |
| 3.23 | Audio-encoded password | `<audio src>` or linked audio files (spoken TTS, Morse/DTMF tones) | Speech-to-text (e.g. `speech_recognition`) or tone/Morse decode; regex scan the transcript |
| 3.24 | Steganography in image pixel data | Any fetched image (LSB-style hidden payload) | Run steg-extraction (e.g. `stegano`, `zsteg`) as a fallback on images that don't otherwise yield a hit |
| 3.25 | Strings inside binary files | favicon.ico, font files, `.wasm` modules — anything fetched regardless of declared Content-Type | `strings`-style printable-ASCII scan over the raw bytes of every fetched resource, not filtered by content-type |
| 3.26 | Full-page screenshot + OCR | Rendered page, after each page settles | Catch-all fallback pass — screenshot the viewport and OCR it, to catch visual-only text not covered by other vectors |

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
    interception (3.1–3.3, 3.6–3.11, 3.17, 3.18), screenshots (3.19)
  - `python-dotenv` — loads credentials from `.env`, keeps them out of git
  - `Pillow` — image decoding, EXIF metadata (3.14), preprocessing
    (upscale/threshold) before OCR (3.19)
  - `pytesseract` — OCR for rendered text in images (3.19); requires the
    Tesseract OCR binary installed on the host (not a pip package)
  - stdlib `re`/`json` cover regex scanning and JSON parsing (3.2–3.11,
    3.17) — no extra dependency needed

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
