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
- How to extract: Parse `src` attribute; recurse into frame's own DOM via Playwright's `frame` objects. Recursion means the *full* §3 extraction pass, not just a raw-HTML regex scan — each frame gets its own rendered-DOM scan (§3.29), `localStorage`/`sessionStorage`/cookies (§3.6/§3.7), IndexedDB (§3.6), and Cache Storage (§3.30) walk via `frame.evaluate(...)`, since a frame's client-side state is its own and a main-frame-only scan would never see it

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
- Where: Comments, `data-*` attributes, hidden inputs/elements, `<meta>` tags — all literal text within the top-level HTML document
- Detection method: Already covered by §3.2's raw-body regex scan (comments/attributes/hidden elements are just more text in that same response). This row exists to label *where* in the document a hit came from for the §4 audit log, not as an independent scanning pass

**3.4 — CSS**
- Where: Inline `<style>`, linked `.css` files, `content:` properties, custom properties, comments
- Detection method: Fetch and regex scan every stylesheet's raw text. **Caveat:** a password can be assembled from several `::before`/`::after` rules (one char per selector) or hex-escaped (`content: "\0041"`), so it may render as one string without ever appearing contiguous in the raw CSS — raw-text regex alone can miss it; this case depends on the render+OCR fallback (3.26)

**3.5 — JS**
- Where: Inline `<script>`, linked `.js` files, string literals, comments, `console.log` output
- Detection method: Fetch and regex scan raw JS source; capture console messages via Playwright's `page.on('console')`. **Caveat:** this only catches literal source tokens — a password built at runtime (`String.fromCharCode(...)`, `atob(...)`, concatenation/XOR) won't match; fall back to diffing `window`'s own keys before/after load, or rely on 3.6 (active) / 3.8 (postponed) catching the constructed value once it lands in storage/state

**3.6 — Client-side storage**
- Where: `localStorage`, `sessionStorage`, `IndexedDB` — set by JS after load, invisible to any HTTP-level capture. Also folds in `window.name` and `history.state`: both are JS-writable global state that can carry a value across navigations without ever touching the usual storage buckets — cheap to grab in the same pass
- Detection method: `page.evaluate()` to dump `localStorage`/`sessionStorage`/`window.name`/`history.state` (trivial, synchronous); regex scan the dump. **`IndexedDB` is meaningfully more work** — async: enumerate `indexedDB.databases()`, open each, walk every object store via a cursor. Also see §3.30 (Cache Storage) — a related but distinct client-side storage API

**3.7 — Cookies (JS-visible)**
- Where: `document.cookie` — can differ from the `Set-Cookie` header if JS sets/rotates cookies client-side
- Detection method: `page.evaluate(() => document.cookie)` after load, in addition to header inspection in 3.1

**3.9 — Structured data**
- Where: `<script type="application/ld+json">` (schema.org markup) — inert data, not executed as JS, but still literal text within the top-level HTML document
- Detection method: Already covered by §3.2's raw-body regex scan (the JSON-LD text sits in that same response, whether or not it's parsed as JSON first). This row exists to label the hit as "found in structured data" for the §4 audit log, not as an independent scanning pass

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

**3.17 — Web app manifest**
- Where: `manifest.json` linked via `<link rel="manifest">` (see §2.6)
- Detection method: Fetch and regex scan the manifest JSON body itself, not just the URLs it lists

**3.20 — Canvas-drawn text**
- Where: `<canvas>` elements (`fillText`/`strokeText`, no DOM text node)
- Detection method: Tag every `<canvas>` in the DOM harvest, screenshot each one individually via its Playwright locator, and OCR the screenshot (same OCR pipeline as 3.19)

**3.21 — Custom web-font glyph substitution**
- Where: `@font-face` with remapped `unicode-range` — copied/parsed DOM text ≠ what's visually rendered — whether the rule is inline (`<style>`) or in a linked stylesheet
- Detection method: Cheap presence check (regex for `@font-face { ... unicode-range: ... }`) run against both inline `<style>` text and every fetched CSS file; when the condition is seen anywhere on the page, take a full-page screenshot and OCR it, regex-scanning the result. (Simplified from "compare OCR against `textContent` per element" to "OCR the whole page when the trigger condition fires" — cheaper, and the OCR pass only runs on pages that actually show the signal, not every page.)

