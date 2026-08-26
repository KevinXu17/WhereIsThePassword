"""JS injected via page.evaluate() to harvest one page's DOM in a single round trip.

Covers the static (non-interactive) parts of design.md §2.6 / §3.10 / §4.1 that
don't need Playwright-level clicking: <a>, forms, formaction controls, click
candidates (tagged for the caller to locate+click), iframes' src, meta-refresh,
<link rel=manifest>, non-rendered text attributes, and the cheap postponed
presence signals (canvas / svg-paths-without-text / audio / manifest).
"""

DOM_HARVEST_JS = r"""
() => {
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch { return null; } };

  const links = new Set();
  document.querySelectorAll('a[href]').forEach(a => { const u = abs(a.getAttribute('href')); if (u) links.add(u); });
  document.querySelectorAll('area[href]').forEach(a => { const u = abs(a.getAttribute('href')); if (u) links.add(u); });

  // Redirects surfaced in markup (meta refresh) rather than headers.
  let metaRefresh = null;
  const metaTag = document.querySelector('meta[http-equiv="refresh" i]');
  if (metaTag) {
    const content = metaTag.getAttribute('content') || '';
    const m = content.match(/url\s*=\s*(.+)$/i);
    if (m) metaRefresh = abs(m[1].trim().replace(/^['"]|['"]$/g, ''));
  }

  // Forms: action/method + declared fields, so the caller can submit them.
  const forms = [];
  document.querySelectorAll('form').forEach((form, idx) => {
    form.setAttribute('data-crawler-form-id', String(idx));
    const fields = [];
    form.querySelectorAll('input, select, textarea').forEach(el => {
      fields.push({
        name: el.getAttribute('name') || '',
        type: (el.getAttribute('type') || el.tagName.toLowerCase()),
        tag: el.tagName.toLowerCase(),
        options: el.tagName.toLowerCase() === 'select'
          ? Array.from(el.options).map(o => o.value)
          : [],
      });
    });
    // formaction overrides on submit controls (§2.6 button[formaction]).
    const submitOverrides = [];
    form.querySelectorAll('[formaction]').forEach((el, subIdx) => {
      el.setAttribute('data-crawler-submit-id', String(subIdx));
      submitOverrides.push({
        submitId: subIdx,
        formaction: abs(el.getAttribute('formaction')),
      });
    });
    forms.push({
      formId: idx,
      action: abs(form.getAttribute('action') || location.href),
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields,
      submitOverrides,
    });
  });

  // JS click / SPA-router candidates: tag them so Python can locate+click.
  const clickCandidates = [];
  const seen = new Set();
  document.querySelectorAll('[onclick], [role="button" i], button, [tabindex]').forEach((el, idx) => {
    if (seen.has(el)) return;
    seen.add(el);
    if (el.closest('form') && el.tagName.toLowerCase() !== 'button') return; // forms handled separately
    el.setAttribute('data-crawler-click-id', String(idx));
    clickCandidates.push({
      id: idx,
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || '').trim().slice(0, 60),
    });
  });

  // "Load more" / infinite-scroll triggers.
  const loadMoreCandidates = [];
  document.querySelectorAll('button, a, [role="button" i]').forEach((el, idx) => {
    const t = (el.textContent || '').trim().toLowerCase();
    if (/load more|show more|view more|next page/.test(t)) {
      el.setAttribute('data-crawler-loadmore-id', String(idx));
      loadMoreCandidates.push({ id: idx, text: t.slice(0, 60) });
    }
  });

  // iframes (recursed into separately via page.frames() on the Python side).
  const iframes = [];
  document.querySelectorAll('iframe[src]').forEach(f => { const u = abs(f.getAttribute('src')); if (u) iframes.push(u); });

  // <object data> / <embed src> (§2.6 postponed, cheap to grab while we're here).
  document.querySelectorAll('object[data]').forEach(o => { const u = abs(o.getAttribute('data')); if (u) links.add(u); });
  document.querySelectorAll('embed[src]').forEach(o => { const u = abs(o.getAttribute('src')); if (u) links.add(u); });

  // Non-rendered text attributes (§3.10).
  const attrTexts = [];
  document.querySelectorAll('[alt], [title], [placeholder]').forEach(el => {
    ['alt', 'title', 'placeholder'].forEach(a => {
      const v = el.getAttribute(a);
      if (v) attrTexts.push(v);
    });
  });
  document.querySelectorAll('*').forEach(el => {
    for (const attr of el.attributes) {
      if (attr.name.startsWith('aria-') && attr.value) attrTexts.push(attr.value);
      if (attr.name.startsWith('data-') && !attr.name.startsWith('data-crawler-') && attr.value) attrTexts.push(attr.value);
    }
  });

  // §4.1 cheap postponed-vector presence signals.
  const manifestLink = document.querySelector('link[rel="manifest" i]');
  const manifestHref = manifestLink ? abs(manifestLink.getAttribute('href')) : null;

  // §3.20 — tag every <canvas> so the caller can locate+screenshot it
  // individually (a DOM count alone isn't enough to act on).
  const canvasIds = [];
  document.querySelectorAll('canvas').forEach((el, idx) => {
    el.setAttribute('data-crawler-canvas-id', String(idx));
    canvasIds.push(idx);
  });

  // §3.22 — same tagging for inline <svg> built from <path> shapes with no
  // real <text>/<tspan> node (letters drawn as paths, nothing to parse).
  const svgTextIds = [];
  document.querySelectorAll('svg').forEach((svg, idx) => {
    const hasPath = svg.querySelector('path');
    const hasText = svg.querySelector('text, tspan');
    if (hasPath && !hasText) {
      svg.setAttribute('data-crawler-svgtext-id', String(idx));
      svgTextIds.push(idx);
    }
  });

  // §3.21 — inline <style> text isn't visited as its own fetched resource
  // (unlike a linked .css file), so the font-face/unicode-range check needs
  // it handed over explicitly here instead.
  const inlineStyleText = Array.from(document.querySelectorAll('style'))
    .map(s => s.textContent || '').join('\n');

  const audioEls = [];
  document.querySelectorAll('audio[src], audio source[src]').forEach(a => { const u = abs(a.getAttribute('src')); if (u) audioEls.push(u); });
  // §3.23 signal also covers a plain <a> link to an audio file, not just
  // <audio>/<source> elements (design.md's own spec for this signal).
  document.querySelectorAll('a[href]').forEach(a => {
    const href = (a.getAttribute('href') || '').toLowerCase();
    if (/\.(mp3|wav|ogg|m4a)(\?|#|$)/.test(href)) { const u = abs(a.getAttribute('href')); if (u) audioEls.push(u); }
  });
  const imgSrcs = [];
  document.querySelectorAll('img[src]').forEach(img => { const u = abs(img.getAttribute('src')); if (u) imgSrcs.push(u); });

  // §3.29 — rendered DOM (post-JS). §3.2's raw-body scan only ever sees what
  // the server actually sent; anything a script inserts/mutates afterward
  // (SPA routing, `innerHTML =`, etc.) only ever exists here.
  const outerHTML = document.documentElement ? document.documentElement.outerHTML : '';
  const bodyText = document.body ? document.body.innerText : '';

  // Open shadow roots aren't reachable through outerHTML/innerText at all
  // (the browser doesn't serialize them into either) — walk the tree
  // explicitly. Closed shadow roots are genuinely unreachable from outside;
  // that's a real browser limitation too, not something to work around.
  const shadowTexts = [];
  (function collectShadow(root) {
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) {
        shadowTexts.push(el.shadowRoot.textContent || '');
        el.shadowRoot.querySelectorAll('*').forEach(inner => {
          for (const attr of inner.attributes) if (attr.value) shadowTexts.push(attr.value);
        });
        collectShadow(el.shadowRoot);
      }
    });
  })(document);

  // §3.31 — blob:/data: URLs never touch the network, so §3.11's response
  // interception can't see them. blob: needs an in-page fetch (the URL is
  // only valid inside the page that created it); data: can be decoded
  // straight from the string on the Python side.
  const blobUrls = new Set();
  const dataUrls = new Set();
  document.querySelectorAll('img[src], iframe[src], source[src], object[data], embed[src], a[href]').forEach(el => {
    const raw = el.getAttribute('src') || el.getAttribute('href') || el.getAttribute('data') || '';
    if (raw.startsWith('blob:')) blobUrls.add(raw);
    else if (raw.startsWith('data:')) dataUrls.add(raw);
  });

  return {
    links: Array.from(links),
    metaRefresh,
    forms,
    clickCandidates,
    loadMoreCandidates,
    iframes,
    attrTexts,
    manifestHref,
    inlineStyleText,
    outerHTML,
    bodyText,
    shadowTexts,
    blobUrls: Array.from(blobUrls),
    dataUrls: Array.from(dataUrls),
    signals: {
      hasManifestLink: !!manifestLink,
      canvasIds,
      svgTextIds,
      audioEls,
    },
    imgSrcs,
    title: document.title,
  };
}
"""

