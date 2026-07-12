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
     * POST JSON（Authorization: Bearer）；2xx 返回 true，任何异常/非 2xx 返回 false。
     * 超时按调用方传入（各通道语义不同：秤要快失败入队、三星整包读得慢），
     * tag 用调用方自己的 Log TAG，adb 过滤习惯不变。
     */
    static boolean postJson(String tag, String url, String token, String json,
                            int connectTimeoutMs, int readTimeoutMs) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(connectTimeoutMs);
            conn.setReadTimeout(readTimeoutMs);
            conn.setDoOutput(true);
            conn.setRequestProperty("Authorization", "Bearer " + token);
            conn.setRequestProperty("Content-Type", "application/json");
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int code = conn.getResponseCode();
            Log.i(tag, "POST " + url + " -> " + code);
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
}