**3.22 — SVG text-as-paths**
- Where: `<path>` shapes forming letters, no real `<text>` node
- Detection method: Tag every inline `<svg>` that has a `<path>` but no `<text>`/`<tspan>` descendant, screenshot each individually, and OCR the screenshot

**3.25 — Strings inside binary files**
- Where: favicon.ico, font files, `.wasm` modules, and anything else whose response `Content-Type` isn't claimed by another vector
- Detection method: Decode the raw response bytes as latin1 (1:1 byte↔codepoint, so embedded ASCII text survives regardless of surrounding binary data) and regex scan the result — equivalent to a `strings`-style scan without needing a separate run-extraction pass

**3.27 — URL string itself**
- Where: Path segments, query-string values, and fragment of every discovered URL (e.g. `/secret/VISUALPING{...}`, `?token=...`)
- Detection method: Regex scan the URL string at enqueue time, before even fetching it

**3.28 — HTTP status line reason phrase**
- Where: The custom text after the status code (`200 VISUALPING{...}` instead of `200 OK`) — servers can set this arbitrarily
- Detection method: Read `response.statusText()` on every response; regex scan it the same as headers (3.1)

#### Added after a follow-up code review (beyond the original numbering)

The rows below weren't in the original design — a later review of the
implementation identified real gaps that §3.1–3.28 don't cover: content
that only ever exists after JS has already run, client-side storage
mechanisms beyond `localStorage`/`sessionStorage`/IndexedDB, iframe
content that a main-frame-only scan would silently miss, and (§3.33) a
page gated behind a real access-control check rather than anything a
passive scan could ever observe.

**3.29 — Rendered DOM (post-JS), including open shadow DOM**
- Where: `document.documentElement.outerHTML` / `document.body.innerText` after the page settles — content a script inserted or mutated in (SPA routing, `element.innerHTML = ...`, `history.pushState` without a full navigation) never appears in §3.2's raw server response at all. Open shadow roots also don't serialize into either property and need their own walk; closed shadow roots are genuinely unreachable from outside the page, a real browser limitation rather than a gap here
- Detection method: `page.evaluate()` after load to grab `outerHTML`/`innerText`, plus a recursive walk collecting `textContent` and attribute values out of every open `shadowRoot` found anywhere in the tree; regex scan all of it the same as raw HTML

**3.30 — Cache Storage API**
- Where: `caches.open(name).then(c => c.put(request, response))` — a service worker or page script can stash an arbitrary response body here; reading it back (`cache.match()`) never makes a network request, so response interception (§3.11) never sees it, and it's a distinct API from IndexedDB (§3.6)
- Detection method: `caches.keys()` to enumerate every named cache, `cache.keys()` + `cache.match()` to walk every entry, `response.text()` to read the body; regex scan each

**3.31 — `blob:`/`data:` URL content**
- Where: `URL.createObjectURL(new Blob([...]))` assigned to a `src`/`href`, or a literal `data:` URI — neither is ever requested over the network, so §3.11's response interception can't see either one
- Detection method: `data:` URLs are decoded directly (base64 or percent-encoded payload, parsed from the string itself); `blob:` URLs are only valid inside the page that created them, so they're read back via an in-page `fetch(blobUrl).then(r => r.text())` call before the page navigates away

**3.32 — Server-Sent Events (postponed)**
- Where: An `EventSource`-driven `text/event-stream` connection — like WebSocket (§3.12), a deliberately long-lived, open-ended response
- Detection method: Signal only, same treatment as §3.12 — a normal `resp.body()` await could hang indefinitely waiting for a stream that's never meant to end, so the body is never read; presence is detected from the `Content-Type` header alone

