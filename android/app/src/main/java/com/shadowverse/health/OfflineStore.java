package com.shadowverse.health;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;

import androidx.work.Constraints;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;

/**
 * 离线记录队列 + bootstrap 缓存（docs/offline-plan.md 阶段二）。
 *
 * - 队列存 SharedPreferences JSON 数组（照 ScaleScanService.KEY_QUEUE 模式，
 *   上限 500 条），元素即服务端契约 {"type","client_id","date","payload"}；
 *   client_id 由壳生成 UUID，date 取壳本地日期（时钟即真相）。
 * - 补发走 POST /api/ingest/offline（Bearer 同秤/手表）：服务端 import_raw
 *   幂等去重，重放安全；HTTP 2xx 即视为整批成功（单条坏 payload 服务端
 *   留档隔离，重发也修不好，不阻塞队列）。
 * - drain 不在锁内做网络 IO：先快照、POST、成功后按 client_id 摘除——
 *   POST 期间新入队的记录不丢也不被误删。
 * - bootstrap（active 习惯清单 + 常用训练类型）在每次成功加载服务器页面后
 *   刷新缓存，离线本地页用缓存副本渲染打卡清单。
 */
final class OfflineStore {

    private static final String TAG = "OfflineStore";
    private static final String PREFS_NAME = "shell";
    private static final String KEY_QUEUE = "offline_queue";
    private static final String KEY_QUEUE_SIZE = "offline_queue_size";  // 免得数个数也要解析全队列
    private static final String KEY_BOOTSTRAP = "offline_bootstrap";
    private static final String KEY_BOOTSTRAP_AT = "offline_bootstrap_at";
    private static final int QUEUE_MAX = 500;
    private static final String WORK_NAME = "offline-flush";
    private static final String CHANNEL_ID = "offline";
    private static final int NOTIFICATION_ID = 3001;
    private static final Object LOCK = new Object();

    private OfflineStore() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    private static JSONArray loadQueue(Context ctx) {
        try {
            return new JSONArray(prefs(ctx).getString(KEY_QUEUE, "[]"));
        } catch (JSONException e) {
            return new JSONArray();  // 损坏的队列直接重置（与秤队列同策略）
        }
    }

    private static void saveQueue(Context ctx, JSONArray arr) {
        prefs(ctx).edit()
                .putString(KEY_QUEUE, arr.toString())
                .putInt(KEY_QUEUE_SIZE, arr.length())
                .apply();
    }

    /** 服务端契约 {"type","client_id","date","payload"}（enqueue/setQueuedHabit 共用）。 */
    private static JSONObject newRecord(String type, String date, JSONObject payload)
            throws JSONException {
        JSONObject rec = new JSONObject();
        rec.put("type", type);
        rec.put("client_id", UUID.randomUUID().toString());
        rec.put("date", date);
        rec.put("payload", payload);
        return rec;
    }

    // ---- 队列写入（WebView JS 桥线程调用） ----

    /** 入队一条离线记录；返回新队列长度，payload 非法返回 -1。
     *  队列从空转非空时顺带注册补发任务（KEEP 幂等，不必每条都去敲 WorkManager）。 */
    static int enqueue(Context ctx, String type, String date, String payloadJson) {
        JSONObject payload;
        try {
            payload = new JSONObject(payloadJson);
        } catch (JSONException e) {
            Log.w(TAG, "enqueue 忽略非法 payload: " + e);
            return -1;
        }
        boolean wasEmpty;
        int size;
        synchronized (LOCK) {
            JSONArray arr = loadQueue(ctx);
            wasEmpty = arr.length() == 0;
            try {
                arr.put(newRecord(type, date, payload));
            } catch (JSONException e) {
                return -1;
            }
            while (arr.length() > QUEUE_MAX) {
                arr.remove(0);
            }
            saveQueue(ctx, arr);
            size = arr.length();
        }
        if (wasEmpty) {
            scheduleFlush(ctx);
        }
        return size;
    }

