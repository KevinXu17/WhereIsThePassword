"""BFS crawl orchestrator (design.md §2) + §3 extraction wiring."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque, Counter
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from playwright.async_api import async_playwright, Response, TimeoutError as PWTimeout, Error as PWError

from . import scanners
from .audit import AuditLog
from .config import (
    TARGET_URL, AUTH_USERNAME, AUTH_PASSWORD, AUTH_HEADER, MAX_VISITS,
    NAV_TIMEOUT_MS, ACTION_TIMEOUT_MS, POST_LOAD_SETTLE_MS, MAX_SCROLL_ROUNDS,
    MAX_CLICK_CANDIDATES_PER_PAGE, MAX_FORMS_PER_PAGE, MAX_SELECT_OPTION_TRIALS,
    AUDIT_LOG_PATH, USER_AGENT, GEO_BYPASS_PATHS, GEO_BYPASS_PROXY,
)
from .dom_script import DOM_HARVEST_JS, STORAGE_JS, INDEXEDDB_JS, CACHE_STORAGE_JS
from .media import scan_image_metadata, ocr_image, find_sourcemap_url, scan_sourcemap
from .patterns import find_passwords, find_passwords_deep, find_html_passwords_labeled
from .urlutils import resolve, normalize, is_same_origin, PaginationLimiter

logger = logging.getLogger("crawler")

LOAD_MORE_TEXT_RE = re.compile(r"load more|show more|view more|next page", re.IGNORECASE)


@dataclass
class ResourceOutcome:
    """Result of scanning one fetched resource, cached by URL so repeats are cheap."""
    hits: list[tuple[str, list[str]]] = field(default_factory=list)
    postponed_signals: list[str] = field(default_factory=list)


def dummy_value(field_type: str, name: str, options: list[str]) -> str:
    field_type = (field_type or "text").lower()
    if options:
        for opt in options:
            if opt:
                return opt
        return ""
    if field_type in ("number", "range"):
        return "1"
    if field_type == "email":
        return "crawler@example.com"
    if field_type == "date":
        return "2024-01-01"
    if field_type in ("checkbox", "radio"):
        return "on"
    if field_type == "tel":
        return "5555555555"
    if field_type == "url":
        return "http://example.com"
    if field_type == "hidden":
        return ""  # keep server-provided default rather than clobbering it
    return "test"


def url_section(url: str) -> str:
    """First path segment, for the human-readable per-section breakdown —
    purely a reporting label, never a crawl limit (design.md has no
    per-section cap; only the global §2.2 visit cap and §2.4 trial cap)."""
    path = urlsplit(url).path.strip("/")
    return path.split("/", 1)[0] if path else "_root"


class Crawler:
    def __init__(self, max_visits: int | None = None, audit_log_path=None) -> None:
        self.max_visits = max_visits if max_visits is not None else MAX_VISITS
        self.audit = AuditLog(audit_log_path or AUDIT_LOG_PATH)
        self.limiter = PaginationLimiter()
        self.visited: set[str] = set()
        self.queue: deque[str] = deque([TARGET_URL])
        self.enqueued: set[str] = {normalize(TARGET_URL)}
        self.resource_cache: dict[str, ResourceOutcome] = {}
        self.ocr_done: set[str] = set()
        self._background_tasks: list[asyncio.Task] = []
        self.section_counts: Counter[str] = Counter()
        self.geo_bypass_done: set[str] = set()

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        async with async_playwright() as pw:
            self.playwright = pw
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                http_credentials={"username": AUTH_USERNAME, "password": AUTH_PASSWORD},
                user_agent=USER_AGENT,
                ignore_https_errors=True,
            )
            self.context = context
            page = await context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)

            try:
                while self.queue:
                    if len(self.visited) >= self.max_visits:
                        self.audit.dump_remaining_queue(self.queue)
                        break
                    url = self.queue.popleft()
                    key = normalize(url)
                    if key in self.visited:
                        continue
                    self.visited.add(key)
                    section = url_section(url)
                    self.section_counts[section] += 1
                    logger.info(
                        "[%d/%d] [%s:%d] visiting %s",
                        len(self.visited), self.max_visits,
                        section, self.section_counts[section], url,
                    )
                    try:
                        links = await self.visit_page(page, url)
                    except Exception as exc:  # noqa: BLE001 - keep the crawl alive
                        logger.exception("visit failed for %s: %s", url, exc)
                        self.audit.record(resource=url, vector="", status="crawler_error")
                        links = []
                    for link in links:
                        self._maybe_enqueue(link)
                if self._background_tasks:
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
            finally:
                if self._background_tasks:
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
                await context.close()
                await browser.close()
                self.audit.close()

    def _maybe_enqueue(self, link: str) -> None:
        if not link:
            return
        if not is_same_origin(link):
            return  # not a skip worth logging — offsite links are expected noise, not a decision
        key = normalize(link)
        if key in self.visited or key in self.enqueued:
            return  # not a skip either — this is routine BFS dedup, not a §2.4 decision
        if not self.limiter.allow(link):
            self.audit.record_skip(link, "query-trial-cap")
            self.enqueued.add(key)
            return
        self.enqueued.add(key)
        self.queue.append(link)

    # ------------------------------------------------------------- one page

    async def visit_page(self, page, url: str) -> list[str]:
        responses: list[Response] = []
        ws_opened = {"flag": False}
        console_hits: list[str] = []

        def on_response(resp: Response) -> None:
            responses.append(resp)

        def on_ws(_ws) -> None:
            ws_opened["flag"] = True

        def on_console(msg) -> None:
            try:
                console_hits.extend(find_passwords_deep(msg.text))
            except Exception:
                pass

        page.on("response", on_response)
        page.on("websocket", on_ws)
        page.on("console", on_console)

        discovered: set[str] = set()
        # §3.27 — scan the URL string itself, before even fetching it. Deep
        # scan so a percent-encoded token in the path/query still matches.
        url_hits = find_passwords_deep(url)

        main_status: object = "navigation_failed"
        try:
            main_resp = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            if main_resp is not None:
                main_status = main_resp.status
        except PWTimeout:
            main_status = "timeout"
        except PWError as exc:
            main_status = f"error: {exc}"

        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        await page.wait_for_timeout(POST_LOAD_SETTLE_MS)

        try:
            await self._trigger_lazy_load(page)
        except Exception as exc:
            logger.debug("lazy-load pass failed on %s: %s", url, exc)

        dom = {}
        storage = {}
        idb_records: list[str] = []
        cache_records: list[str] = []
        try:
            dom = await page.evaluate(DOM_HARVEST_JS)
        except Exception as exc:
            logger.debug("DOM harvest failed on %s: %s", url, exc)
        try:
            storage = await page.evaluate(STORAGE_JS)
        except Exception as exc:
            logger.debug("storage dump failed on %s: %s", url, exc)
        try:
            idb_records = await page.evaluate(INDEXEDDB_JS)
        except Exception as exc:
            logger.debug("indexeddb walk failed on %s: %s", url, exc)
        try:
            cache_records = await page.evaluate(CACHE_STORAGE_JS)
        except Exception as exc:
            logger.debug("cache storage walk failed on %s: %s", url, exc)

        # §3.31 — blob: URLs must be read back *now*, before click exploration
        # below can navigate away and back: `URL.createObjectURL(...)` mints
        # a fresh random UUID on every page load, so a blob: URL captured
        # here would already be invalid against a page reloaded later in
        # this same visit.
        blob_data_hits: list[str] = []
        for data_url in dom.get("dataUrls") or []:
            blob_data_hits.extend(find_passwords_deep(scanners.decode_data_url(data_url)))
        for blob_url in dom.get("blobUrls") or []:
            try:
                blob_text = await page.evaluate(
                    "(u) => fetch(u).then(r => r.text()).catch(() => '')", blob_url
                )
                blob_data_hits.extend(find_passwords_deep(blob_text))
            except Exception as exc:
                logger.debug("blob: fetch failed on %s: %s", blob_url, exc)

        # ---- JS click / SPA-router navigation candidates (§2.6) ----
        click_links: list[str] = []
        try:
            click_links = await self._explore_clickables(page, url)
        except Exception as exc:
            logger.debug("click exploration failed on %s: %s", url, exc)
        discovered.update(click_links)

        # ---- forms + formaction overrides, submitted headlessly via APIRequestContext ----
        form_links: list[str] = []
        try:
            form_links = await self._submit_forms(dom.get("forms") or [])
        except Exception as exc:
            logger.debug("form submission failed on %s: %s", url, exc)
        discovered.update(form_links)

        # ---- iframes: recurse via Playwright frame objects ----
        # Mirrors the main frame's own extraction, not just a raw-body scan —
        # a same-origin (and, via Playwright's CDP-level access, even
        # cross-origin) frame has its own DOM/storage/IndexedDB/Cache
        # Storage that a main-frame-only scan would silently never see.
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                furl = frame.url
                if not furl or furl in ("about:blank",):
                    continue
                discovered.add(furl)
                fcontent = await frame.content()
                by_vector: dict[str, list[str]] = {}
                for vec, pw in find_html_passwords_labeled(fcontent):
                    by_vector.setdefault(vec, []).append(pw)
                deep_already = {pw for pws in by_vector.values() for pw in pws}
                deep_extra = [pw for pw in find_passwords_deep(fcontent) if pw not in deep_already]
                if deep_extra:
                    by_vector.setdefault("3.2", []).extend(deep_extra)

                try:
                    fdom = await frame.evaluate(DOM_HARVEST_JS)
                except Exception:
                    fdom = {}
                rendered_text = "\n".join(
                    [fdom.get("outerHTML") or "", fdom.get("bodyText") or ""]
                    + (fdom.get("shadowTexts") or [])
                )
                rendered_hits = find_passwords_deep(rendered_text)
                if rendered_hits:
                    by_vector.setdefault("3.29", []).extend(rendered_hits)

                try:
                    fstorage = await frame.evaluate(STORAGE_JS)
                except Exception:
                    fstorage = {}
                storage_hits = scanners.scan_storage(fstorage) if fstorage else []
                if storage_hits:
                    by_vector.setdefault("3.6/3.7", []).extend(storage_hits)

                try:
                    fidb = await frame.evaluate(INDEXEDDB_JS)
                except Exception:
                    fidb = []
                idb_hits_f = scanners.scan_indexeddb(fidb)
                if idb_hits_f:
                    by_vector.setdefault("3.6", []).extend(idb_hits_f)

                try:
                    fcache = await frame.evaluate(CACHE_STORAGE_JS)
                except Exception:
                    fcache = []
                cache_hits_f = scanners.scan_cache_storage(fcache)
                if cache_hits_f:
                    by_vector.setdefault("3.30", []).extend(cache_hits_f)

                if by_vector:
                    self.audit.record_many(furl, list(by_vector.items()), status="iframe")
            except Exception:
                pass
        for iframe_src in dom.get("iframes") or []:
            discovered.add(iframe_src)

        # ---- static link sources from the DOM harvest ----
        for link in dom.get("links") or []:
            discovered.add(link)
        if dom.get("metaRefresh"):
            discovered.add(dom["metaRefresh"])

        # ---- responses fired during this visit: headers/body/CSS/JS/images/etc ----
        page_level_hits: list[tuple[str, list[str]]] = []
        if url_hits:
            page_level_hits.append(("3.27", url_hits))
        if console_hits:
            page_level_hits.append(("3.5", list(dict.fromkeys(console_hits))))

        storage_hits = scanners.scan_storage(storage) if storage else []
        if storage_hits:
            page_level_hits.append(("3.6/3.7", storage_hits))
        idb_hits = scanners.scan_indexeddb(idb_records)
        if idb_hits:
            page_level_hits.append(("3.6", idb_hits))
        cache_hits = scanners.scan_cache_storage(cache_records)
        if cache_hits:
            page_level_hits.append(("3.30", cache_hits))
        attr_hits = scanners.scan_attr_texts(dom.get("attrTexts") or [])
        if attr_hits:
            page_level_hits.append(("3.10", attr_hits))

        # §3.29 — rendered DOM (post-JS): catches text a script inserted or
        # mutated in after the server's raw HTML (§3.2) was already sent —
        # SPA routing, `innerHTML = ...`, etc. — plus open shadow-root text
        # that outerHTML/innerText never serialize on their own.
        rendered_text = "\n".join(
            [dom.get("outerHTML") or "", dom.get("bodyText") or ""]
            + (dom.get("shadowTexts") or [])
        )
        rendered_hits = find_passwords_deep(rendered_text)
        if rendered_hits:
            page_level_hits.append(("3.29", rendered_hits))

        # §3.31 — blob:/data: URL content, gathered earlier (before click
        # exploration could invalidate any blob: URL by navigating away).
        if blob_data_hits:
            page_level_hits.append(("3.31", list(dict.fromkeys(blob_data_hits))))

        page_signals: list[str] = []
        sig = (dom.get("signals") or {})
        canvas_ids = sig.get("canvasIds") or []
        svg_text_ids = sig.get("svgTextIds") or []
        inline_style_text = dom.get("inlineStyleText") or ""
        inline_font_glyph_signal = scanners.font_face_has_unicode_range(inline_style_text)
        if ws_opened["flag"]:
            page_signals.append("3.12")
        if inline_font_glyph_signal:
            page_signals.append("3.21")
        if sig.get("audioEls"):
            page_signals.append("3.23")
        if dom.get("imgSrcs"):
            page_signals.append("3.15")

        self.audit.record_many(url, page_level_hits, status=main_status, postponed_signals=page_signals)

        # §3.17 — manifest link is present: fetch its JSON body and scan it
        # (previously only the tag's *presence* was logged, the body itself
        # was never actually read).
        manifest_href = dom.get("manifestHref")
        if manifest_href:
            resolved_manifest = resolve(url, manifest_href)
            if resolved_manifest and resolved_manifest not in self.resource_cache:
                self.resource_cache[resolved_manifest] = ResourceOutcome()  # claim it
                self._background_tasks.append(
                    asyncio.ensure_future(self._fetch_and_scan_manifest(resolved_manifest))
                )

        # §3.20 — screenshot + OCR every tagged <canvas> individually.
        for cid in canvas_ids:
            try:
                shot = await page.locator(f'[data-crawler-canvas-id="{cid}"]').first.screenshot(
                    timeout=ACTION_TIMEOUT_MS
                )
                canvas_hits = ocr_image(shot)
                if canvas_hits:
                    self.audit.record_many(url, [("3.20", canvas_hits)], status=main_status)
            except Exception as exc:
                logger.debug("canvas screenshot/OCR failed on %s (id=%s): %s", url, cid, exc)

        # §3.22 — same for <svg> built from <path> shapes with no real
        # <text>/<tspan> node — nothing for a text parser to find, so
        # screenshot + OCR is the only way to read it.
        for sid in svg_text_ids:
            try:
                shot = await page.locator(f'[data-crawler-svgtext-id="{sid}"]').first.screenshot(
                    timeout=ACTION_TIMEOUT_MS
                )
                svg_hits = ocr_image(shot)
                if svg_hits:
                    self.audit.record_many(url, [("3.22", svg_hits)], status=main_status)
            except Exception as exc:
                logger.debug("svg screenshot/OCR failed on %s (id=%s): %s", url, sid, exc)

        for resp in responses:
            try:
                links_from_resp = await self._process_response(resp)
                discovered.update(links_from_resp)
            except Exception as exc:
                logger.debug("response processing failed for %s: %s", resp.url, exc)

        # §3.21 — a custom @font-face + unicode-range remap means the DOM's
        # own textContent can lie about what's actually rendered. Only worth
        # the cost of a full-page screenshot + OCR when that specific
        # trigger condition was actually seen — either inline (checked
        # above) or in an external stylesheet just fetched and classified
        # via the responses loop above.
        css_font_glyph_signal = any(
            "3.21" in (self.resource_cache.get(r.url) or ResourceOutcome()).postponed_signals
            for r in responses
        )
        if inline_font_glyph_signal or css_font_glyph_signal:
            try:
                shot = await page.screenshot(full_page=True, timeout=ACTION_TIMEOUT_MS)
                glyph_hits = ocr_image(shot)
                if glyph_hits:
                    self.audit.record_many(url, [("3.21", glyph_hits)], status=main_status)
            except Exception as exc:
                logger.debug("font-glyph screenshot/OCR failed on %s: %s", url, exc)

        page.remove_listener("response", on_response)
        page.remove_listener("websocket", on_ws)
        page.remove_listener("console", on_console)

        return [r for r in (resolve(url, d) for d in discovered) if r]

    # --------------------------------------------------------- lazy loading

    async def _trigger_lazy_load(self, page) -> None:
        for _ in range(MAX_SCROLL_ROUNDS):
            prev_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
            await page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            await page.wait_for_timeout(250)

            clicked = False
            try:
                locator = page.get_by_text(LOAD_MORE_TEXT_RE)
                count = await locator.count()
                if count:
                    await locator.first.click(timeout=ACTION_TIMEOUT_MS)
                    clicked = True
                    await page.wait_for_timeout(300)
            except Exception:
                pass

            new_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
            if new_height == prev_height and not clicked:
                break

    # ------------------------------------------------- JS click candidates

    async def _explore_clickables(self, page, original_url: str) -> list[str]:
        found_links: list[str] = []
        tried: set[str] = set()
        for _ in range(MAX_CLICK_CANDIDATES_PER_PAGE):
            try:
                candidates = await page.evaluate(DOM_HARVEST_JS)
            except Exception:
                break
            candidate_list = candidates.get("clickCandidates") or []
            target = None
            for c in candidate_list:
                sig = f"{c.get('tag')}::{c.get('text')}"
                if sig not in tried:
                    tried.add(sig)
                    target = c
                    break
            if target is None:
                break

            before_url = page.url
            before_pages = len(page.context.pages)
            try:
                locator = page.locator(f'[data-crawler-click-id="{target["id"]}"]')
                if await locator.count() == 0:
                    continue
                await locator.first.click(timeout=ACTION_TIMEOUT_MS, force=True)
                await page.wait_for_timeout(300)
            except Exception:
                continue

            # New tab opened via the click?
            if len(page.context.pages) > before_pages:
                new_page = page.context.pages[-1]
                try:
                    await new_page.wait_for_load_state("load", timeout=NAV_TIMEOUT_MS)
                    found_links.append(new_page.url)
                except Exception:
                    pass
                finally:
                    try:
                        await new_page.close()
                    except Exception:
                        pass

            after_url = page.url
            if after_url != before_url:
                found_links.append(after_url)
                try:
                    await page.goto(original_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                    await page.wait_for_timeout(200)
                except Exception:
                    break
        return found_links

    # ------------------------------------------------------------- forms

    async def _submit_forms(self, forms: list[dict]) -> list[str]:
        links: list[str] = []
        for form in forms[:MAX_FORMS_PER_PAGE]:
            action = form.get("action")
            method = (form.get("method") or "get").lower()
            if not action:
                continue

            base_data: dict[str, str] = {}
            select_fields: list[tuple[str, list[str]]] = []
            for f in form.get("fields") or []:
                name = f.get("name")
                if not name:
                    continue
                options = f.get("options") or []
                base_data[name] = dummy_value(f.get("type"), name, options)
                if (f.get("tag") or "").lower() == "select" and len(options) > 1:
                    select_fields.append((name, options))

            # A <select> previously only ever got submitted with its first
            # option — any page reachable only via a *different* option
            # value was silently never visited. Vary one select at a time
            # (holding the rest at their dummy default) rather than the full
            # cross-product, which would blow up fast with >1 dropdown.
            variants = [dict(base_data)]
            for name, options in select_fields:
                seen_values = {base_data.get(name)}
                for opt in options:
                    if not opt or opt in seen_values:
                        continue
                    seen_values.add(opt)
                    variant = dict(base_data)
                    variant[name] = opt
                    variants.append(variant)
                    if len(variants) >= MAX_SELECT_OPTION_TRIALS:
                        break
                if len(variants) >= MAX_SELECT_OPTION_TRIALS:
                    break

            for data in variants:
                try:
                    result_url = await self._submit_one(action, method, data)
                    if result_url:
                        links.append(result_url)
                except Exception as exc:
                    logger.debug("form submit to %s failed: %s", action, exc)

                for override in form.get("submitOverrides") or []:
                    fa = override.get("formaction")
                    if not fa:
                        continue
                    try:
                        result_url = await self._submit_one(fa, method, data)
                        if result_url:
                            links.append(result_url)
                    except Exception as exc:
                        logger.debug("formaction submit to %s failed: %s", fa, exc)
        return links

    async def _submit_one(self, action: str, method: str, data: dict) -> str | None:
        headers = dict(AUTH_HEADER)
        if method == "get":
            base = action.split("?")[0]
            qs = urlencode(data)
            target = f"{base}?{qs}" if qs else base
            resp = await self.context.request.get(target, headers=headers, timeout=NAV_TIMEOUT_MS)
        else:
            resp = await self.context.request.post(action, form=data, headers=headers, timeout=NAV_TIMEOUT_MS)
        await self._scan_api_response(resp, vector_hint="form-submit")
        return resp.url

    # ------------------------------------------------------------ responses

    async def _process_response(self, resp: Response) -> list[str]:
        url = resp.url
        try:
            status = resp.status
        except Exception:
            status = "unknown"
        try:
            status_text = resp.status_text
        except Exception:
            status_text = ""
        try:
            # Playwright's `resp.headers` (sync) can silently drop Set-Cookie
            # on some Chromium versions (documented CDP quirk) — design.md
            # §3.1 explicitly calls out Set-Cookie, so use `all_headers()`.
            headers = await resp.all_headers()
        except Exception:
            try:
                headers = dict(resp.headers)
            except Exception:
                headers = {}
        try:
            req = resp.request
            resource_type = req.resource_type
        except Exception:
            resource_type = "other"

        cached = self.resource_cache.get(url)
        discovered: list[str] = []

        if cached is None:
            hits: list[tuple[str, list[str]]] = []
            signals: list[str] = []

            header_hits = scanners.scan_headers(headers)
            if header_hits:
                hits.append(("3.1", header_hits))
            status_hits = scanners.scan_status_text(status_text)
            if status_hits:
                hits.append(("3.28", status_hits))

            content_type = headers.get("content-type", "")

            # §3.32 — Server-Sent Events. A live `text/event-stream`
            # connection may never "complete" the way `resp.body()` expects
            # (it's a deliberately long-lived, open-ended response) — same
            # concern as WebSocket (§3.12), so this is signal-only, and
            # critically skips the `resp.body()` call below entirely rather
            # than risk hanging the crawl waiting on a stream that never ends.
            if scanners.is_sse(content_type):
                outcome = ResourceOutcome(hits=[], postponed_signals=["3.32"])
                self.resource_cache[url] = outcome
                self.audit.record_many(url, [], status=status, postponed_signals=["3.32"])
                return discovered

            body: bytes | None = None
            try:
                body = await resp.body()
            except Exception:
                body = None
            text_body = None
            if body is not None:
                try:
                    text_body = body.decode("utf-8", "ignore")
                except Exception:
                    text_body = None

            # §3.1 Location header target is also a discovered link (§2.6 redirects).
            location = headers.get("location")
            if location:
                resolved_loc = resolve(url, location)
                if resolved_loc:
                    discovered.append(resolved_loc)



            if resource_type == "document":
                if isinstance(status, int) and 200 <= status < 300:
                    # §3.3/§3.9 are pure attribution labels on top of this same
                    # raw-body scan (design.md is explicit they're not their
                    # own pass) — group same-vector hits into one row each.
                    by_vector: dict[str, list[str]] = {}
                    for vec, pw in find_html_passwords_labeled(text_body):
                        by_vector.setdefault(vec, []).append(pw)
                    already = {pw for pws in by_vector.values() for pw in pws}
                    deep_extra = [pw for pw in find_passwords_deep(text_body) if pw not in already]
                    if deep_extra:
                        by_vector.setdefault("3.2", []).extend(deep_extra)
                    for vec, pws in by_vector.items():
                        hits.append((vec, pws))
                else:
                    error_hits = find_passwords_deep(text_body)
                    if error_hits:
                        hits.append(("3.18", error_hits))
                    self._maybe_geo_bypass(url)
            elif scanners.is_css(content_type, url):
                css_hits = find_passwords_deep(text_body)
                if css_hits:
                    hits.append(("3.4", css_hits))
                if scanners.font_face_has_unicode_range(text_body or ""):
                    signals.append("3.21")
                self._maybe_check_sourcemap(url, text_body, headers)
                for raw_ref in scanners.extract_css_urls(text_body or ""):
                    resolved = resolve(url, raw_ref)
                    if resolved and resolved not in self.resource_cache:
                        self.resource_cache[resolved] = ResourceOutcome()  # claim it, avoid dup fetch
                        self._background_tasks.append(
                            asyncio.ensure_future(self._fetch_and_scan_binary(resolved))
                        )
            elif scanners.is_js(content_type, url):
                js_hits = find_passwords_deep(text_body)
                if js_hits:
                    hits.append(("3.5", js_hits))
                self._maybe_check_sourcemap(url, text_body, headers)
            elif scanners.is_svg(content_type, url):
                svg_hits = find_passwords_deep(text_body)
                if svg_hits:
                    hits.append(("3.14", svg_hits))
            elif scanners.is_image(content_type, url) and body:
                meta_hits = scan_image_metadata(body)
                if meta_hits:
                    hits.append(("3.14", meta_hits))
                if url not in self.ocr_done:
                    self.ocr_done.add(url)
                    ocr_hits = ocr_image(body)
                    if ocr_hits:
                        hits.append(("3.19", ocr_hits))
            elif resource_type in ("xhr", "fetch"):
                if scanners.is_json_ld_or_manifest(content_type):
                    ld_hits = find_passwords_deep(text_body)
                    if ld_hits:
                        hits.append(("3.9", ld_hits))
                else:
                    xhr_hits = find_passwords_deep(text_body)
                    if xhr_hits:
                        hits.append(("3.11", xhr_hits))
                    for u in _extract_url_like_strings(text_body):
                        resolved_u = resolve(url, u)
                        if resolved_u:
                            discovered.append(resolved_u)
            elif scanners.is_downloadable_doc(content_type, url):
                signals.append("3.13")
            elif scanners.is_audio(content_type, url):
                signals.append("3.23")
            elif scanners.is_binary_unclaimed(content_type, url):
                # §3.25 — actually scan the raw bytes now, not just log that
                # an unclaimed binary content-type was seen.
                binary_hits = scanners.scan_binary_strings(body)
                if binary_hits:
                    hits.append(("3.25", binary_hits))
            else:
                other_hits = find_passwords_deep(text_body)
                if other_hits:
                    hits.append(("3.11", other_hits))

            outcome = ResourceOutcome(hits=hits, postponed_signals=signals)
            self.resource_cache[url] = outcome
        else:
            outcome = cached

        self.audit.record_many(url, outcome.hits, status=status, postponed_signals=outcome.postponed_signals)
        return discovered

    def _maybe_check_sourcemap(self, url: str, text: str | None, headers: dict) -> None:
        map_url = find_sourcemap_url(text or "", headers)
        if not map_url:
            return
        resolved = resolve(url, map_url)
        if not resolved or resolved in self.resource_cache:
            return
        self.resource_cache[resolved] = ResourceOutcome()  # claim it, avoid dup fetch
        self._background_tasks.append(asyncio.ensure_future(self._fetch_and_scan_sourcemap(resolved)))

    async def _fetch_and_scan_sourcemap(self, map_url: str) -> None:
        try:
            resp = await self.context.request.get(map_url, headers=AUTH_HEADER, timeout=NAV_TIMEOUT_MS)
            body = await resp.body()
            import json

            data = json.loads(body.decode("utf-8", "ignore"))
            hits = scan_sourcemap(data)
            self.resource_cache[map_url] = ResourceOutcome(hits=[("3.16", hits)] if hits else [])
            self.audit.record_many(map_url, [("3.16", hits)] if hits else [], status=resp.status)
        except Exception as exc:
            logger.debug("sourcemap fetch/scan failed for %s: %s", map_url, exc)

    async def _fetch_and_scan_binary(self, res_url: str) -> None:
        """Fetch a resource discovered only as a reference (e.g. a CSS `url()`
        that the renderer never actually requested) and run it through the
        same classification as a normally-observed response."""
        try:
            resp = await self.context.request.get(res_url, headers=AUTH_HEADER, timeout=NAV_TIMEOUT_MS)
            status = resp.status
            headers = dict(resp.headers)
            content_type = headers.get("content-type", "")
            body = await resp.body()
        except Exception as exc:
            logger.debug("followup fetch failed for %s: %s", res_url, exc)
            return

        hits: list[tuple[str, list[str]]] = []
        signals: list[str] = []
        header_hits = scanners.scan_headers(headers)
        if header_hits:
            hits.append(("3.1", header_hits))

        if scanners.is_image(content_type, res_url) and body:
            meta_hits = scan_image_metadata(body)
            if meta_hits:
                hits.append(("3.14", meta_hits))
            if res_url not in self.ocr_done:
                self.ocr_done.add(res_url)
                ocr_hits = ocr_image(body)
                if ocr_hits:
                    hits.append(("3.19", ocr_hits))
        elif scanners.is_svg(content_type, res_url):
            svg_hits = find_passwords_deep(body.decode("utf-8", "ignore") if body else "")
            if svg_hits:
                hits.append(("3.14", svg_hits))
        elif scanners.is_binary_unclaimed(content_type, res_url):
            binary_hits = scanners.scan_binary_strings(body)
            if binary_hits:
                hits.append(("3.25", binary_hits))

        outcome = ResourceOutcome(hits=hits, postponed_signals=signals)
        self.resource_cache[res_url] = outcome
        self.audit.record_many(res_url, hits, status=status, postponed_signals=signals)

    async def _fetch_and_scan_manifest(self, manifest_url: str) -> None:
        """§3.17 — fetch the web-app manifest JSON and scan its body (both
        parsed values and the raw text, in case it isn't valid JSON)."""
        try:
            resp = await self.context.request.get(manifest_url, headers=AUTH_HEADER, timeout=NAV_TIMEOUT_MS)
            status = resp.status
            body = await resp.body()
            text = body.decode("utf-8", "ignore") if body else ""
        except Exception as exc:
            logger.debug("manifest fetch failed for %s: %s", manifest_url, exc)
            return

        hits = find_passwords_deep(text)
        outcome = ResourceOutcome(hits=[("3.17", hits)] if hits else [])
        self.resource_cache[manifest_url] = outcome
        self.audit.record_many(manifest_url, [("3.17", hits)] if hits else [], status=status)

    def _maybe_geo_bypass(self, url: str) -> None:
        """§3.33 — a hardcoded, known-specific dead end (see GEO_BYPASS_PATHS'
        docstring in config.py for why this isn't a generic heuristic).
        No-op unless both the path matches and GEO_BYPASS_PROXY is set —
        never affects the crawl otherwise, and never retried once attempted
        for a given URL regardless of outcome."""
        if not GEO_BYPASS_PROXY:
            return
        if urlsplit(url).path not in GEO_BYPASS_PATHS:
            return
        if url in self.geo_bypass_done:
            return
        self.geo_bypass_done.add(url)
        self._background_tasks.append(asyncio.ensure_future(self._fetch_via_geo_proxy(url)))

    async def _fetch_via_geo_proxy(self, url: str) -> None:
        """One-off fetch of a single known GeoIP-gated URL through a
        separate `APIRequestContext` configured with GEO_BYPASS_PROXY — kept
        completely isolated from the main browser `context` so this never
        changes the egress IP (and therefore the GeoIP result) for anything
        else the crawl fetches."""
        proxy_context = None
        try:
            proxy_context = await self.playwright.request.new_context(
                proxy={"server": GEO_BYPASS_PROXY},
                extra_http_headers=AUTH_HEADER,
                ignore_https_errors=True,
                timeout=NAV_TIMEOUT_MS,
            )
            resp = await proxy_context.get(url, timeout=NAV_TIMEOUT_MS)
            status = resp.status
            body = await resp.body()
            text = body.decode("utf-8", "ignore") if body else ""
        except Exception as exc:
            logger.debug("geo-bypass proxy fetch failed for %s: %s", url, exc)
            return
        finally:
            if proxy_context is not None:
                try:
                    await proxy_context.dispose()
                except Exception:
                    pass

        hits = find_passwords_deep(text)
        self.audit.record_many(url, [("3.33", hits)] if hits else [], status=status)
        if hits:
            logger.info(
                "geo-bypass succeeded for %s via proxy (status %s)", url, status
            )

    async def _scan_api_response(self, resp, vector_hint: str) -> None:
        try:
            url = resp.url
            status = resp.status
            headers = dict(resp.headers)
            body = await resp.body()
            text = body.decode("utf-8", "ignore") if body else ""
        except Exception:
            return
        hits: list[tuple[str, list[str]]] = []
        header_hits = scanners.scan_headers(headers)
        if header_hits:
            hits.append(("3.1", header_hits))
        body_hits = find_passwords_deep(text)
        if body_hits:
            hits.append(("3.11", body_hits))
        self.audit.record_many(url, hits, status=status)


def _extract_url_like_strings(text: str | None) -> list[str]:
    if not text:
        return []
    out = []
    for m in re.finditer(r'"((?:/[a-zA-Z0-9_\-./?=&%]{1,200})|(?:https?://[^"\s]{1,200}))"', text):
        out.append(m.group(1))
    return out