**3.33 — GeoIP-gated page**
- Where: `/status/eu-region/` returns 403 with a body that names the visitor's *actual* resolved country ("Your IP is from Canada", "...from Germany", etc.) — confirmed by live-testing `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, `CF-IPCountry`, `X-Country`/`X-Country-Code`, `X-GeoIP-Country`, `X-AppEngine-Country`, `X-Client-Country`, `X-Forwarded-Country`, and `Accept-Language: de-DE`, individually and combined — every attempt still 403'd with the same "Canada" body, meaning this is a real GeoIP lookup against the actual TCP connection's source IP, not a header the app trusts. Confirmed genuine by routing the *same* request through a real German-egress proxy instead: 200, with the page body containing `VISUALPING{5488187886a5755a}` literally wrapped
- Detection method: Not a generic "detect and bypass any geo-block" capability — deliberately hardcoded to this one confirmed URL (`GEO_BYPASS_PATHS` in `config.py`), since a 403-with-region-flavored-text heuristic would misfire on unrelated sites. When `GEO_BYPASS_PROXY` is configured (`.env`, unset by default — a no-op otherwise), the crawler re-fetches *just* this URL through a separate, throwaway `APIRequestContext` configured with that proxy, kept fully isolated from the main browser context so the egress IP (and therefore the GeoIP result) never changes for anything else the crawl fetches
- Caveat: public proxies are ephemeral — a working one today may be dead tomorrow. This vector is inherently "confirm the proxy is still live before relying on it," not a permanent automated fix. Whatever proxy is configured receives the site's Basic Auth credentials in plaintext; only use one you trust

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

#### Postponed — future development

Recognized as valid extraction vectors but deprioritized for the first
implementation pass. Revisit once the vectors above are working.

**3.8 — Embedded JSON state blobs**
- Where: Framework hydration data — `window.__INITIAL_STATE__`, `<script type="application/json">` (e.g. Next.js `__NEXT_DATA__`), Redux/GraphQL cache dumps
- Detection method: If the blob is a literal assignment/tag in the top-level HTML (the common case), it's already covered by §3.2's raw-body scan — this row just labels the hit as "found in hydration state" for the §4 audit log. It only earns an *independent* check (`page.evaluate` reading `window.__INITIAL_STATE__` etc. after load) when the state is populated by a later XHR/fetch call rather than embedded at initial load — in which case it's really §3.11's job, not a raw-body regex

**3.12 — WebSocket messages**
- Where: Frames sent/received over `ws://`/`wss://` connections
- Detection method: Playwright `page.on('websocket')` + frame-received/sent listeners; regex scan payload text

**3.13 — Downloadable files**
- Where: Linked non-HTML documents (PDF, TXT, CSV, DOCX, ZIP)
- Detection method: Fetch and extract text and metadata (e.g. PDF `Author`/`Producer` fields); regex scan extracted content

**3.15 — QR codes / encoded images**
- Where: Data visually encoded in an image rather than stored as text
- Detection method: Decode QR/barcodes (e.g. `pyzbar`) from fetched images; regex scan decoded text

**3.23 — Audio-encoded password**
- Where: `<audio src>` or linked audio files (spoken TTS, Morse/DTMF tones)
- Detection method: Speech-to-text (e.g. `speech_recognition`) or tone/Morse decode; regex scan the transcript

**3.24 — Steganography in image pixel data**
- Where: Any fetched image (LSB-style hidden payload)
- Detection method: Run steg-extraction (e.g. `stegano`, `zsteg`) as a fallback on images that don't otherwise yield a hit

**3.26 — Full-page screenshot + OCR**
- Where: Rendered page, after each page settles
- Detection method: Catch-all fallback pass — screenshot the viewport and OCR it, to catch visual-only text not covered by other vectors

## 4. Audit Log

Every visited resource is logged with:


