// 簡易 Service Worker：快取 App 外殼資源，離線時顯示提示頁。
// Gradio 的資料互動（SSE / WebSocket / API）一律走網路，不做快取。
const CACHE = "tainan-line-v1";
const SHELL = [
  "/pwa/offline.html",
  "/pwa/icon-192.png",
  "/pwa/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/pwa/offline.html")));
    return;
  }

  const url = new URL(req.url);
  if (SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
