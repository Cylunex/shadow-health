package com.shadowverse.health;

import android.content.Context;
import android.net.Uri;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 原生页面快照缓存（docs/offline-plan.md 阶段三）：离线可读。
 *
 * shouldInterceptRequest 把**同源成功 GET 响应**（导航 HTML + /fragments/* +
 * /static/*）写壳内磁盘缓存（URL sha1 做键，LRU 上限 ~20MB）：
 * - 在线：代理取回 → 落缓存 → 原样回给 WebView（永远是最后一次成功响应，无 TTL）；
 *   带 ETag/Last-Modified 复验，304 直接回放磁盘副本（不重传不重写，省流量省闪存）
 * - 离线（**连接阶段**失败：拒连/无路由/连接超时）：同 URL 回放缓存；主文档 HTML
 *   注入「📴 离线快照 · 截至 HH:MM」横幅 + 「去离线记录」按钮；无缓存返回 null →
 *   WebView 原生加载失败 → onReceivedError 回落本地启动页。连接失败后 10s 内
 *   直接走回放（负缓存），避免每个子资源都干等超时
 * - **读取阶段**失败（连上了但响应慢/断流）不当离线：返回 null 交 WebView 原生
 *   重试——在线的慢页面不该被换成昨天的快照
 * - 只代理 GET；POST/PUT/DELETE 永不拦截。302/304 之外的非 200 返回 null 交回
 *   WebView 原生处理（会二次请求，局域网开销可忽略）；Content-Disposition
 *   attachment（备份 zip 等）放行给 DownloadListener，/settings/backup 构建
 *   昂贵，直接列入排除名单不代理
 *
 * 同源判定经 Uri 规范化（scheme/host 忽略大小写、补默认端口）：WebView 会把
 * 请求 URL 规范化（小写 host、去默认端口），裸字符串前缀比对会静默漏配。
 * 磁盘读写/淘汰统一挂 IO_LOCK：并发写同键会交错出损坏的快照体。
 *
 * 子路径部署（V3 P1）：serverUrl 带 path 前缀（/shealth）时，路径匹配先剥前缀
 * 再按应用内路径比；同域不在前缀下的请求不代理——否则片段/静态匹配静默失效、
 * 前缀下的 /login 反而会被缓存。
 */
final class SnapshotCache {

    private static final String TAG = "SnapshotCache";
    private static final String DIR_NAME = "snapshots";
    private static final long MAX_TOTAL_BYTES = 20L * 1024 * 1024;
    private static final long MAX_ENTRY_BYTES = 3L * 1024 * 1024;  // 单条超限不代理/不落盘
    private static final long OFFLINE_NEGATIVE_MS = 10_000;  // 连接失败后短窗内直接回放
    private static final Object IO_LOCK = new Object();

    /** 连接失败负缓存的截止时刻（epoch ms）。 */
    private static volatile long offlineUntil;
    /** 缓存目录总字节数（IO_LOCK 内维护；-1 = 未初始化，首次用时扫一遍）。 */
    private static long totalBytes = -1;
    /** 最近以快照回放的主文档 URL：MainActivity.onPageFinished 消费后保持探测循环。 */
    private static final Set<String> replayedMain =
            Collections.newSetFromMap(new ConcurrentHashMap<>());

    /** 连接阶段失败（≈离线）；读取阶段的 IOException 不算。 */
    private static final class ConnectFailure extends IOException {
        ConnectFailure(IOException cause) {
            super(cause);
        }
    }

    private SnapshotCache() {
    }

    /** onPageFinished 用：该 URL 是否刚以离线快照回放（消费一次）。 */
    static boolean consumeReplayedMain(String url) {
        return url != null && replayedMain.remove(url);
    }

