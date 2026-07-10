package com.shadowverse.health;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelUuid;
import android.util.Log;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 小米体脂秤 2（XMTZC05HM）BLE 前台监听服务。
 *
 * 秤每次测量通过 BLE Service Data（UUID 0x181B）广播体重/阻抗/RTC 时间，
 * 无需配对。本服务常驻低功耗扫描，测量稳定后 POST 到 shadow-health 的
 * /api/ingest/miscale（Bearer token 与 NAS 网关共用）；服务端按
 * (RTC 时间戳 + 体重) 去重，和 NAS 网关同时在线也只记一条。
 *
 * 协议与 gateway/miscale_listener.py 完全一致：
 *   [0] 单位 0x02=kg(×0.005)；非 kg 帧跳过（换算系数无法可靠验证）
 *   [1] 标志 bit1=带阻抗 bit5=已稳定 bit7=离秤
 *   [2:4] 年(LE) [4]月 [5]日 [6]时 [7]分 [8]秒（秤 RTC，本地时间）
 *   [9:11] 阻抗Ω(LE, 0<z<3000 有效)  [11:13] 体重原始值(LE)
 */
public class ScaleScanService extends Service {

    private static final String TAG = "ScaleScan";
    private static final String CHANNEL_ID = "miscale";
    private static final int NOTIFICATION_ID = 1001;
    private static final ParcelUuid UUID_BODY_COMPOSITION =
            ParcelUuid.fromString("0000181b-0000-1000-8000-00805f9b34fb");

    /** 一次测量连播多帧：先纯体重、后带阻抗；等这个窗口再上报，避免丢阻抗。 */
    private static final long SETTLE_MS = 12_000;
    /** 已上报测量的去重缓存保留时长。 */
    private static final long SENT_TTL_MS = 600_000;

    private BluetoothLeScanner scanner;
    private ScanCallback scanCallback;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    // pending/sent 只在主线程访问：BLE 回调与 IO 回调统一 handler.post 回主线程
    private final Map<String, Measurement> pending = new HashMap<>();
    private final Map<String, Long> sent = new HashMap<>();
    private volatile boolean stopped;
    private BroadcastReceiver btStateReceiver;

    private static class Measurement {
        final String key;
        final String tsIso;
        final double weightKg;
        Integer impedance;   // 后续帧可补
        final Runnable flush;

