/* Service Worker：讓 App 可安裝，並在離線時顯示離線頁。
 * Gradio 的互動（資料下載、LINE 廣播）一定需要網路，所以不快取 API 與動態內容。 */
const VERSION = "v1";
const CACHE = `tainan-line-${VERSION}`;
const PRECACHE = [
  "/offline",
  "/manifest.json",
  "/pwa/icon-192.png",
  "/pwa/icon-512.png",
  "/pwa/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
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
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 頁面導覽：網路優先，失敗時回離線頁
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }

  // 圖示與 manifest：快取優先，背景更新
  if (url.pathname.startsWith("/pwa/") || url.pathname === "/manifest.json") {
    event.respondWith(
      caches.match(req).then((hit) => {
        const refresh = fetch(req)
          .then((res) => {
            if (res.ok) caches.open(CACHE).then((c) => c.put(req, res.clone()));
            return res;
          })
          .catch(() => hit);
        return hit || refresh;
      })
    );
  }
  // 其餘（含 /gradio_api、串流、上傳）一律不攔截，直接走網路
});
