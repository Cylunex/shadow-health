package com.shadowverse.health;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONException;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * WebView shell for the shadow-health LAN web app.
 *
 * - Server addresses live in SharedPreferences（可配多个，每行一个，内网在前
 *   frp 外网在后；探测按序自动切换，见 ServerConfig）。
 *   Change on first launch, via a 3-finger long-press anywhere, or the MENU key.
 * - Back key walks WebView history before exiting.
 * - 启动先秒开内置本地页（assets/offline.html），后台探测 /healthz：在线自动切
 *   服务器页面，离线停留本地页直接记录（队列见 OfflineStore，30s 自动重试）；
 *   加载失败也回落到同一本地页（docs/offline-plan.md 阶段二）。
 * - Cookies persist (flushed on pause) so the login session survives restarts.
 *   注意 cookie 按 origin 隔离：每个服务器地址首次使用需各自登录一次。
 */
public class MainActivity extends Activity {

    private static final String PREFS_NAME = "shell";
    private static final String KEY_SERVER_URL = ServerConfig.KEY_ACTIVE;
    private static final String KEY_INGEST_TOKEN = "ingest_token";
    private static final String KEY_SCALE_SCAN = "scale_scan_enabled";
    private static final String DEFAULT_SERVER_URL = ServerConfig.DEFAULT_SERVER_URL;
    private static final String LOCAL_PAGE_URL = "file:///android_asset/offline.html";
    private static final int DARK_BG = Color.parseColor("#0f172a");
    private static final long LONG_PRESS_MS = 700;
    private static final long PROBE_INTERVAL_MS = 30_000;
    private static final long BOOTSTRAP_REFRESH_MS = 60 * 60_000L;
    private static final int REQ_SCALE_PERMS = 42;
    private static final int REQ_FILE_CHOOSER = 44;

    private WebView webView;
    private SharedPreferences prefs;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Runnable probeRunnable = this::probeAndConnect;
    private Runnable pendingLongPress;
    private String lastAttemptedUrl;
    private boolean showingLocalPage;
    private boolean onSnapshotPage;  // 当前主文档是离线快照回放：保持探测、连上原地刷新
    private String lastError;  // 最近一次加载失败原因，本地页状态栏展示
    private boolean clearHistoryOnNextLoad;  // 本地启动页不留在返回历史里
    private String erroredUrl;  // 加载失败的 URL 也会触发 onPageFinished，需忽略以免复位标志
    private boolean pendingTimedScan;  // 称重模式等权限授予后再启动
    private ValueCallback<Uri[]> filePathCallback;  // <input type=file> 的回调，选择结果回传给 WebView

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Window win = getWindow();
        win.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        win.setStatusBarColor(DARK_BG);
        win.setNavigationBarColor(DARK_BG);

        prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);

        webView = new WebView(this);
        webView.setBackgroundColor(DARK_BG);
        webView.setLayoutParams(new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        webView.addJavascriptInterface(new ShellBridge(), "ShellBridge");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                // Keep http/https inside the WebView; swallow anything else
                // (tel:, mailto:, intent:) instead of crashing on missing handlers.
                return !(url.startsWith("http://") || url.startsWith("https://"));
            }

            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(
                    WebView view, WebResourceRequest request) {
                // 阶段三快照缓存：在线代理落盘、离线回放（详见 SnapshotCache）。
                // 多服务器：按请求源匹配清单里的服务器（各 origin 独立缓存条目）
                return SnapshotCache.intercept(getApplicationContext(), request,
                        ServerConfig.matching(getApplicationContext(), request.getUrl()));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (url != null && url.equals(erroredUrl)) {
                    erroredUrl = null;  // 失败页自身的 finished 回调，不代表加载成功
                    return;
                }
                if (url != null && url.startsWith("http")) {
                    showingLocalPage = false;
                    if (SnapshotCache.consumeReplayedMain(url)) {
                        // 离线快照页 ≠ 在线：保持 30s 探测，连上后原地 reload 回真页面
                        onSnapshotPage = true;
                        handler.removeCallbacks(probeRunnable);
                        handler.postDelayed(probeRunnable, PROBE_INTERVAL_MS);
                        return;
                    }
                    onSnapshotPage = false;
                    lastError = null;
                    handler.removeCallbacks(probeRunnable);  // 在线了，停 30s 自动探测
                    if (clearHistoryOnNextLoad) {
                        clearHistoryOnNextLoad = false;
                        view.clearHistory();  // 返回键不该退回本地启动页
                    }
                    maybeRefreshBootstrap();
                    if (OfflineStore.queueSize(MainActivity.this) > 0) {
                        OfflineStore.scheduleFlush(MainActivity.this);
                    }
                }
            }

            @Override
            public void onReceivedHttpAuthRequest(WebView view,
                                                  android.webkit.HttpAuthHandler handler,
                                                  String host, String realm) {
                // frp 等入口的 HTTP Basic 验证：地址里配了 user:pass@ 就自动应答
                String[] cred = ServerConfig.credentialsForHost(MainActivity.this, host);
                if (cred != null) {
                    handler.proceed(cred[0], cred[1]);
                } else {
                    handler.cancel();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    String failedUrl = request.getUrl().toString();
                    if (failedUrl.startsWith("file://")) {
                        return;  // 本地页自身失败没有回落目标，防递归
                    }
                    lastAttemptedUrl = failedUrl;
                    erroredUrl = failedUrl;
                    showLocalPage(String.valueOf(error.getDescription()));
                }
            }
        });

        // 必须设 WebChromeClient：否则 WebView 静默取消所有 JS 对话框——window.confirm()
        // 恒返回 false，页面里 hx-confirm 的删除/放弃确认全部点了没反应；
        // 且没有 onShowFileChooser 时 <input type=file>（拍照记餐、导入 zip）打不开。
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);  // 上一次未完成的选择先作废
                }
                filePathCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), REQ_FILE_CHOOSER);
                } catch (Exception e) {
                    filePathCallback = null;
                    Toast.makeText(MainActivity.this, "无法打开文件选择器", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });

        // CSV 导出等 Content-Disposition: attachment 响应 WebView 自身不处理，
        // 不设 DownloadListener 会被静默丢弃——转交系统 DownloadManager 并带上登录 Cookie。
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null) {
                    req.addRequestHeader("Cookie", cookie);
                }
                // frp Basic 验证：DownloadManager 不走 WebView 的认证缓存，得自带头
                String basicAuth = ServerConfig.basicAuthHeaderForUrl(MainActivity.this, url);
                if (basicAuth != null) {
                    req.addRequestHeader("Authorization", basicAuth);
                }
                String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                req.setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                dm.enqueue(req);
                Toast.makeText(MainActivity.this, "已开始下载 " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(MainActivity.this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        });

        setContentView(webView);

        if (!prefs.contains(KEY_SERVER_URL)) {
            showAddressDialog(true);
        } else {
            showLocalPage(null);   // 秒开本地页（毫秒出屏，永不白屏）
            probeAndConnect();     // 在线则自动切服务器页，体感只是闪一下启动屏
        }
        // 上次离线的积压记录：联网即补发
        if (OfflineStore.queueSize(this) > 0) {
            OfflineStore.scheduleFlush(this);
        }

        // 上次开着秤监听且权限还在：随应用启动恢复前台服务
        if (prefs.getBoolean(KEY_SCALE_SCAN, false) && missingScalePermissions().isEmpty()) {
            startScaleService();
        }
        // 三星健康同步开着：重申周期任务（UPDATE 策略幂等，无授权弹窗）
        if (prefs.getBoolean(SamsungSync.PREF_ENABLED, false)) {
            SamsungSync.schedule(getApplicationContext());
        }
        // 每日提醒开着：重申周期任务（对齐下一个 20:30）
        if (prefs.getBoolean(Reminders.PREF_ENABLED, false)) {
            Reminders.schedule(getApplicationContext());
        }
    }

    /** 提醒需要通知权限（Android 13+），未授予则请求；被拒不拦路（任务照跑，只是不显示）。 */
    private void ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 43);
        }
    }

    private String getServerUrl() {
        String url = ServerConfig.active(this);
        return url.isEmpty() ? DEFAULT_SERVER_URL : url;
    }

    private void loadServer() {
        clearHistoryOnNextLoad = showingLocalPage;
        erroredUrl = null;  // 新一轮加载：stale 守卫不能吞掉这次的 onPageFinished
        lastAttemptedUrl = ServerConfig.bare(getServerUrl());  // loadUrl 不吃 userinfo
        webView.loadUrl(lastAttemptedUrl);
    }

    // ---- 本地启动页 + /healthz 探测（docs/offline-plan.md 阶段二） ----

    private void showLocalPage(String reason) {
        showingLocalPage = true;
        lastError = reason;
        webView.loadUrl(LOCAL_PAGE_URL);
        handler.removeCallbacks(probeRunnable);
        handler.postDelayed(probeRunnable, PROBE_INTERVAL_MS);  // 30s 自动重试
    }

    /** 后台探测（多地址按序，见 ServerConfig.resolve）：通了就回到断点页面并补发
     *  队列；服务器切换时把断点深链换到新地址同路径；全不通留在本地页/快照页等下轮。 */
    private void probeAndConnect() {
        if (isFinishing() || isDestroyed()) {
            return;  // 销毁后 io 已 shutdown，execute 会 RejectedExecutionException
        }
        handler.removeCallbacks(probeRunnable);
        io.execute(() -> {
            final String server = ServerConfig.resolve(MainActivity.this);  // "" = 全不通
            handler.post(() -> {
                if (isFinishing() || isDestroyed()) {
                    return;
                }
                if (!server.isEmpty()) {
                    if (showingLocalPage) {
                        // 回到中断前的页面（中途断网的深链）；服务器切换则换源同路径，
                        // 深链不属于任何已知服务器（历史残留）就回首页
                        clearHistoryOnNextLoad = true;
                        erroredUrl = null;
                        String target = ServerConfig.rebase(
                                MainActivity.this, lastAttemptedUrl, server);
                        webView.loadUrl(target != null ? target : ServerConfig.bare(server));
                    } else if (onSnapshotPage) {
                        String rebased = ServerConfig.rebase(
                                MainActivity.this, webView.getUrl(), server);
                        if (rebased != null && !rebased.equals(webView.getUrl())) {
                            webView.loadUrl(rebased);  // 服务器切换：换源加载同页面
                        } else {
                            webView.reload();  // 快照页原地换成在线真页面（横幅随之消失）
                        }
                    }
                    if (OfflineStore.queueSize(MainActivity.this) > 0) {
                        OfflineStore.scheduleFlush(MainActivity.this);  // 补发统一走 Worker
                    }
                } else if (showingLocalPage || onSnapshotPage) {
                    if (showingLocalPage) {
                        webView.evaluateJavascript(
                                "window.onProbeResult&&window.onProbeResult(false)", null);
                    }
                    handler.postDelayed(probeRunnable, PROBE_INTERVAL_MS);
                }
            });
        });
    }

    /** 成功加载服务器页面后刷新 bootstrap 缓存（≥1 小时才重拉，页页刷不划算）。 */
    private void maybeRefreshBootstrap() {
        if (OfflineStore.bootstrapAgeMs(this) < BOOTSTRAP_REFRESH_MS) {
            return;
        }
        io.execute(() -> OfflineStore.fetchBootstrap(getApplicationContext()));
    }

    private static String localDate() {
        return new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date());
    }

    // ---- server address dialog -------------------------------------------

    private void showAddressDialog(final boolean firstRun) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI
                | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        input.setMinLines(2);
        input.setMaxLines(4);
        input.setHint("服务器地址（每行一个，靠前优先：内网在上、frp 外网在下）");
        List<String> configured = ServerConfig.urls(this);
        input.setText(configured.isEmpty()
                ? DEFAULT_SERVER_URL : String.join("\n", configured));
        input.setSelection(input.getText().length());

        final EditText tokenInput = new EditText(this);
        tokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        tokenInput.setHint("INGEST_TOKEN（体脂秤/离线补同步用，同 .env）");
        tokenInput.setText(prefs.getString(KEY_INGEST_TOKEN, ""));

        final CheckBox scanBox = new CheckBox(this);
        scanBox.setText("后台监听体脂秤（常驻通知）");
        scanBox.setChecked(prefs.getBoolean(KEY_SCALE_SCAN, false));

        final CheckBox samsungBox = new CheckBox(this);
        samsungBox.setText("同步三星健康数据（手表）");
        samsungBox.setChecked(prefs.getBoolean(SamsungSync.PREF_ENABLED, false));

        final CheckBox reminderBox = new CheckBox(this);
        reminderBox.setText("每日提醒（20:30 打卡/目标缺口）");
        reminderBox.setChecked(prefs.getBoolean(Reminders.PREF_ENABLED, false));

        TextView hint = new TextView(this);
        hint.setText("可填多个地址（每行一个）：断线按顺序自动切换，各地址首次使用需各自登录。"
                + "frp 等入口开了 Basic 验证时写成 http://用户:密码@域名。"
                + "秤监听需要蓝牙权限；三星同步需先在三星健康开发者模式里开 Data Read"
                + "（版本号连点 10 次解锁）。国产 ROM 记得允许自启动，否则后台会被清理。");
        hint.setTextSize(12);

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        col.setPadding(pad, pad / 2, pad, 0);
        col.addView(input);
        col.addView(tokenInput);
        col.addView(scanBox);
        col.addView(samsungBox);
        col.addView(reminderBox);
        col.addView(hint);

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("连接设置")
                .setMessage("局域网内 shadow-health 服务地址")
                .setView(col)
                .setPositiveButton("保存并连接", (d, w) -> {
                    List<String> urls = new ArrayList<>();
                    for (String line : input.getText().toString().split("\n")) {
                        String url = ServerConfig.normalizeUrl(line);
                        if (!url.isEmpty() && !urls.contains(url)) {
                            urls.add(url);
                        }
                    }
                    if (urls.isEmpty()) {
                        urls.add(DEFAULT_SERVER_URL);
                    }
                    boolean scanOn = scanBox.isChecked();
                    boolean samsungOn = samsungBox.isChecked();
                    boolean samsungWas = prefs.getBoolean(SamsungSync.PREF_ENABLED, false);
                    boolean reminderOn = reminderBox.isChecked();
                    ServerConfig.save(this, urls);
                    prefs.edit()
                            .putString(KEY_INGEST_TOKEN, tokenInput.getText().toString().trim())
                            .putBoolean(KEY_SCALE_SCAN, scanOn)
                            .putBoolean(SamsungSync.PREF_ENABLED, samsungOn)
                            .putBoolean(Reminders.PREF_ENABLED, reminderOn)
                            .apply();
                    loadServer();
                    applyScaleScanSetting(scanOn);
                    if (samsungOn && !samsungWas) {
                        SamsungSync.enable(this);   // 弹三星健康授权页 + 注册周期任务 + 立即同步
                    } else if (samsungOn) {
                        SamsungSync.syncNow(getApplicationContext());
                    } else if (samsungWas) {
                        SamsungSync.disable(getApplicationContext());
                    }
                    if (reminderOn) {
                        Reminders.schedule(getApplicationContext());
                        ensureNotificationPermission();
                    } else {
                        Reminders.cancel(getApplicationContext());
                    }
                })
                .setNeutralButton("恢复默认", (d, w) -> {
                    ServerConfig.save(this, java.util.Collections.singletonList(DEFAULT_SERVER_URL));
                    loadServer();
                });
        if (firstRun) {
            b.setCancelable(false);
        } else {
            b.setNegativeButton("取消", null);
        }
        b.show();
    }

    // ---- 体脂秤监听服务 ------------------------------------------------------

    private List<String> missingScalePermissions() {
        List<String> need = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= 31) {
            if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN)
                    != PackageManager.PERMISSION_GRANTED) {
                need.add(Manifest.permission.BLUETOOTH_SCAN);
            }
        } else {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    != PackageManager.PERMISSION_GRANTED) {
                need.add(Manifest.permission.ACCESS_FINE_LOCATION);
            }
        }
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            need.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        return need;
    }

    private void applyScaleScanSetting(boolean enabled) {
        if (!enabled) {
            stopService(new Intent(this, ScaleScanService.class));
            return;
        }
        List<String> missing = missingScalePermissions();
        if (missing.isEmpty()) {
            startScaleService();
        } else {
            requestPermissions(missing.toArray(new String[0]), REQ_SCALE_PERMS);
        }
    }

    private void startScaleService() {
        startForegroundService(new Intent(this, ScaleScanService.class));
    }

    /** 称重模式（页面按钮触发）：监听 3 分钟自动停，不留常驻通知。 */
    private void startTimedScaleScan() {
        List<String> missing = missingScalePermissions();
        if (!missing.isEmpty()) {
            pendingTimedScan = true;
            requestPermissions(missing.toArray(new String[0]), REQ_SCALE_PERMS);
            return;
        }
        startForegroundService(new Intent(this, ScaleScanService.class)
                .putExtra(ScaleScanService.EXTRA_TIMED, true));
        Toast.makeText(this,
                "秤监听已开启 " + (ScaleScanService.TIMED_SCAN_MS / 60_000) + " 分钟，请上秤",
                Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE_CHOOSER) {
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(
                        WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                filePathCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != REQ_SCALE_PERMS) {
            return;
        }
        boolean timed = pendingTimedScan;  // 称重模式的授权失败不动常驻开关设置
        pendingTimedScan = false;
        if (permissions.length == 0) {
            // 系统取消了权限请求（来电/分屏等）：按未授予处理，不能默认放行
            if (!timed) {
                prefs.edit().putBoolean(KEY_SCALE_SCAN, false).apply();
            }
            Toast.makeText(this, "权限请求被中断，秤监听未开启", Toast.LENGTH_LONG).show();
            return;
        }
        boolean scanGranted = true;
        for (int i = 0; i < permissions.length; i++) {
            boolean isNotif = Manifest.permission.POST_NOTIFICATIONS.equals(permissions[i]);
            if (!isNotif && results[i] != PackageManager.PERMISSION_GRANTED) {
                scanGranted = false;  // 通知权限被拒不拦路，扫描权限被拒才算失败
            }
        }
        if (scanGranted) {
            if (timed) {
                startTimedScaleScan();
            } else {
                startScaleService();
            }
        } else {
            if (!timed) {
                prefs.edit().putBoolean(KEY_SCALE_SCAN, false).apply();
            }
            Toast.makeText(this, "未授予蓝牙权限，秤监听未开启", Toast.LENGTH_LONG).show();
        }
    }

    // ---- JS bridge（本地启动页 + 离线队列） --------------------------------

    private class ShellBridge {
        @JavascriptInterface
        public void changeAddress() {
            handler.post(() -> showAddressDialog(false));
        }

        /** 快照页横幅「去离线记录」：回到可记录的本地启动页。 */
        @JavascriptInterface
        public void openOfflinePage() {
            handler.post(() -> showLocalPage(null));
        }

        /** 称重模式：监听 3 分钟自动停（今日页/离线页按钮触发，见 ScaleScanService）。 */
        @JavascriptInterface
        public void startScaleScan() {
            handler.post(MainActivity.this::startTimedScaleScan);
        }

        /** 本地页手动重试：探测通了自动切服务器页，不通回调 window.onProbeResult(false)。
         *  post probeRunnable 本体（而非新引用），onDestroy 的 removeCallbacks 才能取消。 */
        @JavascriptInterface
        public void probeNow() {
            handler.post(probeRunnable);
        }

        /** 上次在线时缓存的 bootstrap JSON（习惯清单/训练类型/餐次），没有则空串。 */
        @JavascriptInterface
        public String getBootstrap() {
            return OfflineStore.bootstrap(MainActivity.this);
        }

        @JavascriptInterface
        public String getOfflineStatus() {
            JSONObject o = new JSONObject();
            try {
                o.put("queued", OfflineStore.queueSize(MainActivity.this));
                o.put("error", lastError == null ? "" : lastError);
                o.put("server", ServerConfig.bare(getServerUrl()));  // 本地页展示，不露凭据
            } catch (JSONException ignored) {
            }
            return o.toString();
        }

        /** 入队一条离线记录（type: habit/diet/workout/metric）；返回队列长度，-1 失败。
         *  补发任务由 OfflineStore 在队列空转非空时注册，这里不必每条都敲 WorkManager。 */
        @JavascriptInterface
        public int enqueueRecord(String type, String payloadJson) {
            if (!"habit".equals(type) && !"diet".equals(type)
                    && !"workout".equals(type) && !"metric".equals(type)) {
                return -1;
            }
            return OfflineStore.enqueue(MainActivity.this, type, localDate(), payloadJson);
        }

        /** 今日打卡暂存：声明式覆盖同日同习惯（doneCount<=0 = 撤销）；返回队列长度。 */
        @JavascriptInterface
        public int setQueuedHabit(int habitId, int doneCount) {
            return OfflineStore.setQueuedHabit(MainActivity.this, habitId, localDate(), doneCount);
        }

        /** 今日已暂存打卡 {habit_id: done_count}，本地页渲染勾选态。 */
        @JavascriptInterface
        public String getQueuedHabits() {
            return OfflineStore.queuedHabits(MainActivity.this, localDate());
        }
    }

    // ---- gestures / keys ----------------------------------------------------

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        switch (ev.getActionMasked()) {
            case MotionEvent.ACTION_POINTER_DOWN:
                if (ev.getPointerCount() == 3) {
                    cancelPendingLongPress();
                    pendingLongPress = () -> showAddressDialog(false);
                    handler.postDelayed(pendingLongPress, LONG_PRESS_MS);
                } else if (ev.getPointerCount() > 3) {
                    cancelPendingLongPress();
                }
                break;
            case MotionEvent.ACTION_POINTER_UP:
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                cancelPendingLongPress();
                break;
            default:
                break;
        }
        return super.dispatchTouchEvent(ev);
    }

    private void cancelPendingLongPress() {
        if (pendingLongPress != null) {
            handler.removeCallbacks(pendingLongPress);
            pendingLongPress = null;
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            showAddressDialog(false);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onBackPressed() {
        if (!showingLocalPage && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    // ---- lifecycle ----------------------------------------------------------

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
        webView.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
        // 回前台：有积压就再排一次补发（KEEP 策略幂等）；停在本地页/快照页则立刻探测
        if (OfflineStore.queueSize(this) > 0) {
            OfflineStore.scheduleFlush(this);
        }
        if (showingLocalPage || onSnapshotPage) {
            probeAndConnect();
        }
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(probeRunnable);
        io.shutdownNow();
        CookieManager.getInstance().flush();
        webView.destroy();
        super.onDestroy();
    }
}
