package com.shadowverse.health;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * 多服务器地址配置（内网 NAS + frp 外网映射等，按序探测自动切换）。
 *
 * - "server_urls"：换行分隔的完整清单（连接设置对话框每行一个，靠前优先）；
 * - "server_url"：当前活动地址——探测通了谁就写谁。所有后台上报（秤/离线队列/
 *   三星直读/提醒）和快照拦截仍读活动地址，老安装只有 server_url 时行为不变。
 * - resolve() 做网络探测（GET /healthz，4s 超时），只能在后台线程调；
 *   活动地址仍通时零切换成本（只多一次探测），不通才按清单顺序找下一个。
 * - frp 等入口开 HTTP Basic 验证时地址写成 http://用户:密码@域名 ——
 *   bare() 去凭据后给 WebView/同源比较用，basicAuthHeader() 生成验证头；
 *   API 上报的 app token 此时挪到 X-Ingest-Token 头（服务端两个头都认）。
 * - 注意：登录 cookie 按 origin 隔离，每个地址首次使用需各自登录一次。
 */
final class ServerConfig {

    private static final String PREFS_NAME = "shell";
    static final String KEY_ACTIVE = "server_url";
    static final String KEY_URLS = "server_urls";
    static final String DEFAULT_SERVER_URL = "http://192.168.1.100:8080";

    private ServerConfig() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    /** 配置的地址清单（已规范化、按优先序）；从未配置返回空表。 */
    static List<String> urls(Context ctx) {
        List<String> out = new ArrayList<>();
        String raw = prefs(ctx).getString(KEY_URLS, "");
        for (String line : raw.split("\n")) {
            String url = normalizeUrl(line);
            if (!url.isEmpty() && !out.contains(url)) {
                out.add(url);
            }
        }
        if (out.isEmpty()) {
            // 老安装升级：只有单地址 server_url
            String single = normalizeUrl(prefs(ctx).getString(KEY_ACTIVE, ""));
            if (!single.isEmpty()) {
                out.add(single);
            }
        }
        return out;
    }

    /** 当前活动地址；从未配置返回 ""。 */
    static String active(Context ctx) {
        String url = normalizeUrl(prefs(ctx).getString(KEY_ACTIVE, ""));
        if (!url.isEmpty()) {
            return url;
        }
        List<String> all = urls(ctx);
        return all.isEmpty() ? "" : all.get(0);
    }

    /** 保存清单并把活动地址重置为第一个（用户改配置 = 优先序重新生效）。 */
    static void save(Context ctx, List<String> list) {
        StringBuilder sb = new StringBuilder();
        for (String u : list) {
            if (sb.length() > 0) {
                sb.append('\n');
            }
            sb.append(u);
        }
        prefs(ctx).edit()
                .putString(KEY_URLS, sb.toString())
                .putString(KEY_ACTIVE, list.isEmpty() ? "" : list.get(0))
                .apply();
    }

    static void setActive(Context ctx, String url) {
        prefs(ctx).edit().putString(KEY_ACTIVE, url).apply();
    }

    /**
     * 找可达服务器（阻塞探测，仅后台线程）：活动地址优先，不通按清单顺序；
     * 探测通了就落为活动地址并返回；全不通/未配置返回 ""。
     */
    static String resolve(Context ctx) {
        String current = active(ctx);
        if (!current.isEmpty() && probe(current)) {
            return current;
        }
        for (String candidate : urls(ctx)) {
            if (candidate.equals(current)) {
                continue;  // 刚探过
            }
            if (probe(candidate)) {
                setActive(ctx, candidate);
                return candidate;
            }
        }
        return "";
    }

    /** 后台上报用：可达地址优先，全不通退回活动地址走各自原有的失败重试路径。 */
    static String resolveOrActive(Context ctx) {
        String ok = resolve(ctx);
        return ok.isEmpty() ? active(ctx) : ok;
    }

