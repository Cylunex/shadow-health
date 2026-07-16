/* shadow-health Service Worker
 * 通过 GET /sw.js 路由以根 scope 注册（直接放 /static/ 下 scope 罩不住页面）。
 * 策略：/static/* cache-first（.apk 等大二进制除外）；导航请求 network-first、
 * 离线回退 offline.html；非 GET（POST/PUT/DELETE…）一律 passthrough 不缓存。
 * 升级方式：改 SW_VERSION 即可让旧缓存在 activate 时被清掉。
 * 注意：服务器对 /static 不发 Cache-Control，install/回源必须显式绕过浏览器
 * HTTP 缓存（cache: 'reload'/'no-cache'），否则新版本缓存会固化到旧资源。
 */
const SW_VERSION = 'v19';
const CACHE_NAME = 'shadow-health-' + SW_VERSION;
// 子路径部署（/shealth）时本文件在 <前缀>/sw.js 下发，从自身 URL 反推前缀
const ROOT = self.location.pathname.replace(/\/sw\.js$/, '');
const OFFLINE_URL = ROOT + '/static/offline.html';
const PRECACHE = [
  ROOT + '/static/app.css',
  ROOT + '/static/vendor/htmx.min.js',
  ROOT + '/static/vendor/alpine.min.js',
  ROOT + '/static/vendor/chart.umd.min.js',
  ROOT + '/static/vendor/chartjs-adapter-date-fns.bundle.min.js',
  ROOT + '/static/icon.svg',
  ROOT + '/static/icon-192.png',
  ROOT + '/static/icon-512.png',
  OFFLINE_URL,
];
// 不缓存的静态文件：部署时同名替换的大二进制（缓存后旧包会被继续下发）
const NO_CACHE_EXT = /\.(apk|zip|7z)$/i;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      // cache: 'reload' 强制回源——不然 addAll 可能命中浏览器 HTTP 启发式缓存，
      // 把升级前的旧 app.css/vendor 固化进新版本缓存
      .then((cache) => cache.addAll(PRECACHE.map((u) => new Request(u, { cache: 'reload' }))))
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
  if (url.origin === self.location.origin && url.pathname.startsWith(ROOT + '/static/')) {
    if (NO_CACHE_EXT.test(url.pathname)) return; // APK 等直连网络
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          // 回源同样绕过 HTTP 缓存，进 SW 缓存的必须是服务器当前版本
          fetch(new Request(req, { cache: 'no-cache' })).then((resp) => {
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