    /** 拦截入口：不该管的请求一律返回 null（WebView 原生加载）。 */
    static WebResourceResponse intercept(Context ctx, WebResourceRequest request, String serverUrl) {
        if (serverUrl == null || serverUrl.isEmpty()
                || !"GET".equalsIgnoreCase(request.getMethod())
                || !sameOrigin(request.getUrl(), serverUrl)) {
            return null;
        }
        String path = request.getUrl().getPath();
        if (path == null || path.isEmpty()) {
            path = "/";
        }
        // 子路径部署：serverUrl 可能带 path 前缀（如 http://NAS:55080/shealth）。
        // 下面的 cacheable/排除名单全按应用内路径写，必须先剥前缀再比；
        // 同域但不在前缀下的请求（服务面板、/stock/ 等其他应用）一律不代理。
        String prefix = pathPrefix(serverUrl);
        if (!prefix.isEmpty()) {
            if (path.equals(prefix)) {
                path = "/";  // 不带尾斜杠进站（nginx 会 301，非 200 交回原生）
            } else if (path.startsWith(prefix + "/")) {
                path = path.substring(prefix.length());
            } else {
                return null;
            }
        }
        boolean cacheable = request.isForMainFrame()
                || path.startsWith("/fragments/") || path.startsWith("/static/");
        if (!cacheable
                || path.startsWith("/api/") || path.startsWith("/uploads/")
                || path.equals("/healthz") || path.equals("/login") || path.equals("/logout")
                || path.equals("/sw.js") || path.equals("/settings/backup")) {
            return null;  // 探测/鉴权/照片/重下载不代理（backup 全量打包，代理会白构建一次）
        }

        String url = request.getUrl().toString();
        try {
            if (System.currentTimeMillis() < offlineUntil) {
                // 负缓存窗内：直接回放（无缓存则交回原生），不逐请求干等连接超时
                return replay(ctx, url, request.isForMainFrame());
            }
            return fetchAndStore(ctx, request, url);
        } catch (ConnectFailure e) {
            offlineUntil = System.currentTimeMillis() + OFFLINE_NEGATIVE_MS;
            WebResourceResponse cached = replay(ctx, url, request.isForMainFrame());
            if (cached != null) {
                Log.i(TAG, "离线回放快照: " + path);
            }
            return cached;  // null → WebView 原生失败 → onReceivedError → 本地启动页
        } catch (IOException e) {
            Log.w(TAG, "代理读取失败，交回原生加载: " + e);
            return null;  // 连上但读挂了：在线慢页不回快照
        } catch (RuntimeException e) {
            Log.w(TAG, "快照拦截异常，回退原生加载: " + e);
            return null;  // 拦截层任何意外都不能拖垮 WebView
        }
    }

    /** serverUrl 里的 path 前缀，规范成 "" 或 "/xxx"（无尾斜杠）。 */
    private static String pathPrefix(String serverUrl) {
        String p = Uri.parse(serverUrl).getPath();
        if (p == null) {
            return "";
        }
        while (p.endsWith("/")) {
            p = p.substring(0, p.length() - 1);
        }
        return p;
    }

    /** scheme/host 忽略大小写 + 默认端口补齐后的同源比较。 */
    private static boolean sameOrigin(Uri requestUri, String serverUrl) {
        Uri server = Uri.parse(serverUrl);
        String reqScheme = requestUri.getScheme();
        String srvScheme = server.getScheme();
        String reqHost = requestUri.getHost();
        String srvHost = server.getHost();
        if (reqScheme == null || srvScheme == null || reqHost == null || srvHost == null) {
            return false;
        }
        return reqScheme.equalsIgnoreCase(srvScheme)
                && reqHost.equalsIgnoreCase(srvHost)
                && effectivePort(requestUri) == effectivePort(server);
    }

    private static int effectivePort(Uri uri) {
        int port = uri.getPort();
        if (port != -1) {
            return port;
        }
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }

    // ---- 在线：代理取回 + 落缓存（304 复验直接回放磁盘） ----

