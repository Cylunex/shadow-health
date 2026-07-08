package com.shadowverse.health;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
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
import android.widget.EditText;
import android.widget.FrameLayout;

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
    private static final String DEFAULT_SERVER_URL = "http://192.168.1.100:8080";
    private static final int DARK_BG = Color.parseColor("#0f172a");
    private static final long LONG_PRESS_MS = 700;

    private WebView webView;
    private SharedPreferences prefs;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable pendingLongPress;
    private String lastAttemptedUrl;
    private boolean showingErrorPage;

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
                if (url != null && url.startsWith("http")) {
                    showingErrorPage = false;
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    lastAttemptedUrl = request.getUrl().toString();
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
        input.setText(getServerUrl());
        input.setSelection(input.getText().length());
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        FrameLayout wrap = new FrameLayout(this);
        wrap.setPadding(pad, pad / 2, pad, 0);
        wrap.addView(input);

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("服务器地址")
                .setMessage("局域网内 shadow-health 服务地址")
                .setView(wrap)
                .setPositiveButton("保存并连接", (d, w) -> {
                    String url = normalizeUrl(input.getText().toString());
                    prefs.edit().putString(KEY_SERVER_URL, url).apply();
                    loadServer();
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
