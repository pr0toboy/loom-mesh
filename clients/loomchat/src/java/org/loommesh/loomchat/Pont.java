package org.loommesh.loomchat;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.drawable.Icon;
import android.speech.RecognizerIntent;
import android.speech.tts.TextToSpeech;
import android.webkit.JavascriptInterface;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** JS bridge "LoomNative": voice in/out, launcher shortcuts, widget snapshot. Only the local page can call it. */
public class Pont {
    static final int DICTATE = 2;
    private final Activity act;
    private TextToSpeech tts;
    volatile String pendingRoom;
    volatile String pendingShare;

    @JavascriptInterface public String takeShare() { String s = pendingShare; pendingShare = null; return s == null ? "" : s; }

    /** Room asked by a launcher shortcut; read once by the page when it is ready (cold start). */
    @JavascriptInterface public String takePendingRoom() { String r = pendingRoom; pendingRoom = null; return r == null ? "" : r; }

    Pont(Activity a) { act = a; }

    @JavascriptInterface public void dictate(String lang) {
        act.runOnUiThread(() -> {
            Intent it = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
            it.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
            if (lang != null && !lang.isEmpty()) it.putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang);
            try { act.startActivityForResult(it, DICTATE); } catch (Exception e) { /* no recognizer installed */ }
        });
    }

    @JavascriptInterface public void speak(String text) {
        act.runOnUiThread(() -> {
            if (tts == null) {
                tts = new TextToSpeech(act, st -> { if (st == TextToSpeech.SUCCESS) say(text); });
            } else say(text);
        });
    }
    private void say(String text) {
        boolean fr = text.matches("(?s).*[éèàçù].*");
        tts.setLanguage(fr ? Locale.FRENCH : Locale.ENGLISH);
        tts.setOnUtteranceProgressListener(new android.speech.tts.UtteranceProgressListener() {
            @Override public void onStart(String id) { }
            @Override public void onDone(String id) { if ("loomchat".equals(id)) done(); }
            @Override @Deprecated public void onError(String id) { if ("loomchat".equals(id)) done(); }
        });
        // Long replies: speak sentence by sentence (<= 300 chars per utterance). One huge
        // utterance is refused or cut by some engines even under getMaxSpeechInputLength().
        java.util.List<String> parts = new java.util.ArrayList<>();
        StringBuilder cur = new StringBuilder();
        for (String sent : text.split("(?<=[.!?:;\\n])\\s+")) {
            while (sent.length() > 300) { parts.add(sent.substring(0, 300)); sent = sent.substring(300); }
            if (cur.length() + sent.length() + 1 > 300 && cur.length() > 0) { parts.add(cur.toString()); cur.setLength(0); }
            if (cur.length() > 0) cur.append(' ');
            cur.append(sent);
        }
        if (cur.length() > 0) parts.add(cur.toString());
        for (int n = 0; n < parts.size(); n++) {
            boolean lastPart = n == parts.size() - 1;
            tts.speak(parts.get(n), n == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD, null, lastPart ? "loomchat" : "loomchat-" + n);
        }
    }
    private void done() {
        if (act instanceof MainActivity) ((MainActivity) act).js("window.onSpeakDone && window.onSpeakDone()");
    }
    /** Saves a downloaded Matrix file (base64) to the public Downloads folder. */
    @JavascriptInterface public String saveFile(String name, String mime, String b64) {
        try {
            byte[] data = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
            String safe = name.replaceAll("[\\\\/:*?\"<>|]", "_");
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                android.content.ContentValues v = new android.content.ContentValues();
                v.put(android.provider.MediaStore.Downloads.DISPLAY_NAME, safe);
                v.put(android.provider.MediaStore.Downloads.MIME_TYPE, mime == null || mime.isEmpty() ? "application/octet-stream" : mime);
                android.net.Uri u = act.getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                if (u == null) return "";
                try (java.io.OutputStream o = act.getContentResolver().openOutputStream(u)) { o.write(data); }
                return "Downloads/" + safe;
            }
            java.io.File f = new java.io.File(act.getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS), safe);
            try (java.io.FileOutputStream o = new java.io.FileOutputStream(f)) { o.write(data); }
            return f.getAbsolutePath();
        } catch (Exception e) { return ""; }
    }

    /** Floating head (Android Bubbles, API 30+) for one agent room. avatarB64 = PNG/JPEG base64 or "". */
    @SuppressWarnings("deprecation")
    @JavascriptInterface public String bubble(String room, String name, String avatarB64) {
        if (android.os.Build.VERSION.SDK_INT < 30) return "Bubbles need Android 11+";
        if (room == null || !room.matches("![A-Za-z0-9._:=-]+")) return "bad room";
        try {
            if (android.os.Build.VERSION.SDK_INT >= 33 && act.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                act.runOnUiThread(() -> act.requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 7));
                return "Allow notifications for LoomChat, then tap the bubble button again";
            }
            android.app.NotificationManager nm = act.getSystemService(android.app.NotificationManager.class);
            android.app.NotificationChannel ch = new android.app.NotificationChannel("bubbles", "Agent bubbles",
                    android.app.NotificationManager.IMPORTANCE_HIGH);
            ch.setAllowBubbles(true);
            ch.setSound(null, null);
            nm.createNotificationChannel(ch);
            Icon icon = Icon.createWithResource(act, R.mipmap.ic_launcher);
            if (avatarB64 != null && !avatarB64.isEmpty()) {
                byte[] b = android.util.Base64.decode(avatarB64, android.util.Base64.DEFAULT);
                android.graphics.Bitmap bm = android.graphics.BitmapFactory.decodeByteArray(b, 0, b.length);
                if (bm != null) icon = Icon.createWithAdaptiveBitmap(padForAdaptive(bm));
            }
            String id = "b" + room.replaceAll("[^A-Za-z0-9]", "");
            android.app.Person who = new android.app.Person.Builder().setName(name).setIcon(icon).setKey(id).setImportant(true).build();
            Intent open = new Intent(act, MainActivity.class).setAction(Intent.ACTION_VIEW).putExtra("room", room);
            ShortcutManager sm = act.getSystemService(ShortcutManager.class);
            sm.pushDynamicShortcut(new ShortcutInfo.Builder(act, id).setShortLabel(name).setLongLived(true)
                    .setPerson(who).setIcon(icon).setIntent(open).build());
            Intent target = new Intent(act, BubbleActivity.class).setAction(Intent.ACTION_VIEW).putExtra("room", room)
                    .setData(android.net.Uri.parse("loomchat://bubble/" + id));
            android.app.PendingIntent pi = android.app.PendingIntent.getActivity(act, id.hashCode(), target,
                    android.app.PendingIntent.FLAG_MUTABLE | android.app.PendingIntent.FLAG_UPDATE_CURRENT);
            android.app.Notification.BubbleMetadata meta = new android.app.Notification.BubbleMetadata.Builder(pi, icon)
                    .setDesiredHeight(640).setAutoExpandBubble(false).setSuppressNotification(true).build();
            android.app.Notification n = new android.app.Notification.Builder(act, "bubbles")
                    .setSmallIcon(R.drawable.ic_fg).setContentTitle(name).setContentText("Tap to open " + name)
                    .setShortcutId(id).addPerson(who).setBubbleMetadata(meta)
                    .setStyle(new android.app.Notification.MessagingStyle(who).addMessage(name, System.currentTimeMillis(), who))
                    .setCategory(android.app.Notification.CATEGORY_MESSAGE).build();
            nm.notify(id.hashCode(), n);
            if (android.os.Build.VERSION.SDK_INT >= 31) {
                int pref = nm.getBubblePreference();
                if (pref == android.app.NotificationManager.BUBBLE_PREFERENCE_ALL) return "ok";
                if (pref == android.app.NotificationManager.BUBBLE_PREFERENCE_SELECTED)
                    return "Bubbles are set to 'Selected conversations': pull down the LoomChat notification for " + name
                            + " and tap the bubble icon, or set Settings > Apps > LoomChat > Notifications > Bubbles to 'All conversations'";
                return "Bubbles are off for LoomChat: Settings > Apps > LoomChat > Notifications > Bubbles > 'All conversations'";
            }
            return nm.areBubblesAllowed() ? "ok" : "Bubbles are off for LoomChat: Settings > Apps > LoomChat > Notifications > Bubbles";
        } catch (Exception e) { return "Bubble failed: " + e.getMessage(); }
    }

    private static android.graphics.Bitmap padForAdaptive(android.graphics.Bitmap src) {
        int s = 216, inner = 144;  // adaptive icons keep the centre 2/3
        android.graphics.Bitmap out = android.graphics.Bitmap.createBitmap(s, s, android.graphics.Bitmap.Config.ARGB_8888);
        android.graphics.Canvas c = new android.graphics.Canvas(out);
        c.drawColor(0xFF181B22);
        c.drawBitmap(android.graphics.Bitmap.createScaledBitmap(src, inner, inner, true), (s - inner) / 2f, (s - inner) / 2f, null);
        return out;
    }

    @JavascriptInterface public void stopSpeaking() { act.runOnUiThread(() -> { if (tts != null) tts.stop(); }); }

    /** [{id, name, room}] → dynamic launcher shortcuts (long-press the app icon). */
    @JavascriptInterface public void setShortcuts(String json) {
        try {
            ShortcutManager sm = act.getSystemService(ShortcutManager.class);
            if (sm == null) return;
            JSONArray a = new JSONArray(json);
            int max = Math.min(a.length(), Math.max(1, sm.getMaxShortcutCountPerActivity() - 1));
            List<ShortcutInfo> list = new ArrayList<>();
            for (int i = 0; i < max; i++) {
                JSONObject o = a.getJSONObject(i);
                Intent it = new Intent(act, MainActivity.class).setAction(Intent.ACTION_VIEW)
                        .putExtra("room", o.getString("room")).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
                list.add(new ShortcutInfo.Builder(act, "r" + o.getString("id"))
                        .setShortLabel(o.getString("name")).setLongLabel("Message " + o.getString("name"))
                        .setIcon(Icon.createWithResource(act, R.mipmap.ic_launcher)).setIntent(it).setRank(i).build());
            }
            sm.setDynamicShortcuts(list);
        } catch (Exception e) { /* best effort */ }
    }

    /** Snapshot for the home-screen widget: {waiting:[names], quota:"5h 24% · 7d 28%"}. */
    @JavascriptInterface public void widget(String json) {
        act.getSharedPreferences("widget", Activity.MODE_PRIVATE).edit().putString("snap", json).apply();
        AppWidgetManager m = AppWidgetManager.getInstance(act);
        int[] ids = m.getAppWidgetIds(new ComponentName(act, Widget.class));
        if (ids.length > 0) Widget.update(act, m, ids);
    }
}
