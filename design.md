# Design: Password Crawler

## 1. Objective

Crawl `TARGET_URL` (see `.env`) as an authenticated user (`AUTH_USERNAME` /
`AUTH_PASSWORD`, see `.env`, not committed to git) and discover every
password on the site matching the format:

```
VISUALPING{<16 hex chars>}
```

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


| #   | Vector                   | Where                                                                                              | Detection method |
| --- | ------------------------ | -------------------------------------------------------------------------------------------------- | ---------------- |
| 3.1 | HTTP response — headers | Response headers (incl. non-standard/custom ones,`Set-Cookie`)                                     | ⚠️ TODO        |
| 3.2 | HTTP response — body    | Raw response body (pre-render, not just DOM)                                                       | ⚠️ TODO        |
| 3.3 | HTML                     | Comments,`data-*` attributes, hidden inputs/elements, `<meta>` tags                                | ⚠️ TODO        |
| 3.4 | CSS                      | Inline`<style>`, linked `.css` files, `content:` properties, custom properties, comments           | ⚠️ TODO        |
| 3.5 | JS                       | Inline`<script>`, linked `.js` files, string literals, comments, `console.log` output, source maps | ⚠️ TODO        |

> **#JOBS** — for every vector, define: **title**, **where** it's located,
> the expected **format**, and **how** to scan for the
> `VISUALPING{[0-9a-f]{16}}` pattern in it. Consider whether the regex
> needs to run against raw bytes vs. rendered DOM vs. decoded content
> (e.g. base64, JSON-escaped strings).

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
- **Dependencies:**
  - `playwright` — browser automation (real rendering, click-through nav)
  - `python-dotenv` — loads credentials from `.env`, keeps them out of git

## 6. Open Items (JOBS)

- [ ]  §2.6 — enumerate and finalize all link-discovery sources beyond `<a>`
  (forms, JS click events, redirects, iframes, background XHR, etc.)
- [ ]  §3 — enumerate and finalize all password-extraction vectors, with
  exact locations, expected formats, and detection method per vector
