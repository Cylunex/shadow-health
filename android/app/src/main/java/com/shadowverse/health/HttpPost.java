package com.shadowverse.health;

import android.util.Log;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Bearer JSON 上报的共享 HTTP 助手（秤监听 / 离线队列补发 / 三星直读三处共用，
 * 原来各抄一份 postJson 只差超时值）。Java 静态方法，Kotlin 直接调。
 */
final class HttpPost {

    private HttpPost() {
    }

    /**
     * POST JSON；2xx 返回 true，任何异常/非 2xx 返回 false。
     * 超时按调用方传入（各通道语义不同：秤要快失败入队、三星整包读得慢），
     * tag 用调用方自己的 Log TAG，adb 过滤习惯不变。
     *
     * 鉴权：URL 不带凭据时 Authorization: Bearer（原语义）；URL 带
     * user:pass@（frp Basic 验证）时 Authorization 让给 Basic，app token
     * 挪到 X-Ingest-Token（服务端 _bearer_reject 两个头都认）。
     */
    static boolean postJson(String tag, String url, String token, String json,
                            int connectTimeoutMs, int readTimeoutMs) {
        HttpURLConnection conn = null;
        String bareUrl = ServerConfig.bare(url);
        try {
            conn = (HttpURLConnection) new URL(bareUrl).openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(connectTimeoutMs);
            conn.setReadTimeout(readTimeoutMs);
            conn.setDoOutput(true);
            applyAuth(conn, url, token);
            conn.setRequestProperty("Content-Type", "application/json");
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int code = conn.getResponseCode();
            Log.i(tag, "POST " + bareUrl + " -> " + code);
            return code >= 200 && code < 300;
        } catch (Exception e) {
            Log.w(tag, "POST failed: " + e);
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /** 按 URL 是否带凭据设置鉴权头（GET 调用方也复用：bootstrap/digest）。 */
    static void applyAuth(HttpURLConnection conn, String url, String token) {
        String basic = ServerConfig.basicAuthHeader(url);
        if (basic != null) {
            conn.setRequestProperty("Authorization", basic);
            conn.setRequestProperty("X-Ingest-Token", token);
        } else {
            conn.setRequestProperty("Authorization", "Bearer " + token);
        }
    }
}
