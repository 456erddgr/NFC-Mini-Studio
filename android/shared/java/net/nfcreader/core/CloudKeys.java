package net.nfcreader.core;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 官方云端密钥库（小程序用的那条链路）：按 UID 拿到「这张卡的候选密钥全表」。
 *
 * 和小程序/桌面端完全一致：
 *   POST {base}/submit  {"uid": "8位十六进制"}  Authorization: 内置 token
 *   GET  {base}/query?id=任务号  轮询到 status=completed，payload.result 就是密钥全表
 *
 * 手机没有 18000 次随机数采集能力，做不了 HardNested/mfkey32；这条云端 keygen + 本机逐把
 * 认证就是手机端能做到的「官方方案」，命中率比纯内置字典高得多。
 */
public final class CloudKeys {

    public static final String BASE = "https://nfctoolsapp.nfcreader.net/keygen";
    private static final String TOKEN = "7a422ed2462aaabd3bda83cebe62f5602f83e8fca3cda089407db9aa5000c8d6";
    private static final String UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroMessenger/8.0.0 miniProgram";
    private static final int POLL_INTERVAL_MS = 1200;
    private static final int POLL_TIMEOUT_MS = 15000;

    private CloudKeys() {
    }

    /** 把 88AB12CD / aa:bb:cc:dd / 反序 UID 之类都归一成 8 位十六进制文本。 */
    public static String normalizeUid(String uid) {
        if (uid == null) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < uid.length(); i++) {
            char c = uid.charAt(i);
            if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
                sb.append(c);
            }
        }
        String hex = sb.toString().toUpperCase();
        if (hex.length() > 8) hex = hex.substring(hex.length() - 8);
        while (hex.length() < 8) hex = "0" + hex;
        return hex;
    }

    /**
     * 查询官方密钥库。成功返回候选密钥列表；网络不通 / 未收录 / 超时都返回空列表（不抛异常）。
     */
    public static List<byte[]> lookup(String uid, CardOps.Progress p) {
        List<byte[]> out = new ArrayList<>();
        String u = normalizeUid(uid);
        if (u.isEmpty()) return out;
        try {
            if (p != null) p.on(0, 1, "官方云端密钥库：查询 UID " + u + " …");
            long uidInt = Long.parseLong(u, 16);
            JSONObject body = new JSONObject();
            body.put("uid", String.format("%08X", uidInt));
            JSONObject submit = request(BASE + "/submit", "POST", body.toString());
            if (submit == null || submit.optInt("errcode", 1) != 0) {
                if (p != null) {
                    p.on(0, 1, "官方云端密钥库：提交失败"
                            + (submit == null ? "" : "（" + submit.optString("errmsg") + "）"));
                }
                return out;
            }
            String jobId = submit.optString("payload", "");
            if (jobId.isEmpty()) return out;
            long deadline = System.currentTimeMillis() + POLL_TIMEOUT_MS;
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(POLL_INTERVAL_MS);
                JSONObject query = request(BASE + "/query?id=" + jobId, "GET", null);
                if (query == null || query.optInt("errcode", 1) != 0) continue;
                JSONObject payload = query.optJSONObject("payload");
                if (payload == null) continue;
                String status = payload.optString("status", "").toLowerCase();
                List<byte[]> keys = keysOf(payload);
                if (!keys.isEmpty()) {
                    if (p != null) p.on(0, 1, "官方云端密钥库：拿到 " + keys.size() + " 把候选密钥");
                    return keys;
                }
                if (status.contains("completed") || status.contains("success")
                        || status.contains("done") || status.contains("finish")) {
                    if (p != null) p.on(0, 1, "官方云端密钥库：没有收录这张卡");
                    return out;
                }
                if (status.contains("fail") || status.contains("error")) {
                    if (p != null) p.on(0, 1, "官方云端密钥库：查询失败");
                    return out;
                }
            }
            if (p != null) p.on(0, 1, "官方云端密钥库：还在计算，先用内置字典");
        } catch (Throwable t) {
            if (p != null) p.on(0, 1, "官方云端密钥库：连不上（" + t.getMessage() + "），用内置字典");
        }
        return out;
    }

    private static List<byte[]> keysOf(JSONObject payload) {
        List<byte[]> keys = new ArrayList<>();
        JSONArray arr = payload.optJSONArray("result");
        if (arr == null) return keys;
        for (int i = 0; i < arr.length(); i++) {
            String h = arr.optString(i, "");
            byte[] k = CardOps.hex(h);
            if (k != null && k.length == 6) {
                boolean dup = false;
                for (byte[] old : keys) {
                    if (java.util.Arrays.equals(old, k)) {
                        dup = true;
                        break;
                    }
                }
                if (!dup) keys.add(k);
            }
        }
        return keys;
    }

    private static JSONObject request(String url, String method, String body) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(6000);
            c.setReadTimeout(8000);
            c.setRequestMethod(method);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Authorization", TOKEN);
            c.setRequestProperty("User-Agent", UA);
            if (body != null) {
                c.setDoOutput(true);
                OutputStream os = c.getOutputStream();
                os.write(body.getBytes(StandardCharsets.UTF_8));
                os.flush();
                os.close();
            }
            int code = c.getResponseCode();
            InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
            if (is == null) return null;
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
            is.close();
            String text = new String(bos.toByteArray(), StandardCharsets.UTF_8);
            return new JSONObject(text);
        } catch (Throwable t) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