| Field              | Description                                                     |
| ------------------ | ---------------------------------------------------------------- |
| `timestamp`        | When the resource was fetched                                    |
| `resource`         | URL / asset fetched                                               |
| `vector`           | Which extraction vector was checked (§3)                        |
| `url_path`         | Path component                                                    |
| `status`           | HTTP status code / outcome                                        |
| `password_found`   | Password string if a match was found, else empty                  |
| `postponed_signals` | Postponed §3 vector IDs whose *presence condition* was detected on this resource (§4.1), e.g. `["3.20", "3.23"]` — empty list if none |

### 4.1 Postponed-vector applicability signals

We don't run the full (expensive) extraction logic for a postponed §3
vector, but we can cheaply check whether its *trigger condition* exists
on a page — no OCR, no decoding, no extra network calls beyond what the
crawl already fetches. Log a hit into `postponed_signals` whenever the
condition is seen. After the crawl, tally hits per vector across the
audit log: any postponed vector with real hits is a concrete candidate
to promote into the active table in §3; one with zero hits across the
whole crawl is evidence it isn't needed for this target.

**3.12 — WebSocket messages**
- Signal: a WebSocket connection was opened on this page at all (`page.on('websocket')` fired)
- Cost: free — just observe the event, don't parse frame contents

**3.13 — Downloadable files**
- Signal: a linked resource's `Content-Type`/extension is PDF/TXT/CSV/DOCX/ZIP
- Cost: free — already visible from response headers the crawl inspects anyway (§3.1)

**3.15 — QR codes / encoded images**
- Signal: page contains any `<img>` at all (§3.14/§3.19 already enumerate these)
- Cost: free, but **weak** — presence of *an* image doesn't mean it's a QR code; treat a nonzero count as "worth manually sampling a few," not a precise trigger

**3.23 — Audio-encoded password**
- Signal: page contains `<audio src>` or links to an audio file extension (`.mp3`/`.wav`/`.ogg`/`.m4a`)
- Cost: free — same header/extension check pattern as 3.13

**3.24 — Steganography in image pixel data**
- Signal: none reliable — every image is a hypothetical candidate, so presence alone is meaningless as a promotion trigger
- Recommendation: don't promote from a signal; only justified as a manual last resort on a specific page already confirmed (via ground truth) to hold a password that no other vector — including 3.15 — found

**3.26 — Full-page screenshot + OCR**
- Signal: none presence-based — it's the general catch-all, always "applicable" everywhere
- Recommendation: same as 3.24 — promote only as a miss-driven fallback (a page the crawl reached, where §1's ground truth says a password must be reachable, but **no** vector — active or otherwise-promoted-postponed — found a match there), not from a presence signal

## 5. Engineering Constraints

- **Language:** Python
- **Dependencies (active — needed for the current §3 vectors):**
  - `playwright` — browser automation: rendering, click-through nav, network
    interception (3.1–3.3, 3.6, 3.7, 3.9–3.11, 3.18), screenshots (3.19,
    3.20, 3.21, 3.22)
  - `python-dotenv` — loads credentials from `.env`, keeps them out of git
  - `Pillow` — image decoding, EXIF metadata (3.14), preprocessing
    (upscale/threshold) before OCR (3.19, 3.20, 3.21, 3.22)
  - `pytesseract` — OCR for rendered text in images and screenshots (3.19,
    3.20, 3.21, 3.22); requires the Tesseract OCR binary installed on the
    host (not a pip package)
  - stdlib `re`/`json`/`base64`/`urllib.parse`/`html` cover regex scanning,
    JSON parsing, and the decode-then-rescan pass for base64/`atob()`/
    char-code arrays/percent-encoding/CSS hex-escapes/HTML entities/JS
    string escapes (3.2–3.7, 3.9–3.11, 3.17, 3.25) — no extra dependency
    needed
  - A configured HTTP/SOCKS proxy with German egress (3.33, optional,
    `GEO_BYPASS_PROXY` in `.env`) — not a Python dependency, an external
    resource; unset by default, so this vector is a no-op until one is
    provided. Public proxies are ephemeral (working one day, dead the
    next) and receive the site's Basic Auth credentials in plaintext —
    only point this at a proxy you trust, and expect to have to find a
    fresh one periodically

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