    private static WebResourceResponse fetchAndStore(
            Context ctx, WebResourceRequest request, String url) throws IOException {
        JSONObject meta = readMeta(ctx, url);  // 已有快照 → 带条件头复验
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(15000);
            conn.setInstanceFollowRedirects(false);  // 302 交回 WebView，URL 语义不错位
            Map<String, String> reqHeaders = request.getRequestHeaders();
            if (reqHeaders != null) {
                for (Map.Entry<String, String> h : reqHeaders.entrySet()) {
                    String k = h.getKey();
                    // 压缩与 WebView 自己的条件头都跳过：落盘要的是完整明文体
                    if ("Accept-Encoding".equalsIgnoreCase(k)
                            || "If-None-Match".equalsIgnoreCase(k)
                            || "If-Modified-Since".equalsIgnoreCase(k)) {
                        continue;
                    }
                    conn.setRequestProperty(k, h.getValue());
                }
            }
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) {
                conn.setRequestProperty("Cookie", cookie);
            }
            if (meta != null && binFile(ctx, url).exists()) {
                String etag = meta.optString("etag", "");
                if (!etag.isEmpty()) {
                    conn.setRequestProperty("If-None-Match", etag);
                }
                String lastMod = meta.optString("last_modified", "");
                if (!lastMod.isEmpty()) {
                    conn.setRequestProperty("If-Modified-Since", lastMod);
                }
            }

            int code;
            try {
                code = conn.getResponseCode();  // 隐式 connect：拒连/无路由/连接超时在这抛
            } catch (IOException e) {
                throw new ConnectFailure(e);
            }
            offlineUntil = 0;  // 连上了：清负缓存

            if (code == 304) {
                // 内容没变：回放磁盘副本（在线复验，不注横幅），刷新 LRU 触点
                WebResourceResponse fresh = replay(ctx, url, false);
                if (fresh != null) {
                    return fresh;
                }
                return null;  // 副本恰好丢了：交回原生（无条件头，会拿到 200）
            }
            if (code != 200) {
                return null;  // 重定向/错误：WebView 原生处理（二次请求可接受）
            }
            String disposition = conn.getHeaderField("Content-Disposition");
            if (disposition != null && disposition.toLowerCase(Locale.US).contains("attachment")) {
                return null;  // 下载：交给 DownloadListener
            }
            long declared = conn.getContentLengthLong();
            if (declared > MAX_ENTRY_BYTES) {
                return null;  // 大文件（如 /static/*.apk）不整包进内存，交回原生
            }
            // 会话续期等 Set-Cookie 不能丢（uvicorn 发小写头名，必须忽略大小写匹配）
            for (Map.Entry<String, List<String>> h : conn.getHeaderFields().entrySet()) {
                if (h.getKey() != null && "Set-Cookie".equalsIgnoreCase(h.getKey())
                        && h.getValue() != null) {
                    for (String sc : h.getValue()) {
                        CookieManager.getInstance().setCookie(url, sc);
                    }
                }
            }

            byte[] body = readAll(conn.getInputStream());
            String contentType = conn.getContentType();
            String mime = mimeOf(contentType);
            String encoding = charsetOf(contentType);