STORAGE_JS = r"""
() => {
  const dump = (storage) => {
    const out = {};
    try {
      for (let i = 0; i < storage.length; i++) {
        const k = storage.key(i);
        out[k] = storage.getItem(k);
      }
    } catch (e) {}
    return out;
  };
  let historyState = null;
  try { historyState = JSON.stringify(history.state); } catch (e) {}
  return {
    localStorage: dump(window.localStorage),
    sessionStorage: dump(window.sessionStorage),
    cookie: document.cookie,
    // window.name and history.state are JS-writable global state that can
    // carry a value across navigations without ever touching
    // localStorage/sessionStorage/cookies — cheap to grab while we're here.
    windowName: window.name || '',
    historyState: historyState || '',
  };
}
"""

# §3.30 — Cache Storage API: response bodies a service worker or page script
# stashed via `caches.open(...).then(c => c.put(...))`. Doesn't touch the
# network on read, so §3.11's response interception never sees it, and it's
# a distinct API from IndexedDB — needs its own walk.
CACHE_STORAGE_JS = r"""
async () => {
  const results = [];
  if (!window.caches || !caches.keys) return results;
  try {
    const names = await caches.keys();
    for (const name of names) {
      const cache = await caches.open(name);
      const requests = await cache.keys();
      for (const req of requests) {
        try {
          const res = await cache.match(req);
          if (!res) continue;
          const text = await res.text();
          results.push(text);
        } catch (e) {}
      }
    }
  } catch (e) {}
  return results;
}
"""

