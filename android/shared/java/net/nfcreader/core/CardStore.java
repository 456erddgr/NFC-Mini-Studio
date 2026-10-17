package net.nfcreader.core;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 卡夹：把整卡 dump 存成 JSON 文件（应用私有目录）。
 *
 * 和电脑端「卡夹」语义对齐：一张卡 = 一条记录，含 UID、SAK、ATQA、
 * 每个块的十六进制，以及写入时用到的密钥。
 */
public final class CardStore {

    public static final class Card {
        public String id = "";
        public String name = "";
        public String uid = "";
        public String atqa = "";
        public int sak = -1;
        public int blockCount = 0;
        public String kind = "";
        public long savedAt = 0L;
        public String key = "";
        public String[] blocks = new String[0];

        public String displayName() {
            return (name == null || name.isEmpty()) ? ("卡片 " + uid) : name;
        }
    }

    private CardStore() {
    }

    private static File dir(Context ctx) {
        File d = new File(ctx.getFilesDir(), "cards");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    public static String save(Context ctx, Card c) {
        if (c == null) return null;
        if (c.id == null || c.id.isEmpty()) {
            c.id = "card_" + System.currentTimeMillis();
        }
        if (c.savedAt == 0L) c.savedAt = System.currentTimeMillis();
        try {
            JSONObject o = new JSONObject();
            o.put("id", c.id);
            o.put("name", c.name);
            o.put("uid", c.uid);
            o.put("atqa", c.atqa);
            o.put("sak", c.sak);
            o.put("blockCount", c.blockCount);
            o.put("kind", c.kind);
            o.put("savedAt", c.savedAt);
            o.put("key", c.key);
            JSONArray arr = new JSONArray();
            for (String b : c.blocks) arr.put(b);
            o.put("blocks", arr);
            File f = new File(dir(ctx), c.id + ".json");
            try (FileOutputStream fos = new FileOutputStream(f);
                 OutputStreamWriter w = new OutputStreamWriter(fos, StandardCharsets.UTF_8)) {
                w.write(o.toString());
            }
            return c.id;
        } catch (Throwable t) {
            return null;
        }
    }

    public static List<Card> list(Context ctx) {
        List<Card> out = new ArrayList<>();
        File[] files = dir(ctx).listFiles();
        if (files == null) return out;
        for (File f : files) {
            if (!f.getName().endsWith(".json")) continue;
            Card c = read(f);
            if (c != null) out.add(c);
        }
        Collections.sort(out, (a, b) -> Long.compare(b.savedAt, a.savedAt));
        return out;
    }

    public static Card get(Context ctx, String id) {
        if (id == null) return null;
        return read(new File(dir(ctx), id + ".json"));
    }

    public static boolean remove(Context ctx, String id) {
        if (id == null) return false;
        File f = new File(dir(ctx), id + ".json");
        return f.exists() && f.delete();
    }

    private static Card read(File f) {
        try (FileInputStream fis = new FileInputStream(f);
             InputStreamReader r = new InputStreamReader(fis, StandardCharsets.UTF_8)) {
            StringBuilder sb = new StringBuilder();
            char[] buf = new char[4096];
            int n;
            while ((n = r.read(buf)) > 0) sb.append(buf, 0, n);
            JSONObject o = new JSONObject(sb.toString());
            Card c = new Card();
            c.id = o.optString("id");
            c.name = o.optString("name");
            c.uid = o.optString("uid");
            c.atqa = o.optString("atqa");
            c.sak = o.optInt("sak", -1);
            c.blockCount = o.optInt("blockCount", 0);
            c.kind = o.optString("kind");
            c.savedAt = o.optLong("savedAt", f.lastModified());
            c.key = o.optString("key");
            JSONArray arr = o.optJSONArray("blocks");
            if (arr != null) {
                c.blocks = new String[arr.length()];
                for (int i = 0; i < arr.length(); i++) c.blocks[i] = arr.optString(i, "");
            }
            return c;
        } catch (Throwable t) {
            return null;
        }
    }
}
