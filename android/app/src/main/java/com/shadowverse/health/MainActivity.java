package com.shadowverse.health;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
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

import java.util.ArrayList;
import java.util.List;

/**
 * WebView shell for the shadow-health LAN web app.
 *
 * - Server address lives in SharedPreferences (default http://192.168.1.100:8080).
 *   Change it on first launch, via a 3-finger long-press anywhere, or the MENU key.
 * - Back key walks WebView history before exiting.
 * - Load failures show an inline error page with retry / change-address buttons.
 * - Cookies persist (flushed on pause) so the login session survives restarts.
 */
public class MainActivity extends Activity {

    private static final String PREFS_NAME = "shell";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_INGEST_TOKEN = "ingest_token";
    private static final String KEY_SCALE_SCAN = "scale_scan_enabled";
    private static final String DEFAULT_SERVER_URL = "http://192.168.1.100:8080";
    private static final int DARK_BG = Color.parseColor("#0f172a");
    private static final long LONG_PRESS_MS = 700;
    private static final int REQ_SCALE_PERMS = 42;

    private WebView webView;
    private SharedPreferences prefs;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable pendingLongPress;
    private String lastAttemptedUrl;
    private boolean showingErrorPage;
    private String erroredUrl;  // 加载失败的 URL 也会触发 onPageFinished，需忽略以免复位错误页标志

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
            public void onPageFinished(WebView view, String url) {
                if (url != null && url.equals(erroredUrl)) {
                    erroredUrl = null;  // 失败页自身的 finished 回调，不代表加载成功
                    return;
                }
                if (url != null && url.startsWith("http")) {
                    showingErrorPage = false;
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    lastAttemptedUrl = request.getUrl().toString();
                    erroredUrl = lastAttemptedUrl;
                    showErrorPage(String.valueOf(error.getDescription()));
                }
            }
        });

        setContentView(webView);

        if (!prefs.contains(KEY_SERVER_URL)) {
            showAddressDialog(true);
        } else {
            loadServer();
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
        return prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL);
    }

    private void loadServer() {
        lastAttemptedUrl = getServerUrl();
        webView.loadUrl(lastAttemptedUrl);
    }

    // ---- server address dialog -------------------------------------------

    private void showAddressDialog(final boolean firstRun) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("服务器地址");
        input.setText(getServerUrl());
        input.setSelection(input.getText().length());

        final EditText tokenInput = new EditText(this);
        tokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        tokenInput.setHint("INGEST_TOKEN（体脂秤上报用，同 .env）");
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
        hint.setText("秤监听需要蓝牙权限；三星同步需先在三星健康开发者模式里开 Data Read"
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
                    String url = normalizeUrl(input.getText().toString());
                    boolean scanOn = scanBox.isChecked();
                    boolean samsungOn = samsungBox.isChecked();
                    boolean samsungWas = prefs.getBoolean(SamsungSync.PREF_ENABLED, false);
                    boolean reminderOn = reminderBox.isChecked();
                    prefs.edit()
                            .putString(KEY_SERVER_URL, url)
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
                    prefs.edit().putString(KEY_SERVER_URL, DEFAULT_SERVER_URL).apply();
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

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != REQ_SCALE_PERMS) {
            return;
        }
        if (permissions.length == 0) {
            // 系统取消了权限请求（来电/分屏等）：按未授予处理，不能默认放行
            prefs.edit().putBoolean(KEY_SCALE_SCAN, false).apply();
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
            startScaleService();
        } else {
            prefs.edit().putBoolean(KEY_SCALE_SCAN, false).apply();
            Toast.makeText(this, "未授予蓝牙权限，秤监听未开启", Toast.LENGTH_LONG).show();
        }
    }

    private static String normalizeUrl(String raw) {
        String url = raw == null ? "" : raw.trim();
        if (url.isEmpty()) {
            return DEFAULT_SERVER_URL;
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    // ---- inline error page ------------------------------------------------

    private void showErrorPage(String reason) {
        showingErrorPage = true;
        String html = "<!doctype html><html><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;background:#0f172a;color:#e2e8f0;"
                + "font-family:sans-serif;text-align:center;padding:24px;box-sizing:border-box}"
                + "h2{color:#34d399;margin:0 0 8px}p{color:#94a3b8;font-size:14px;margin:0 0 24px;"
                + "word-break:break-all}button{background:#34d399;color:#0f172a;border:0;"
                + "border-radius:10px;padding:12px 28px;font-size:16px;font-weight:bold;margin:6px}"
                + "button.alt{background:transparent;color:#34d399;border:1px solid #34d399}"
                + "</style></head><body>"
                + "<h2>无法连接服务器</h2>"
                + "<p>" + htmlEscape(lastAttemptedUrl == null ? getServerUrl() : lastAttemptedUrl)
                + "<br>" + htmlEscape(reason) + "</p>"
                + "<div><button onclick='ShellBridge.retry()'>重试</button>"
                + "<button class='alt' onclick='ShellBridge.changeAddress()'>改地址</button></div>"
                + "</body></html>";
        webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private static String htmlEscape(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private class ShellBridge {
        @JavascriptInterface
        public void retry() {
            handler.post(() -> {
                if (lastAttemptedUrl != null) {
                    webView.loadUrl(lastAttemptedUrl);
                } else {
                    loadServer();
                }
            });
        }

        @JavascriptInterface
        public void changeAddress() {
            handler.post(() -> showAddressDialog(false));
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
        if (!showingErrorPage && webView.canGoBack()) {
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
    }

    @Override
    protected void onDestroy() {
        CookieManager.getInstance().flush();
        webView.destroy();
        super.onDestroy();
    }
}
