package com.shadowverse.health;

import android.content.Context;
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
import java.util.Comparator;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 原生页面快照缓存（docs/offline-plan.md 阶段三）：离线可读。
 *
 * shouldInterceptRequest 把**同源成功 GET 响应**（导航 HTML + /fragments/* +
 * /static/*）写壳内磁盘缓存（URL sha1 做键，LRU 上限 ~20MB）：
 * - 在线：代理取回 → 落缓存 → 原样回给 WebView（永远是最后一次成功响应，无 TTL）
 * - 离线（连接异常）：同 URL 回放缓存；主文档 HTML 注入「📴 离线快照 · 截至 HH:MM」
 *   顶部横幅（今日页的计划卡/习惯/饮食区块都是 hx-get 片段，不缓存 /fragments/*
 *   离线页面就只剩骨架）；无缓存返回 null → WebView 原生加载失败 →
 *   onReceivedError 回落本地启动页
 * - 只代理 GET；POST/PUT/DELETE 永不拦截（离线写失败由 base.html 全局 toast 报错，
 *   引导回本地页记录）。302/304 等非 200 响应返回 null 交回 WebView 原生处理
 *   （会二次请求，局域网开销可忽略）；Content-Disposition attachment（CSV 导出）
 *   同样放行，交给 DownloadListener。
 */
final class SnapshotCache {

    private static final String TAG = "SnapshotCache";
    private static final String DIR_NAME = "snapshots";
    private static final long MAX_TOTAL_BYTES = 20L * 1024 * 1024;
    private static final long MAX_ENTRY_BYTES = 3L * 1024 * 1024;  // 单条超限只回放不落盘
    private static final Object EVICT_LOCK = new Object();

    private SnapshotCache() {
    }

    /** 拦截入口：不该管的请求一律返回 null（WebView 原生加载）。 */
    static WebResourceResponse intercept(Context ctx, WebResourceRequest request, String serverUrl) {
        if (serverUrl == null || serverUrl.isEmpty()
                || !"GET".equalsIgnoreCase(request.getMethod())) {
            return null;
        }
        String url = request.getUrl().toString();
        if (!url.equals(serverUrl) && !url.startsWith(serverUrl + "/")) {
            return null;  // 只管同源
        }
        String path = request.getUrl().getPath();
        if (path == null || path.isEmpty()) {
            path = "/";
        }
        boolean cacheable = request.isForMainFrame()
                || path.startsWith("/fragments/") || path.startsWith("/static/");
        if (!cacheable
                || path.startsWith("/api/") || path.startsWith("/uploads/")
                || path.equals("/healthz") || path.equals("/login") || path.equals("/logout")
                || path.equals("/sw.js")) {
            return null;  // 探测/鉴权/照片/下载类不缓存
        }

        try {
            return fetchAndStore(ctx, request, url);
        } catch (IOException e) {
            // 离线（或服务器不可达）：回放最后一次成功响应
            WebResourceResponse cached = replay(ctx, url, request.isForMainFrame());
            if (cached != null) {
                Log.i(TAG, "离线回放快照: " + path);
            }
            return cached;  // null → WebView 原生失败 → onReceivedError → 本地启动页
        } catch (RuntimeException e) {
            Log.w(TAG, "快照拦截异常，回退原生加载: " + e);
            return null;  // 拦截层任何意外都不能拖垮 WebView
        }
    }

    // ---- 在线：代理取回 + 落缓存 ----

