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

| # | Rule |
|---|------|
| 2.1 | Traverse with **breadth-first search**; maintain an explicit `queue` and `visited` set. |
| 2.2 | Cap total visits at **500** to avoid unbounded/infinite crawling. |
| 2.3 | On hitting the cap, **dump the remaining queue** (unvisited URLs) to the log so the limit can be tuned. |
| 2.4 | For URLs with pagination-style params (e.g. `page`), try only **2 trial values** (e.g. `page=1`, `page=2`) rather than exhausting the range. |
| 2.5 | Every request (page loads, XHR/fetch, asset fetches) must carry the authenticated session from `.env`. |

### 2.6 Link-discovery sources

How we find new URLs to enqueue. `<a>` tags are the known baseline;
the rest are candidates to confirm/finalize before implementation.

| Source | Where to look | How to extract |
|---|---|---|
| `<a href>` | HTML body | Static HTML parse |
| ⚠️ TODO | Forms (`<form action>`, GET/POST submission) | ⚠️ TODO |
| ⚠️ TODO | JS click handlers / `onclick` / SPA router navigation | ⚠️ TODO |
| ⚠️ TODO | `<iframe src>` | ⚠️ TODO |
| ⚠️ TODO | Redirects (3xx `Location` header, meta-refresh, JS `location.href`) | ⚠️ TODO |
| ⚠️ TODO | In-page XHR/`fetch()` calls triggered on load (API endpoints) | ⚠️ TODO |
| ⚠️ TODO | `<link>` tags (stylesheet, alternate, prefetch) | ⚠️ TODO |

> **#JOBS** — finalize this table: for each row, set **title**, **where** it
> appears, and **how** we detect/parse it. Add rows for anything missed.

## 3. Password Extraction Vectors

Where in the server's response a `VISUALPING{...}` token might live.
`2.1`–`2.4` are the known baseline categories; specifics need confirmation.

| # | Vector | Where | Detection method |
|---|---|---|---|
| 3.1 | HTTP response — headers | Response headers (incl. non-standard/custom ones, `Set-Cookie`) | ⚠️ TODO |
| 3.2 | HTTP response — body | Raw response body (pre-render, not just DOM) | ⚠️ TODO |
| 3.3 | HTML | Comments, `data-*` attributes, hidden inputs/elements, `<meta>` tags | ⚠️ TODO |
| 3.4 | CSS | Inline `<style>`, linked `.css` files, `content:` properties, custom properties, comments | ⚠️ TODO |
| 3.5 | JS | Inline `<script>`, linked `.js` files, string literals, comments, `console.log` output, source maps | ⚠️ TODO |

> **#JOBS** — for every vector, define: **title**, **where** it's located,
> the expected **format**, and **how** to scan for the
> `VISUALPING{[0-9a-f]{16}}` pattern in it. Consider whether the regex
> needs to run against raw bytes vs. rendered DOM vs. decoded content
> (e.g. base64, JSON-escaped strings).

## 4. Audit Log

Every visited resource is logged with:

| Field | Description |
|---|---|
| `timestamp` | When the resource was fetched |
| `resource` | URL / asset fetched |
| `vector` | Which extraction vector was checked (§3) |
| `url_path` | Path component |
| `status` | HTTP status code / outcome |
| `password_found` | Password string if a match was found, else empty |

## 5. Engineering Constraints

- **Language:** Python
- **Dependencies:**
  - `playwright` — browser automation (real rendering, click-through nav)
  - `python-dotenv` — loads credentials from `.env`, keeps them out of git

## 6. Open Items (JOBS)

- [ ] §2.6 — enumerate and finalize all link-discovery sources beyond `<a>`
  (forms, JS click events, redirects, iframes, background XHR, etc.)
- [ ] §3 — enumerate and finalize all password-extraction vectors, with
  exact locations, expected formats, and detection method per vector
