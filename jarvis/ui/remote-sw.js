/* JARVIS Handy-App: Service Worker für Push-Nachrichten */
const SHELL = "jarvis-app-v1";
self.addEventListener("install", e => e.waitUntil(caches.open(SHELL).then(c => c.addAll(["/", "/icon.png"])).catch(() => {}).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

// App-Seite: immer frisch vom PC – ist er aus, die zuletzt gespeicherte Version (Jarvis ohne PC)
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || !(req.mode === "navigate" || url.pathname === "/icon.png")) return;
  e.respondWith((async () => {
    const cache = await caches.open(SHELL);
    try {
      const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 5000);
      const res = await fetch(url.href, { signal: ctl.signal, cache: "no-store", credentials: "same-origin" }); clearTimeout(tm);
      if (res.ok) cache.put(url.pathname === "/icon.png" ? "/icon.png" : "/", res.clone());
      return res;
    } catch {
      return (await cache.match(url.pathname === "/icon.png" ? "/icon.png" : "/")) || Response.error();
    }
  })());
});

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : "" }; }
  // iOS verlangt, dass jede Push-Nachricht sichtbar angezeigt wird
  e.waitUntil(self.registration.showNotification(d.title || "JARVIS", {
    body: d.body || "", icon: "/icon.png", badge: "/icon.png", tag: d.tag || undefined, data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) { if ("focus" in c) return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