    /** GET /healthz 探活（连接/读取各 4s；地址带凭据时附 Basic 头）。 */
    static boolean probe(String server) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(bare(server) + "/healthz").openConnection();
            String auth = basicAuthHeader(server);
            if (auth != null) {
                conn.setRequestProperty("Authorization", auth);
            }
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(4000);
            return conn.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ---- URL 内嵌凭据（frp Basic 验证：http://用户:密码@域名） ----

    /** 去掉 user:pass@ 部分——WebView 加载/展示/同源与前缀比较都用裸地址。 */
    static String bare(String url) {
        if (url == null || url.isEmpty()) {
            return url;
        }
        int scheme = url.indexOf("://");
        if (scheme < 0) {
            return url;
        }
        int pathStart = url.indexOf('/', scheme + 3);
        int authorityEnd = pathStart < 0 ? url.length() : pathStart;
        int at = url.lastIndexOf('@', authorityEnd - 1);
        if (at <= scheme) {
            return url;
        }
        return url.substring(0, scheme + 3) + url.substring(at + 1);
    }

    /** URL 里的 Basic 验证头；无凭据返回 null。 */
    static String basicAuthHeader(String url) {
        if (url == null || url.isEmpty()) {
            return null;
        }
        String info;
        try {
            info = Uri.parse(url).getUserInfo();
        } catch (Exception e) {
            return null;
        }
        if (info == null || info.isEmpty()) {
            return null;
        }
        byte[] decoded = Uri.decode(info).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        return "Basic " + android.util.Base64.encodeToString(decoded, android.util.Base64.NO_WRAP);
    }

    /** 任意 URL 命中清单里带凭据的服务器时给出 Basic 头（DownloadManager 下载用）。 */
    static String basicAuthHeaderForUrl(Context ctx, String url) {
        if (url == null || url.isEmpty()) {
            return null;
        }
        String server = matching(ctx, Uri.parse(url));
        return server == null ? null : basicAuthHeader(server);
    }

    /** WebView HTTP Basic 认证挑战：按 host 找带凭据的配置项 → [用户名, 密码]。 */
    static String[] credentialsForHost(Context ctx, String host) {
        if (host == null || host.isEmpty()) {
            return null;
        }
        for (String server : urls(ctx)) {
            Uri u = Uri.parse(server);
            String info = u.getUserInfo();
            if (info == null || info.isEmpty() || !host.equalsIgnoreCase(u.getHost())) {
                continue;
            }
            String decoded = Uri.decode(info);
            int i = decoded.indexOf(':');
            return i >= 0
                    ? new String[]{decoded.substring(0, i), decoded.substring(i + 1)}
                    : new String[]{decoded, ""};
        }
        return null;
    }

    /** 请求命中清单里哪个服务器（快照拦截用）；都不匹配返回 null。 */
    static String matching(Context ctx, Uri requestUri) {
        for (String server : urls(ctx)) {
            if (SnapshotCache.sameOrigin(requestUri, server)) {
                return server;
            }
        }
        return null;
    }

    /**
     * 把 url 从清单里的旧服务器换到 target 服务器（保留应用内路径，含子路径前缀）；
     * url 已在 target 上原样返回；不属于任何已知服务器返回 null。
     * url 来自 WebView（永远是裸地址），配置项可能带凭据——一律按 bare 比较，
     * 返回值也是裸地址（loadUrl 不吃 userinfo）。
     */
    static String rebase(Context ctx, String url, String target) {
        if (url == null || url.isEmpty()) {
            return null;
        }
        String bareTarget = bare(target);
        if (url.equals(bareTarget) || url.startsWith(bareTarget + "/")) {
            return url;
        }
        for (String server : urls(ctx)) {
            String s = bare(server);
            if (url.equals(s)) {
                return bareTarget;
            }
            if (url.startsWith(s + "/")) {
                return bareTarget + url.substring(s.length());
            }
        }
        return null;
    }

    /** 补协议、去尾斜杠；空输入返回 ""。 */
    static String normalizeUrl(String raw) {
        String url = raw == null ? "" : raw.trim();
        if (url.isEmpty()) {
            return "";
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }
}
