package com.hks.app;

import android.annotation.SuppressLint;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.widget.Toolbar;

import com.chaquo.python.android.AndroidPlatform;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends AppCompatActivity {

    private static final String BACKEND_URL = "http://127.0.0.1:5050/";
    private static final int BACKEND_PORT = 5050;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Toolbar toolbar = findViewById(R.id.toolbar);
        setSupportActionBar(toolbar);

        webView = findViewById(R.id.webView);
        setupWebView();
        startEmbeddedBackend();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                setTitle("加载中...");
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                setTitle("A股机会雷达");
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                Toast.makeText(MainActivity.this,
                        "加载失败：" + description, Toast.LENGTH_LONG).show();
                retryAfterDelay();
            }
        });

        webView.setWebChromeClient(new WebChromeClient());
    }

    private void startEmbeddedBackend() {
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            Python py = Python.getInstance();
            PyObject entry = py.getModule("android_entry");
            entry.callAttr("start", "127.0.0.1", BACKEND_PORT);
            waitForBackend();
        } catch (Exception e) {
            Toast.makeText(this, "内置后端启动失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    private void waitForBackend() {
        new Thread(() -> {
            for (int i = 0; i < 80; i++) {
                if (isBackendReady()) {
                    handler.post(() -> webView.loadUrl(BACKEND_URL));
                    return;
                }
                try {
                    Thread.sleep(300);
                } catch (InterruptedException ignored) {
                    return;
                }
            }
            handler.post(() -> Toast.makeText(this, "内置后端启动超时", Toast.LENGTH_LONG).show());
        }).start();
    }

    private boolean isBackendReady() {
        try {
            HttpURLConnection conn = (HttpURLConnection)
                    new URL("http://127.0.0.1:5050/api/health").openConnection();
            conn.setConnectTimeout(500);
            conn.setReadTimeout(500);
            int code = conn.getResponseCode();
            conn.disconnect();
            return code == 200;
        } catch (IOException e) {
            return false;
        }
    }

    private void retryAfterDelay() {
        handler.postDelayed(() -> {
            if (isBackendReady()) {
                webView.loadUrl(BACKEND_URL);
            }
        }, 1500);
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == R.id.action_reload) {
            webView.reload();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
