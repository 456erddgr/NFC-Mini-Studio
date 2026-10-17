package net.nfcreader.minireader;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.BatteryManager;
import android.os.Build;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Collections;
import java.util.List;

/**
 * 设备信息工具。
 *
 * 关键修复：targetSdk 31+ 起 Build.SERIAL 恒返回 "unknown"，旧实现用它算哈希，
 * 导致每次启动 SN 都可能变化。这里改为「首次生成后持久化」，保证 SN 稳定。
 */
public final class DeviceInfo {

    private static final String PREF = "nfcmini_device";
    private static final String KEY_SN = "serial";

    private static volatile String snCache;
    private static volatile int batteryCache = -1;
    private static volatile Context appCtx;

    private DeviceInfo() {
    }

    /** 尽早调用一次，之后 batteryCache/snCacheForProtocol 才有上下文。 */
    public static void init(Context ctx) {
        if (ctx == null) return;
        appCtx = ctx.getApplicationContext();
        refreshBattery();
    }

    /** 设备名（用于握手与信标），只保留安全字符。 */
    public static String deviceName() {
        String model = Build.MODEL == null ? "Android" : Build.MODEL.trim();
        if (model.isEmpty()) model = "Android";
        StringBuilder sb = new StringBuilder(model.length());
        for (int i = 0; i < model.length() && sb.length() < 24; i++) {
            char c = model.charAt(i);
            if (c > 32 && c != ' ' && c != '/' && c != '\\' && c != ':') sb.append(c);
            else if (c == ' ') sb.append('_');
        }
        if (sb.length() == 0) sb.append("Android");
        return sb.toString();
    }

    /** 稳定的 12 位大写十六进制 SN（首次生成后持久化）。 */
    public static String serial(Context ctx) {
        String c = snCache;
        if (c != null) return c;
        synchronized (DeviceInfo.class) {
            if (snCache != null) return snCache;
            Context use = ctx != null ? ctx : appCtx;
            String saved = null;
            if (use != null) {
                try {
                    SharedPreferences sp = use.getApplicationContext()
                            .getSharedPreferences(PREF, Context.MODE_PRIVATE);
                    saved = sp.getString(KEY_SN, null);
                    if (saved == null || saved.length() != 12) {
                        saved = buildSerial();
                        sp.edit().putString(KEY_SN, saved).apply();
                    }
                } catch (Throwable t) {
                    saved = buildSerial();
                }
            } else {
                saved = buildSerial();
            }
            snCache = saved;
            return saved;
        }
    }

    /** 协议层 0x01 INFO 用：不要求调用方传 Context。 */
    public static String snCacheForProtocol() {
        return serial(appCtx);
    }

    /** 协议层 0x02 STATE 用：返回最近一次刷新的电量（-1 表示未知）。 */
    public static int batteryCache() {
        return batteryCache;
    }

    /** 刷新电量缓存（UI 定时任务或每次 STATE 命令前调用）。 */
    public static int refreshBattery() {
        Context ctx = appCtx;
        if (ctx == null) return batteryCache;
        int b = readBattery(ctx);
        if (b >= 0) batteryCache = b;
        return batteryCache;
    }

    private static String buildSerial() {
        String seed = Build.FINGERPRINT + "|" + Build.BOARD + "|" + Build.BRAND
                + "|" + Build.DEVICE + "|" + Build.MANUFACTURER
                + "|" + Build.MODEL + "|" + Build.HARDWARE + "|" + Build.PRODUCT;
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] d = md.digest(seed.getBytes("UTF-8"));
            StringBuilder sb = new StringBuilder(12);
            for (int i = 0; i < 6; i++) sb.append(String.format("%02X", d[i] & 0xFF));
            return sb.toString();
        } catch (Throwable t) {
            return String.format("%012X", (System.nanoTime() & 0xFFFFFFFFFFFFL));
        }
    }

    /** 电量百分比（0..100），取不到返回 -1。 */
    public static int readBattery(Context ctx) {
        try {
            IntentFilter f = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
            Intent it = ctx.getApplicationContext().registerReceiver(null, f);
            if (it == null) return -1;
            int level = it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
            int scale = it.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            if (level < 0 || scale <= 0) return -1;
            int pct = Math.round(level * 100f / scale);
            if (pct < 0) pct = 0;
            if (pct > 100) pct = 100;
            return pct;
        } catch (Throwable t) {
            return -1;
        }
    }

    /** 兼容旧调用名。 */
    public static int battery(Context ctx) {
        return readBattery(ctx);
    }

    /**
     * 本机局域网 IPv4，找不到返回 null（不再返回 "—"，
     * 因为旧实现会把 "—" 塞进握手文本，桌面端拿它当 host 必然连不上）。
     */
    public static String localIp() {
        try {
            List<NetworkInterface> all = Collections.list(NetworkInterface.getNetworkInterfaces());
            // 第一轮：只看 wlan / ap / eth 这类真实局域网接口
            for (NetworkInterface ni : all) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                String n = ni.getName() == null ? "" : ni.getName().toLowerCase();
                if (!(n.startsWith("wlan") || n.startsWith("ap") || n.startsWith("eth")
                        || n.startsWith("swlan") || n.startsWith("wifi"))) continue;
                String ip = firstIpv4(ni);
                if (ip != null) return ip;
            }
            // 第二轮：兜底任意非回环 IPv4（排除移动数据网卡）
            for (NetworkInterface ni : all) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                String n = ni.getName() == null ? "" : ni.getName().toLowerCase();
                if (n.startsWith("rmnet") || n.startsWith("ccmni") || n.startsWith("pdp")) continue;
                String ip = firstIpv4(ni);
                if (ip != null) return ip;
            }
        } catch (Throwable ignore) {
        }
        return null;
    }

    private static String firstIpv4(NetworkInterface ni) {
        for (InetAddress a : Collections.list(ni.getInetAddresses())) {
            if (a instanceof Inet4Address && !a.isLoopbackAddress() && !a.isLinkLocalAddress()) {
                String h = a.getHostAddress();
                if (h != null && !h.isEmpty()) return h;
            }
        }
        return null;
    }

    /** 是否连着 Wi-Fi / 热点（手机读卡器要求同一个局域网）。 */
    public static boolean onWifi(Context ctx) {
        Context use = ctx != null ? ctx : appCtx;
        if (use == null) return false;
        try {
            android.net.ConnectivityManager cm = (android.net.ConnectivityManager)
                    use.getApplicationContext().getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return false;
            android.net.NetworkInfo ni = cm.getActiveNetworkInfo();
            if (ni == null || !ni.isConnected()) return false;
            int t = ni.getType();
            return t == android.net.ConnectivityManager.TYPE_WIFI
                    || t == android.net.ConnectivityManager.TYPE_ETHERNET;
        } catch (Throwable t) {
            return false;
        }
    }

    /** Android 版本描述，用于「关于」区域。 */
    public static String osText() {
        return "Android " + Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT + ")";
    }

    /** App 版本名（BuildConfig）。 */
    public static String appVersion() {
        try {
            return BuildConfig.VERSION_NAME + " (" + BuildConfig.VERSION_CODE + ")";
        } catch (Throwable t) {
            return "1.1.0";
        }
    }
}