    private static WebResourceResponse fetchAndStore(
            Context ctx, WebResourceRequest request, String url) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(15000);
            conn.setInstanceFollowRedirects(false);  // 302 交回 WebView，URL 语义不错位
            Map<String, String> reqHeaders = request.getRequestHeaders();
            if (reqHeaders != null) {
                for (Map.Entry<String, String> h : reqHeaders.entrySet()) {
                    String k = h.getKey();
                    // 压缩与条件请求都跳过：确保拿到完整 200 明文体可落盘
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

            int code = conn.getResponseCode();
            if (code != 200) {
                return null;  // 重定向/错误：WebView 原生处理（二次请求可接受）
            }
            String disposition = conn.getHeaderField("Content-Disposition");
            if (disposition != null && disposition.toLowerCase(Locale.US).contains("attachment")) {
                return null;  // CSV 导出等下载：交给 DownloadListener
            }
            // 会话续期等 Set-Cookie 不能丢
            List<String> setCookies = conn.getHeaderFields().get("Set-Cookie");
            if (setCookies != null) {
                for (String sc : setCookies) {
                    CookieManager.getInstance().setCookie(url, sc);
                }
            }

            byte[] body = readAll(conn.getInputStream());
            String contentType = conn.getContentType();
            String mime = mimeOf(contentType);
            String encoding = charsetOf(contentType);

            if (body.length <= MAX_ENTRY_BYTES) {
                store(ctx, url, mime, encoding, body);
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

    // ---- 离线：回放缓存 ----

    private static WebResourceResponse replay(Context ctx, String url, boolean mainFrame) {
        File meta = metaFile(ctx, url);
        File bin = binFile(ctx, url);
        if (!meta.exists() || !bin.exists()) {
            return null;
        }
        try {
            JSONObject m = new JSONObject(
                    new String(readAll(new FileInputStream(meta)), StandardCharsets.UTF_8));
            byte[] body = readAll(new FileInputStream(bin));
            String mime = m.optString("mime", "text/html");
            String encoding = m.optString("encoding", "");
            long ts = m.optLong("ts", 0);
            if (mainFrame && mime.equals("text/html")) {
                body = injectBanner(body, encoding, ts);
            }
            long now = System.currentTimeMillis();
            // LRU 触点：回放算一次使用
            //noinspection ResultOfMethodCallIgnored
            meta.setLastModified(now);
            //noinspection ResultOfMethodCallIgnored
            bin.setLastModified(now);
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

    /** 主文档顶部注入离线横幅：「📴 离线快照 · 截至 HH:MM」（跨天显示日期）。 */
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
        String banner = "<div style=\"position:sticky;top:0;z-index:9999;background:#78350f;"
                + "color:#fde68a;font-size:13px;text-align:center;padding:6px 10px\">"
                + "📴 离线快照 · 截至 " + when + "</div>";
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

    // ---- 磁盘存取（键 = URL sha1；.meta JSON + .bin 响应体，写临时文件后改名） ----

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

    private static void store(Context ctx, String url, String mime, String encoding, byte[] body) {
        try {
            JSONObject m = new JSONObject();
            m.put("url", url);
            m.put("mime", mime);
            m.put("encoding", encoding == null ? "" : encoding);
            m.put("ts", System.currentTimeMillis());
            writeAtomic(binFile(ctx, url), body);
            writeAtomic(metaFile(ctx, url), m.toString().getBytes(StandardCharsets.UTF_8));
            evictIfNeeded(ctx);
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

    /** 总量超 20MB 时按 lastModified 从旧到新成对删除（.meta/.bin 同键同删）。 */
    private static void evictIfNeeded(Context ctx) {
        synchronized (EVICT_LOCK) {
            File[] files = dir(ctx).listFiles();
            if (files == null) {
                return;
            }
            long total = 0;
            for (File f : files) {
                total += f.length();
            }
            if (total <= MAX_TOTAL_BYTES) {
                return;
            }
            Arrays.sort(files, Comparator.comparingLong(File::lastModified));
            for (File f : files) {
                if (total <= MAX_TOTAL_BYTES) {
                    break;
                }
                String name = f.getName();
                if (name.endsWith(".meta") || name.endsWith(".bin")) {
                    String stem = name.substring(0, name.lastIndexOf('.'));
                    for (String ext : new String[]{".meta", ".bin"}) {
                        File pair = new File(f.getParentFile(), stem + ext);
                        if (pair.exists()) {
                            total -= pair.length();
                            //noinspection ResultOfMethodCallIgnored
                            pair.delete();
                        }
                    }
                } else {
                    total -= f.length();  // 残留 .tmp 等
                    //noinspection ResultOfMethodCallIgnored
                    f.delete();
                }
            }
        }
    }

    // ---- 小工具 ----

    private static byte[] readAll(InputStream in) throws IOException {
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