            if (body.length <= MAX_ENTRY_BYTES) {
                store(ctx, url, mime, encoding, body,
                        conn.getHeaderField("ETag"), conn.getHeaderField("Last-Modified"));
            }
            // HX-Trigger 等响应头透传（htmx 片段靠它做被动刷新）
            Map<String, String> respHeaders = new HashMap<>();
            for (Map.Entry<String, List<String>> h : conn.getHeaderFields().entrySet()) {
                String k = h.getKey();
                if (k == null || h.getValue() == null || h.getValue().isEmpty()
                        || "Set-Cookie".equalsIgnoreCase(k)
                        || "Content-Encoding".equalsIgnoreCase(k)
                        || "Transfer-Encoding".equalsIgnoreCase(k)
                        || "Content-Length".equalsIgnoreCase(k)) {
                    continue;
                }
                respHeaders.put(k, h.getValue().get(0));
            }
            return new WebResourceResponse(
                    mime, encoding, 200, "OK", respHeaders, new ByteArrayInputStream(body));
        } finally {
            conn.disconnect();
        }
    }

    // ---- 回放缓存（离线注横幅；304 复验时 mainFrame=false 不注） ----

    private static WebResourceResponse replay(Context ctx, String url, boolean mainFrame) {
        try {
            JSONObject m;
            byte[] body;
            long now = System.currentTimeMillis();
            synchronized (IO_LOCK) {
                File meta = metaFile(ctx, url);
                File bin = binFile(ctx, url);
                if (!meta.exists() || !bin.exists()) {
                    return null;
                }
                m = new JSONObject(
                        new String(readAll(new FileInputStream(meta)), StandardCharsets.UTF_8));
                body = readAll(new FileInputStream(bin));
                // LRU 触点：回放算一次使用
                //noinspection ResultOfMethodCallIgnored
                meta.setLastModified(now);
                //noinspection ResultOfMethodCallIgnored
                bin.setLastModified(now);
            }
            String mime = m.optString("mime", "text/html");
            String encoding = m.optString("encoding", "");
            long ts = m.optLong("ts", 0);
            if (mainFrame && mime.equals("text/html")) {
                body = injectBanner(body, encoding, ts);
                replayedMain.add(url);
            }
            Map<String, String> headers = new HashMap<>();
            headers.put("X-Offline-Snapshot", String.valueOf(ts));
            return new WebResourceResponse(
                    mime, encoding.isEmpty() ? null : encoding, 200, "OK", headers,
                    new ByteArrayInputStream(body));
        } catch (Exception e) {
            Log.w(TAG, "快照回放失败: " + e);
            return null;
        }
    }

    /** 主文档顶部注入离线横幅（非 sticky——base.html 的页头也钉 top-0，会互相盖）。 */
    private static byte[] injectBanner(byte[] body, String encoding, long ts) {
        Charset cs;
        try {
            cs = encoding == null || encoding.isEmpty()
                    ? StandardCharsets.UTF_8 : Charset.forName(encoding);
        } catch (Exception e) {
            cs = StandardCharsets.UTF_8;
        }
        String html = new String(body, cs);
        String when = formatSnapshotTime(ts);
        String banner = "<div style=\"background:#78350f;color:#fde68a;font-size:13px;"
                + "text-align:center;padding:6px 10px\">📴 离线快照 · 截至 " + when
                + " · <a href=\"javascript:void(0)\" "
                + "onclick=\"window.ShellBridge&&ShellBridge.openOfflinePage()\" "
                + "style=\"color:#fbbf24;text-decoration:underline\">去离线记录</a></div>";
        int bodyTag = html.indexOf("<body");
        if (bodyTag >= 0) {
            int close = html.indexOf('>', bodyTag);
            if (close > 0) {
                html = html.substring(0, close + 1) + banner + html.substring(close + 1);
                return html.getBytes(cs);
            }
        }
        return (banner + html).getBytes(cs);
    }

    private static String formatSnapshotTime(long ts) {
        if (ts <= 0) {
            return "未知时间";
        }
        Date d = new Date(ts);
        String day = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(d);
        String today = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date());
        SimpleDateFormat fmt = day.equals(today)
                ? new SimpleDateFormat("HH:mm", Locale.US)
                : new SimpleDateFormat("M月d日 HH:mm", Locale.CHINA);
        return fmt.format(d);
    }

    // ---- 磁盘存取（键 = URL sha1；.meta JSON + .bin 响应体；全部挂 IO_LOCK） ----

    private static File dir(Context ctx) {
        File d = new File(ctx.getFilesDir(), DIR_NAME);
        //noinspection ResultOfMethodCallIgnored
        d.mkdirs();
        return d;
    }

    private static String keyOf(String url) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] digest = md.digest(url.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                sb.append(String.format(Locale.US, "%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            return String.valueOf(url.hashCode());
        }
    }

    private static File metaFile(Context ctx, String url) {
        return new File(dir(ctx), keyOf(url) + ".meta");
    }

    private static File binFile(Context ctx, String url) {
        return new File(dir(ctx), keyOf(url) + ".bin");
    }

    private static JSONObject readMeta(Context ctx, String url) {
        synchronized (IO_LOCK) {
            File meta = metaFile(ctx, url);
            if (!meta.exists()) {
                return null;
            }
            try {
                return new JSONObject(
                        new String(readAll(new FileInputStream(meta)), StandardCharsets.UTF_8));
            } catch (Exception e) {
                return null;
            }
        }
    }

    private static void store(Context ctx, String url, String mime, String encoding,
                              byte[] body, String etag, String lastModified) {
        try {
            JSONObject m = new JSONObject();
            m.put("url", url);
            m.put("mime", mime);
            m.put("encoding", encoding == null ? "" : encoding);
            m.put("ts", System.currentTimeMillis());
            if (etag != null && !etag.isEmpty()) {
                m.put("etag", etag);
            }
            if (lastModified != null && !lastModified.isEmpty()) {
                m.put("last_modified", lastModified);
            }
            byte[] metaBytes = m.toString().getBytes(StandardCharsets.UTF_8);
            synchronized (IO_LOCK) {
                File bin = binFile(ctx, url);
                File meta = metaFile(ctx, url);
                long before = bin.length() + meta.length();
                writeAtomic(bin, body);
                writeAtomic(meta, metaBytes);
                if (totalBytes >= 0) {
                    totalBytes += (bin.length() + meta.length()) - before;
                }
                evictIfNeededLocked(ctx);
            }
        } catch (JSONException | IOException e) {
            Log.w(TAG, "快照落盘失败: " + e);
        }
    }

    private static void writeAtomic(File target, byte[] data) throws IOException {
        File tmp = new File(target.getPath() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data);
        }
        if (!tmp.renameTo(target)) {
            //noinspection ResultOfMethodCallIgnored
            target.delete();
            if (!tmp.renameTo(target)) {
                throw new IOException("rename failed: " + target.getName());
            }
        }
    }

    /** 总量超 20MB 时按 lastModified 从旧到新成对删除（调用方须持 IO_LOCK）。
     *  总量平时增量记账，只在未初始化时全扫一遍。 */
    private static void evictIfNeededLocked(Context ctx) {
        if (totalBytes < 0) {
            totalBytes = 0;
            File[] seed = dir(ctx).listFiles();
            if (seed != null) {
                for (File f : seed) {
                    totalBytes += f.length();
                }
            }
        }
        if (totalBytes <= MAX_TOTAL_BYTES) {
            return;
        }
        File[] files = dir(ctx).listFiles();
        if (files == null) {
            return;
        }
        Arrays.sort(files, Comparator.comparingLong(File::lastModified));
        for (File f : files) {
            if (totalBytes <= MAX_TOTAL_BYTES) {
                break;
            }
            String name = f.getName();
            if (name.endsWith(".meta") || name.endsWith(".bin")) {
                String stem = name.substring(0, name.lastIndexOf('.'));
                for (String ext : new String[]{".meta", ".bin"}) {
                    File pair = new File(f.getParentFile(), stem + ext);
                    if (pair.exists()) {
                        totalBytes -= pair.length();
                        //noinspection ResultOfMethodCallIgnored
                        pair.delete();
                    }
                }
            } else {
                totalBytes -= f.length();  // 残留 .tmp 等
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            }
        }
        if (totalBytes < 0) {
            totalBytes = 0;
        }
    }

    // ---- 小工具 ----

    /** 读整流（OfflineStore.fetchBootstrap 同用，别再抄一份）。 */
    static byte[] readAll(InputStream in) throws IOException {
        try (InputStream is = in; ByteArrayOutputStream buf = new ByteArrayOutputStream()) {
            byte[] chunk = new byte[8192];
            int len;
            while ((len = is.read(chunk)) > 0) {
                buf.write(chunk, 0, len);
            }
            return buf.toByteArray();
        }
    }

    /** "text/html; charset=utf-8" → "text/html"。 */
    private static String mimeOf(String contentType) {
        if (contentType == null || contentType.isEmpty()) {
            return "text/html";
        }
        int i = contentType.indexOf(';');
        return (i > 0 ? contentType.substring(0, i) : contentType).trim();
    }

    /** Content-Type 里的 charset；没有则 text/* 默认 utf-8、其余 null。 */
    private static String charsetOf(String contentType) {
        if (contentType != null) {
            int i = contentType.toLowerCase(Locale.US).indexOf("charset=");
            if (i >= 0) {
                String cs = contentType.substring(i + 8).trim();
                int j = cs.indexOf(';');
                if (j > 0) {
                    cs = cs.substring(0, j);
                }
                return cs.replace("\"", "").trim();
            }
        }
        return contentType != null && contentType.startsWith("text/") ? "utf-8" : null;
    }
}
