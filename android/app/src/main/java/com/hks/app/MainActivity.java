package com.hks.app;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.widget.Toolbar;

public class MainActivity extends AppCompatActivity {

    private static final String PREFS = "hks_prefs";
    private static final String KEY_URL = "backend_url";
    private static final String DEFAULT_URL = "http://192.168.3.9:5050/";

    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Toolbar toolbar = findViewById(R.id.toolbar);
        setSupportActionBar(toolbar);

        webView = findViewById(R.id.webView);
        setupWebView();

        String saved = loadUrl();
        if (saved.isEmpty()) {
            showUrlDialog(null);
        } else {
            webView.loadUrl(saved);
        }
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
                if (failingUrl.equals(webView.getUrl())) {
                    Toast.makeText(MainActivity.this,
                            "无法连接后端：" + description, Toast.LENGTH_LONG).show();
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient());
    }

    private String loadUrl() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        return prefs.getString(KEY_URL, "");
    }

    private void saveUrl(String url) {
        getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit()
                .putString(KEY_URL, url)
                .apply();
    }

    private void showUrlDialog(String current) {
        View view = LayoutInflater.from(this).inflate(R.layout.dialog_url, null);
        EditText input = view.findViewById(R.id.urlInput);
        String value = current == null || current.isEmpty() ? DEFAULT_URL : current;
        input.setText(value);
        input.setSelection(input.getText().length());

        new AlertDialog.Builder(this)
                .setTitle("设置后端地址")
                .setMessage("手机与电脑需在同一 Wi-Fi，地址保持 http:// 前缀")
                .setView(view)
                .setPositiveButton("连接", (dialog, which) -> {
                    String url = input.getText().toString().trim();
                    if (!url.startsWith("http://") && !url.startsWith("https://")) {
                        url = "http://" + url;
                    }
                    saveUrl(url);
                    webView.loadUrl(url);
                })
                .setNegativeButton("取消", null)
                .show();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_settings) {
            showUrlDialog(loadUrl());
            return true;
        }
        if (id == R.id.action_reload) {
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
