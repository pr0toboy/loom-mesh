package org.loommesh.loomchat;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;

/** Shell: one WebView running the local LoomChat UI (assets/index.html), which talks to Matrix over HTTPS. */
public class MainActivity extends Activity {
    private static final int PICK = 1;
    private WebView web;
    private ValueCallback<Uri[]> pending;
    private Pont pont;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMinimumFontSize(1); s.setMinimumLogicalFontSize(1); // terminal view needs < 8px text to fit 80 columns
        s.setAllowFileAccess(false);  // assets load via file:///android_asset/ regardless
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (u.toString().startsWith("file:///android_asset/")) return false;
                if ("file".equals(u.getScheme())) return true;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception e) { /* no browser */ }
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (pending != null) pending.onReceiveValue(null);
                pending = cb;
                try { Intent it = p.createIntent(); if (p.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) it.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true); startActivityForResult(it, PICK); }
                catch (Exception e) { pending = null; return false; }
                return true;
            }
        });
        pont = new Pont(this);
        web.addJavascriptInterface(pont, "LoomNative");
        setContentView(web);
        if (b == null || web.restoreState(b) == null) web.loadUrl("file:///android_asset/index.html");
        handleRoom(getIntent());
    }

    void js(String code) { runOnUiThread(() -> web.evaluateJavascript(code, null)); }

    @Override protected void onNewIntent(Intent it) { super.onNewIntent(it); handleRoom(it); }

    @SuppressWarnings("deprecation")
    private void handleShare(Intent it) {
        if (it == null) return;
        String a = it.getAction();
        if (!Intent.ACTION_SEND.equals(a) && !Intent.ACTION_SEND_MULTIPLE.equals(a)) return;
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            String text = it.getStringExtra(Intent.EXTRA_TEXT);
            if (text != null) o.put("text", text);
            java.util.ArrayList<Uri> uris = new java.util.ArrayList<>();
            if (Intent.ACTION_SEND.equals(a)) {
                Uri u = it.getParcelableExtra(Intent.EXTRA_STREAM);
                if (u != null) uris.add(u);
            } else {
                java.util.ArrayList<Uri> l = it.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
                if (l != null) uris.addAll(l);
            }
            org.json.JSONArray files = new org.json.JSONArray();
            for (Uri u : uris) {
                String name = "file", mime = getContentResolver().getType(u);
                try (android.database.Cursor c = getContentResolver().query(u, new String[]{android.provider.OpenableColumns.DISPLAY_NAME}, null, null, null)) {
                    if (c != null && c.moveToFirst()) name = c.getString(0);
                }
                byte[] data;
                try (java.io.InputStream in = getContentResolver().openInputStream(u)) {
                    if (in == null) continue;
                    data = in.readAllBytes();
                }
                if (data.length > 50 * 1024 * 1024) continue;   // 50 MB cap
                files.put(new org.json.JSONObject().put("name", name).put("mime", mime == null ? "" : mime)
                        .put("b64", android.util.Base64.encodeToString(data, android.util.Base64.NO_WRAP)));
            }
            o.put("files", files);
            pont.pendingShare = o.toString();
            web.evaluateJavascript("window.loomShare && window.loomShare()", null);
        } catch (Exception e) { /* nothing shared */ }
    }

    private void handleRoom(Intent it) {
        handleShare(it);
        String room = it == null ? null : it.getStringExtra("room");
        if (room == null || !room.matches("![A-Za-z0-9._:=-]+")) return;
        pont.pendingRoom = room;
        web.evaluateJavascript("window.loomOpen && window.loomOpen('" + room + "')", null);
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        if (req == Pont.DICTATE) {
            if (res == RESULT_OK && data != null) {
                java.util.ArrayList<String> r = data.getStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_RESULTS);
                if (r != null && !r.isEmpty()) web.evaluateJavascript("window.onDictate(" + org.json.JSONObject.quote(r.get(0)) + ")", null);
            }
            return;
        }
        if (req == PICK && pending != null) {
            Uri[] out = null;
            if (res == RESULT_OK && data != null) {
                android.content.ClipData cd = data.getClipData();
                if (cd != null) { out = new Uri[cd.getItemCount()]; for (int i = 0; i < out.length; i++) out[i] = cd.getItemAt(i).getUri(); }
                else if (data.getData() != null) out = new Uri[]{ data.getData() };
            }
            pending.onReceiveValue(out);
            pending = null;
        } else super.onActivityResult(req, res, data);
    }

    @Override protected void onSaveInstanceState(Bundle o) { super.onSaveInstanceState(o); web.saveState(o); }

    @SuppressWarnings("deprecation")
    @Override public void onBackPressed() {
        web.evaluateJavascript("window.loomBack && window.loomBack()", r -> { if (!"true".equals(r)) finish(); });
    }
}