    /**
     * habit 声明式覆盖：同日同习惯队列里只留一条（多次点按更新计数而不是堆记录，
     * 服务端 ON CONFLICT DO NOTHING 才不会把重复条目变成先到先得的噪音）；
     * doneCount<=0 = 撤销暂存（只删不留）。返回新队列长度。
     */
    static int setQueuedHabit(Context ctx, int habitId, String date, int doneCount) {
        boolean wasEmpty;
        int size;
        synchronized (LOCK) {
            JSONArray arr = loadQueue(ctx);
            wasEmpty = arr.length() == 0;
            JSONArray kept = new JSONArray();
            for (int i = 0; i < arr.length(); i++) {
                JSONObject rec = arr.optJSONObject(i);
                if (rec != null && "habit".equals(rec.optString("type"))
                        && date.equals(rec.optString("date"))
                        && rec.optJSONObject("payload") != null
                        && rec.optJSONObject("payload").optInt("habit_id", -1) == habitId) {
                    continue;  // 移除旧条目
                }
                kept.put(rec);
            }
            if (doneCount > 0) {
                try {
                    JSONObject payload = new JSONObject();
                    payload.put("habit_id", habitId);
                    payload.put("done_count", doneCount);
                    kept.put(newRecord("habit", date, payload));
                } catch (JSONException ignored) {
                }
            }
            while (kept.length() > QUEUE_MAX) {
                kept.remove(0);
            }
            saveQueue(ctx, kept);
            size = kept.length();
        }
        if (wasEmpty && size > 0) {
            scheduleFlush(ctx);
        }
        return size;
    }

