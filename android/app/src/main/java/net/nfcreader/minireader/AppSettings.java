package net.nfcreader.minireader;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 轻量设置存储（SharedPreferences 包装）。
 * 所有字段都有合理默认值，读失败也不会崩。
 */
public final class AppSettings {

    private static final String PREF = "nfcmini_settings";

    /** TCP 服务端口（桌面端 NetTransport 默认连 8765）。 */
    public int tcpPort = ReaderServer.DEFAULT_TCP_PORT;
    /** 是否保持屏幕常亮（关掉后息屏会停止读卡）。 */
    public boolean keepScreenOn = true;
    /** 读到卡片时震动。 */
    public boolean vibrate = true;
    /** 打开 App 是否自动启动读卡服务。 */
    public boolean autoStart = true;
    /** 浅色主题。 */
    public boolean lightTheme = false;
    /** 是否启用液态玻璃效果（关闭可显著降低低端机开销）。 */
    public boolean glass = true;
    /** 桌面模糊强度 0..40。 */
    public int blurRadius = 18;
    /** 背景预设 id 或 "custom"。 */
    public String wallpaperId = WallpaperFactory.DEFAULT_ID;
    /** 自定义背景文件路径（images 目录下的文件名）。 */
    public String customWallpaper = "";
    /** 日志面板是否默认展开。 */
    public boolean showLog = true;

    private final SharedPreferences sp;

    public AppSettings(Context ctx) {
        sp = ctx.getApplicationContext().getSharedPreferences(PREF, Context.MODE_PRIVATE);
        reload();
    }

    public void reload() {
        try {
            tcpPort = sp.getInt("tcpPort", ReaderServer.DEFAULT_TCP_PORT);
            if (tcpPort < 1024 || tcpPort > 65535) tcpPort = ReaderServer.DEFAULT_TCP_PORT;
            keepScreenOn = sp.getBoolean("keepScreenOn", true);
            vibrate = sp.getBoolean("vibrate", true);
            autoStart = sp.getBoolean("autoStart", true);
            lightTheme = sp.getBoolean("lightTheme", false);
            glass = sp.getBoolean("glass", true);
            blurRadius = sp.getInt("blurRadius", 18);
            if (blurRadius < 0) blurRadius = 0;
            if (blurRadius > 40) blurRadius = 40;
            wallpaperId = sp.getString("wallpaperId", WallpaperFactory.DEFAULT_ID);
            customWallpaper = sp.getString("customWallpaper", "");
            showLog = sp.getBoolean("showLog", true);
        } catch (Throwable ignore) {
        }
    }

    public void save() {
        try {
            sp.edit()
                    .putInt("tcpPort", tcpPort)
                    .putBoolean("keepScreenOn", keepScreenOn)
                    .putBoolean("vibrate", vibrate)
                    .putBoolean("autoStart", autoStart)
                    .putBoolean("lightTheme", lightTheme)
                    .putBoolean("glass", glass)
                    .putInt("blurRadius", blurRadius)
                    .putString("wallpaperId", wallpaperId)
                    .putString("customWallpaper", customWallpaper)
                    .putBoolean("showLog", showLog)
                    .apply();
        } catch (Throwable ignore) {
        }
    }
}
