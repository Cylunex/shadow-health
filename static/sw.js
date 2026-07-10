/* shadow-health Service Worker
 * 通过 GET /sw.js 路由以根 scope 注册（直接放 /static/ 下 scope 罩不住页面）。
 * 策略：/static/* cache-first；导航请求 network-first、离线回退 offline.html；
 * 非 GET（POST/PUT/DELETE…）一律 passthrough 不缓存。
 * 升级方式：改 SW_VERSION 即可让旧缓存在 activate 时被清掉。
 */
const SW_VERSION = 'v6';
const CACHE_NAME = 'shadow-health-' + SW_VERSION;
const OFFLINE_URL = '/static/offline.html';
const PRECACHE = [
  '/static/app.css',
  '/static/vendor/htmx.min.js',
  '/static/vendor/alpine.min.js',
  '/static/vendor/chart.umd.min.js',
  '/static/vendor/chartjs-adapter-date-fns.bundle.min.js',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  OFFLINE_URL,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 写操作（POST/PUT/DELETE/PATCH…）全部直连，不缓存
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // 静态资源：cache-first，命中后台不更新，靠 SW_VERSION 整体失效
  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((resp) => {
            if (resp && resp.ok) {
              const copy = resp.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
            }
            return resp;
          })
      )
    );
    return;
  }

  // 页面导航：network-first，网络失败回离线页
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(OFFLINE_URL).then((hit) => hit || Response.error())
      )
    );
    return;
  }
  // 其余 GET（API 等）不做拦截，走默认网络
});
