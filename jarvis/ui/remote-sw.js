/* JARVIS Handy-App: Service Worker für Push-Nachrichten */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

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
