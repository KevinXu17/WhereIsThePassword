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
  const hasManifestLink = !!document.querySelector('link[rel="manifest" i]');
  const canvasCount = document.querySelectorAll('canvas').length;
  let svgPathNoText = 0;
  document.querySelectorAll('svg').forEach(svg => {
    const hasPath = svg.querySelector('path');
    const hasText = svg.querySelector('text, tspan');
    if (hasPath && !hasText) svgPathNoText += 1;
  });
  const audioEls = [];
  document.querySelectorAll('audio[src], audio source[src]').forEach(a => { const u = abs(a.getAttribute('src')); if (u) audioEls.push(u); });
  const imgSrcs = [];
  document.querySelectorAll('img[src]').forEach(img => { const u = abs(img.getAttribute('src')); if (u) imgSrcs.push(u); });

  return {
    links: Array.from(links),
    metaRefresh,
    forms,
    clickCandidates,
    loadMoreCandidates,
    iframes,
    attrTexts,
    signals: {
      hasManifestLink,
      canvasCount,
      svgPathNoText,
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
  return {
    localStorage: dump(window.localStorage),
    sessionStorage: dump(window.sessionStorage),
    cookie: document.cookie,
  };
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
