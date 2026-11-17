package org.loommesh.loomchat;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.widget.RemoteViews;
import org.json.JSONArray;
import org.json.JSONObject;

/** Home-screen widget: which rooms are waiting for you (+ quota when a Loom API is set). Refreshed by the app on each sync. */
public class Widget extends AppWidgetProvider {
    @Override public void onUpdate(Context c, AppWidgetManager m, int[] ids) { update(c, m, ids); }

    static void update(Context c, AppWidgetManager m, int[] ids) {
        String title = "LoomChat", sub = "Open the app to refresh", quota = "";
        try {
            JSONObject o = new JSONObject(c.getSharedPreferences("widget", Context.MODE_PRIVATE).getString("snap", "{}"));
            JSONArray w = o.optJSONArray("waiting");
            int n = w == null ? 0 : w.length();
            title = n == 0 ? "Nobody is waiting" : (n == 1 ? "1 agent needs you" : n + " agents need you");
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < n; i++) { if (i > 0) sb.append(", "); sb.append(w.getString(i)); }
            if (n > 0) sub = sb.toString(); else sub = "All caught up";
            quota = o.optString("quota", "");
            String up = o.optString("updated", "");
            if (!up.isEmpty()) quota = (quota.isEmpty() ? "" : quota + " · ") + "updated " + up;
        } catch (Exception e) { /* keep defaults */ }
        PendingIntent pi = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        for (int id : ids) {
            RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget);
            v.setTextViewText(R.id.w_title, title);
            v.setTextViewText(R.id.w_sub, sub);
            v.setTextViewText(R.id.w_quota, quota);
            v.setOnClickPendingIntent(R.id.w_root, pi);
            m.updateAppWidget(id, v);
        }
    }
}