        Measurement(String key, String tsIso, double weightKg, Integer impedance, Runnable flush) {
            this.key = key;
            this.tsIso = tsIso;
            this.weightKg = weightKg;
            this.impedance = impedance;
            this.flush = flush;
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "体脂秤监听", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("后台接收体脂秤蓝牙广播");
            nm.createNotificationChannel(ch);
        }
        // 跟随蓝牙开关：启动时蓝牙没开，开启后自动开扫（系统受保护广播，无导出要求）
        btStateReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                int state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1);
                if (state == BluetoothAdapter.STATE_ON) {
                    startScan();
                    updateNotification("等待上秤…");
                } else if (state == BluetoothAdapter.STATE_TURNING_OFF
                        || state == BluetoothAdapter.STATE_OFF) {
                    stopScan();
                    updateNotification("蓝牙未开启");
                }
            }
        };
        registerReceiver(btStateReceiver, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = buildNotification("等待上秤…");
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, n,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(NOTIFICATION_ID, n);
        }
        startScan();
        return START_STICKY;
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(
                this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("体脂秤监听中")
                .setContentText(text)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void updateNotification(String text) {
        if (stopped) {
            return;  // onDestroy 后的 in-flight 回调不得复活常驻通知
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(NOTIFICATION_ID, buildNotification(text));
    }

    // ---- BLE 扫描 ------------------------------------------------------------

    private void startScan() {
        if (scanCallback != null) {
            return; // 已在扫
        }
        BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = bm == null ? null : bm.getAdapter();
        if (adapter == null || !adapter.isEnabled()) {
            updateNotification("蓝牙未开启");
            return;
        }
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            updateNotification("蓝牙不可用");
            return;
        }

        scanCallback = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                if (result == null || result.getScanRecord() == null) {
                    return;
                }
                byte[] data = result.getScanRecord().getServiceData(UUID_BODY_COMPOSITION);
                if (data != null) {
                    handler.post(() -> handleFrame(data));  // 状态统一主线程访问
                }
            }

            @Override
            public void onScanFailed(int errorCode) {
                Log.w(TAG, "scan failed: " + errorCode);
                updateNotification("扫描失败（" + errorCode + "），重启应用重试");
            }
        };

        // 按 0x181B Service Data 过滤（空数据 = 只要求存在该 UUID 的 service data），
        // 低功耗模式常驻；个别 ROM 不支持空过滤时退回无过滤扫描。
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_POWER)
                .build();
        try {
            List<ScanFilter> filters = new ArrayList<>();
            filters.add(new ScanFilter.Builder()
                    .setServiceData(UUID_BODY_COMPOSITION, new byte[0])
                    .build());
            scanner.startScan(filters, settings, scanCallback);
        } catch (Exception e) {
            Log.w(TAG, "filtered scan failed, fallback to unfiltered", e);
            try {
                scanner.startScan(null, settings, scanCallback);
            } catch (Exception e2) {
                Log.e(TAG, "startScan failed", e2);
                updateNotification("无扫描权限或蓝牙异常");
                scanCallback = null;
            }
        }
    }

    private void stopScan() {
        if (scanner != null && scanCallback != null) {
            try {
                scanner.stopScan(scanCallback);
            } catch (Exception ignored) {
            }
        }
        scanCallback = null;
    }

    // ---- 帧处理（与 NAS 网关同款去抖/去重） -----------------------------------

    private void handleFrame(byte[] d) {
        if (d.length != 13) {
            return;
        }
        int unit = d[0] & 0xFF;
        int fl = d[1] & 0xFF;
        boolean stabilized = (fl & 0x20) != 0;
        boolean loadRemoved = (fl & 0x80) != 0;
        boolean hasImpedance = (fl & 0x02) != 0;
        if (!stabilized || loadRemoved) {
            return;
        }

        int rawWeight = ((d[12] & 0xFF) << 8) | (d[11] & 0xFF);
        if (unit != 0x02) {
            // 非 kg 模式：换算系数无法可靠验证，宁可跳过并记日志，不落错误数据
            Log.w(TAG, String.format(Locale.US, "跳过非 kg 单位帧 unit=0x%02x", unit));
            return;
        }
        double weight = rawWeight * 0.005;
        if (weight < 10 || weight > 300) {
            return;
        }
        weight = Math.round(weight * 100) / 100.0;

        Integer impedance = null;
        if (hasImpedance) {
            int z = ((d[10] & 0xFF) << 8) | (d[9] & 0xFF);
            if (z > 0 && z < 3000) {
                impedance = z;
            }
        }

        Calendar cal = Calendar.getInstance();
        // 兜底默认值取整到分钟：RTC 失效时同一测量的连播帧（乃至手机/网关双端）才能生成同一去重键
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        int year = ((d[3] & 0xFF) << 8) | (d[2] & 0xFF);
        try {
            Calendar rtc = Calendar.getInstance();
            rtc.set(year, (d[4] & 0xFF) - 1, d[5] & 0xFF, d[6] & 0xFF, d[7] & 0xFF, d[8] & 0xFF);
            // RTC 掉电/没对过时的兜底：偏差超过 3 天用系统时间（上面已取整）
            if (Math.abs(rtc.getTimeInMillis() - System.currentTimeMillis()) < 3L * 86400_000) {
                cal = rtc;
            }
        } catch (Exception ignored) {
        }
        String tsIso = String.format(Locale.US, "%04d-%02d-%02dT%02d:%02d:%02d",
                cal.get(Calendar.YEAR), cal.get(Calendar.MONTH) + 1,
                cal.get(Calendar.DAY_OF_MONTH), cal.get(Calendar.HOUR_OF_DAY),
                cal.get(Calendar.MINUTE), cal.get(Calendar.SECOND));
        String key = String.format(Locale.US, "%s-%d",
                tsIso.replaceAll("[-:]", ""), Math.round(weight * 200));

        long now = System.currentTimeMillis();
        Iterator<Map.Entry<String, Long>> it = sent.entrySet().iterator();
        while (it.hasNext()) {
            if (now - it.next().getValue() > SENT_TTL_MS) {
                it.remove();
            }
        }
        if (sent.containsKey(key)) {
            return;
        }

        Measurement m = pending.get(key);
        if (m == null) {
            final String fKey = key;
            Runnable flush = () -> flushMeasurement(fKey);
            m = new Measurement(key, tsIso, weight, impedance, flush);
            pending.put(key, m);
            handler.postDelayed(flush, SETTLE_MS);
        } else if (m.impedance == null && impedance != null) {
            m.impedance = impedance;
        }
        // 拿到阻抗就不必再等窗口
        if (m.impedance != null) {
            handler.removeCallbacks(m.flush);
            flushMeasurement(key);
        }
    }

    private void flushMeasurement(String key) {
        Measurement m = pending.remove(key);
        if (m == null) {
            return;
        }
        sent.put(key, System.currentTimeMillis());
        SharedPreferences prefs = getSharedPreferences("shell", MODE_PRIVATE);
        String server = prefs.getString("server_url", "");
        String token = prefs.getString("ingest_token", "");
        if (server.isEmpty() || token.isEmpty()) {
            updateNotification("未配置服务器/Token");
            return;
        }
        String json = String.format(Locale.US,
                "{\"measurements\":[{\"ts\":\"%s\",\"weight_kg\":%.2f,\"impedance\":%s}]}",
                m.tsIso, m.weightKg, m.impedance == null ? "null" : m.impedance.toString());
        io.execute(() -> {
            boolean ok = false;
            for (int attempt = 1; attempt <= 3 && !ok; attempt++) {
                ok = postJson(server + "/api/ingest/miscale", token, json);
                if (!ok) {
                    try {
                        Thread.sleep(2000L * attempt);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                }
            }
            final String text = ok
                    ? String.format(Locale.US, "已记录 %.2f kg%s", m.weightKg,
                        m.impedance != null ? "（含体成分）" : "")
                    : "上报失败，检查服务器/Token";
            final boolean finalOk = ok;
            handler.post(() -> {
                if (!finalOk) {
                    sent.remove(key); // 允许下次广播重试（主线程访问，防并发损坏）
                }
                updateNotification(text);
            });
        });
    }

    private static boolean postJson(String url, String token, String json) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Authorization", "Bearer " + token);
            conn.setRequestProperty("Content-Type", "application/json");
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int code = conn.getResponseCode();
            Log.i(TAG, "POST " + url + " -> " + code);
            return code >= 200 && code < 300;
        } catch (Exception e) {
            Log.w(TAG, "POST failed: " + e);
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ---- 生命周期 -------------------------------------------------------------

    @Override
    public void onDestroy() {
        stopped = true;
        stopScan();
        if (btStateReceiver != null) {
            try {
                unregisterReceiver(btStateReceiver);
            } catch (Exception ignored) {
            }
            btStateReceiver = null;
        }
        handler.removeCallbacksAndMessages(null);
        io.shutdownNow();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.cancel(NOTIFICATION_ID);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
