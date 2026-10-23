/* Task Printer service worker: offline app shell + Web Share Target. */
const VERSION = "tp-v3";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/manifest.webmanifest",
  "/icons/icon-96.png",
  "/icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION && k !== "share-target").map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;

  // Share target: stash the shared payload and let the page submit it with its session cookie.
  if (event.request.method === "POST" && url.pathname === "/share") {
    event.respondWith((async () => {
      const form = await event.request.formData();
      const cache = await caches.open("share-target");
      await cache.put("/shared-payload", new Response(form));
      return Response.redirect("/?share-target=1", 303);
    })());
    return;
  }

  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;

  // Network first for the shell so updates land immediately; cache as fallback offline.
  event.respondWith((async () => {
    try {
      const res = await fetch(event.request);
      if (res.ok && (SHELL.includes(url.pathname) || url.pathname.startsWith("/icons/"))) {
        const cache = await caches.open(VERSION);
        cache.put(event.request, res.clone());
      }
      return res;
    } catch {
      const cached = await caches.match(event.request, { ignoreSearch: url.pathname === "/" });
      return cached || caches.match("/");
    }
  })());
});