# §3.6 IndexedDB — async, walks every store via a cursor.
INDEXEDDB_JS = r"""
async () => {
  const results = [];
  if (!window.indexedDB || !indexedDB.databases) return results;
  try {
    const dbs = await indexedDB.databases();
    for (const dbInfo of dbs) {
      if (!dbInfo.name) continue;
      await new Promise((resolve) => {
        const req = indexedDB.open(dbInfo.name);
        req.onsuccess = () => {
          const db = req.result;
          const storeNames = Array.from(db.objectStoreNames);
          if (storeNames.length === 0) { db.close(); resolve(); return; }
          let remaining = storeNames.length;
          storeNames.forEach(storeName => {
            try {
              const tx = db.transaction(storeName, 'readonly');
              const store = tx.objectStore(storeName);
              const cursorReq = store.openCursor();
              cursorReq.onsuccess = (event) => {
                const cursor = event.target.result;
                if (cursor) {
                  try { results.push(JSON.stringify(cursor.value)); } catch (e) {}
                  cursor.continue();
                } else {
                  remaining -= 1;
                  if (remaining === 0) { db.close(); resolve(); }
                }
              };
              cursorReq.onerror = () => { remaining -= 1; if (remaining === 0) { db.close(); resolve(); } };
            } catch (e) {
              remaining -= 1;
              if (remaining === 0) { db.close(); resolve(); }
            }
          });
        };
        req.onerror = () => resolve();
        req.onblocked = () => resolve();
      });
    }
  } catch (e) {}
  return results;
}
"""
