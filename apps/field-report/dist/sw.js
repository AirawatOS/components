// ADR-0082 S2 — Field Report offline shell cache. Precache the app shell on install; serve cache-first for
// same-scope GETs so the capture form loads with NO network. Governed writes still go through /bridge (never
// cached) — an offline submit is queued in IndexedDB by the page and synced on reconnect.
const CACHE = "fieldkit-v1";
const SHELL = ["./", "./index.html"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.indexOf("/bridge/") !== -1) return;   // never cache bridge/API calls
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      if (res && res.ok && res.type === "basic") { const cp = res.clone(); caches.open(CACHE).then(c => c.put(e.request, cp)); }
      return res;
    }).catch(() => caches.match("./index.html")))                                       // offline nav → cached shell
  );
});