    /** 某日已暂存的打卡：{habit_id: done_count}（本地页渲染勾选态用）。 */
    static String queuedHabits(Context ctx, String date) {
        JSONObject out = new JSONObject();
        synchronized (LOCK) {
            JSONArray arr = loadQueue(ctx);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject rec = arr.optJSONObject(i);
                if (rec == null || !"habit".equals(rec.optString("type"))
                        || !date.equals(rec.optString("date"))) {
                    continue;
                }
                JSONObject payload = rec.optJSONObject("payload");
                if (payload == null) {
                    continue;
                }
                int id = payload.optInt("habit_id", -1);
                if (id > 0) {
                    try {
                        out.put(String.valueOf(id), payload.optInt("done_count", 1));
                    } catch (JSONException ignored) {
                    }
                }
            }
        }
        return out.toString();
    }

    static int queueSize(Context ctx) {
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            if (p.contains(KEY_QUEUE_SIZE)) {
                return p.getInt(KEY_QUEUE_SIZE, 0);
            }
            int n = loadQueue(ctx).length();  // 旧安装升级：补写一次计数
            p.edit().putInt(KEY_QUEUE_SIZE, n).apply();
            return n;
        }
    }

    // ---- 补发 ----

    /**
     * 补发整个队列（IO/Worker 线程调用）：快照 → 单次批量 POST → 按 client_id 摘除。
     * 返回成功补发条数；0 = 无积压；-1 = 网络/服务端失败（下轮再试）；
     * -2 = 有积压但服务器/Token 未配置（重试无意义，得提示用户去配置）。
     */
    static int drain(Context ctx) {
        // 多服务器：探测可达地址（Worker 线程，允许阻塞）；全不通退回活动地址走失败重试
        String server = ServerConfig.resolveOrActive(ctx);
        String token = prefs(ctx).getString("ingest_token", "");
        JSONArray snapshot;
        synchronized (LOCK) {
            snapshot = loadQueue(ctx);
        }
        if (snapshot.length() == 0) {
            return 0;
        }
        if (server.isEmpty() || token.isEmpty()) {
            return -2;
        }
        JSONObject body = new JSONObject();
        try {
            body.put("records", snapshot);
        } catch (JSONException e) {
            return -1;
        }
        if (!postJson(server + "/api/ingest/offline", token, body.toString())) {
            return -1;
        }
        Set<String> sentIds = new HashSet<>();
        for (int i = 0; i < snapshot.length(); i++) {
            JSONObject rec = snapshot.optJSONObject(i);
            if (rec != null) {
                sentIds.add(rec.optString("client_id"));
            }
        }
        synchronized (LOCK) {
            JSONArray current = loadQueue(ctx);
            JSONArray remaining = new JSONArray();
            for (int i = 0; i < current.length(); i++) {
                JSONObject rec = current.optJSONObject(i);
                if (rec == null || sentIds.contains(rec.optString("client_id"))) {
                    continue;
                }
                remaining.put(rec);  // POST 期间新入队的保留，下轮补发
            }
            saveQueue(ctx, remaining);
        }
        Log.i(TAG, "离线队列补发 " + snapshot.length() + " 条");
        return snapshot.length();
    }

    /** 注册一次性补发任务：约束联网即跑（KEEP：已有排队任务则不重复）。 */
    static void scheduleFlush(Context ctx) {
        OneTimeWorkRequest req = new OneTimeWorkRequest.Builder(OfflineFlushWorker.class)
                .setConstraints(new Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build();
        WorkManager.getInstance(ctx).enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, req);
    }

    /** 通知栏「已补同步 N 条离线记录」；13+ 无通知权限则静默跳过。 */
    static void notifySynced(Context ctx, int n) {
        notify(ctx, "离线记录已补同步", "已补同步 " + n + " 条离线记录");
    }

    /** 有积压但服务器/Token 未配置：不提示的话队列会无声躺一辈子。 */
    static void notifyConfigMissing(Context ctx, int pending) {
        notify(ctx, "离线记录无法补同步",
                "有 " + pending + " 条离线记录待同步：请在连接设置里填服务器地址和 INGEST_TOKEN");
    }

    private static void notify(Context ctx, String title, String text) {
        if (Build.VERSION.SDK_INT >= 33
                && ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        try {
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                NotificationChannel ch = new NotificationChannel(
                        CHANNEL_ID, "离线补同步", NotificationManager.IMPORTANCE_DEFAULT);
                ch.setDescription("离线暂存记录补发结果");
                nm.createNotificationChannel(ch);
            }
            PendingIntent pi = PendingIntent.getActivity(
                    ctx, 2, new Intent(ctx, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
            Notification notification = new Notification.Builder(ctx, CHANNEL_ID)
                    .setContentTitle(title)
                    .setContentText(text)
                    .setSmallIcon(R.mipmap.ic_launcher)
                    .setContentIntent(pi)
                    .setAutoCancel(true)
                    .build();
            nm.notify(NOTIFICATION_ID, notification);
        } catch (Exception e) {
            Log.w(TAG, "补同步通知失败: " + e);
        }
    }

    // ---- bootstrap 缓存 ----

    static String bootstrap(Context ctx) {
        return prefs(ctx).getString(KEY_BOOTSTRAP, "");
    }

    static long bootstrapAgeMs(Context ctx) {
        long at = prefs(ctx).getLong(KEY_BOOTSTRAP_AT, 0);
        return at == 0 ? Long.MAX_VALUE : System.currentTimeMillis() - at;
    }

    /** 拉取并缓存 /api/offline/bootstrap（IO 线程调用）；成功返回 true。 */
    static boolean fetchBootstrap(Context ctx) {
        String server = ServerConfig.resolveOrActive(ctx);  // IO 线程调用，允许探测
        String token = prefs(ctx).getString("ingest_token", "");
        if (server.isEmpty() || token.isEmpty()) {
            return false;
        }
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(server + "/api/offline/bootstrap").openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setRequestProperty("Authorization", "Bearer " + token);
            if (conn.getResponseCode() != 200) {
                return false;
            }
            String bodyText = new String(
                    SnapshotCache.readAll(conn.getInputStream()), StandardCharsets.UTF_8);
            new JSONObject(bodyText);  // 校验是合法 JSON 再入缓存
            prefs(ctx).edit()
                    .putString(KEY_BOOTSTRAP, bodyText)
                    .putLong(KEY_BOOTSTRAP_AT, System.currentTimeMillis())
                    .apply();
            Log.i(TAG, "bootstrap 缓存已刷新");
            return true;
        } catch (Exception e) {
            Log.w(TAG, "bootstrap 拉取失败: " + e);
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ---- HTTP ----

    private static boolean postJson(String url, String token, String json) {
        // 批量补发一次可带上百条记录，读超时给足 15s
        return HttpPost.postJson(TAG, url, token, json, 8000, 15000);
    }
}
