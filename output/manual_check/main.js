// main.js — internal site runtime (generated)
(function () {
  'use strict';
  var MENU = [
    { path: "/docs/upstream-sample-channel/", label: "Upstream Sample Channel" },
    { path: "/notes/archive-region/", label: "Archive Region" },
    { path: "/wiki/shard-schedule/", label: "Shard Schedule" },
    { path: "/docs/change-signal-anchor/", label: "Change Signal Anchor" },
    { path: "/wiki/rule-change/", label: "Rule Change" },
    { path: "/wiki/digest-session-ledger/", label: "Digest Session Ledger" },
    { path: "/wiki/domain-queue-backoff/", label: "Domain Queue Backoff" }
  ];
  function buildNav() {
    var nav = document.querySelector('[data-extra-nav]');
    if (!nav || nav.childNodes.length) return;
    for (var i = 0; i < MENU.length; i++) {
      var a = document.createElement('a');
      a.href = MENU[i].path;
      a.textContent = MENU[i].label;
      nav.appendChild(a);
    }
  }
  if (document.readyState !== 'loading') { buildNav(); }
  else { document.addEventListener('DOMContentLoaded', buildNav); }
})();
