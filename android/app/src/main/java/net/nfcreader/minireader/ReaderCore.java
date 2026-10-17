package net.nfcreader.minireader;

import android.content.Context;
import android.net.wifi.WifiManager;
import android.os.Handler;
import android.os.Looper;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 应用级单例：持有 NFC 引擎、TCP 服务与设置。
 *
 * 之所以单独抽出来，是因为旧版把所有状态放在 MainActivity 里，
 * 一旦用户切到后台 Activity 被回收，服务就跟着没了。
 * 现在由 ReaderService（前台服务）持有，Activity 只是观察者。
 */
public final class ReaderCore implements NfcEngine.Log, ReaderServer.Listener {

    /** UI 观察者。 */
    public interface Listener {
        /** 新日志行。 */
        void onLog(String line);

        /** 服务状态变化。 */
        void onServerState(boolean running, int clients, String hint);

        /** 卡片状态可能变化（建议刷新）。 */
        void onCardState();
    }

    private static volatile ReaderCore INSTANCE;

    public static ReaderCore get() {
        ReaderCore c = INSTANCE;
        if (c == null) {
            synchronized (ReaderCore.class) {
                if (INSTANCE == null) INSTANCE = new ReaderCore();
                c = INSTANCE;
            }
        }
        return c;
    }

    private final List<Listener> listeners = new CopyOnWriteArrayList<>();
    private final Handler main = new Handler(Looper.getMainLooper());

    private volatile boolean initialized = false;
    private Context appCtx;
    private AppSettings settings;
    private NfcEngine engine;
    private ReaderServer server;
    private WifiManager.MulticastLock multicastLock;

    private final List<String> logLines = new CopyOnWriteArrayList<>();

    private ReaderCore() {
    }

    public synchronized void init(Context ctx) {
        if (initialized) return;
        appCtx = ctx.getApplicationContext();
        DeviceInfo.init(appCtx);
        settings = new AppSettings(appCtx);
        engine = new NfcEngine(this);
        server = new ReaderServer(engine);
        server.addListener(this);
        initialized = true;
    }

    public boolean isInitialized() {
        return initialized;
    }

    public Context context() {
        return appCtx;
    }

    public AppSettings settings() {
        return settings;
    }

    public NfcEngine engine() {
        return engine;
    }

    public ReaderServer server() {
        return server;
    }

    public boolean isRunning() {
        return server != null && server.isRunning();
    }

    public int clientCount() {
        return server == null ? 0 : server.clientCount();
    }

    /** 启动服务，成功返回 null，失败返回中文原因。 */
    public String start() {
        if (!initialized) return "初始化未完成";
        acquireMulticast();
        String err = server.start(settings.tcpPort);
        if (err != null) releaseMulticast();
        return err;
    }

    public void stop() {
        if (!initialized) return;
        server.stop();
        releaseMulticast();
    }

    // ------------------------------------------------------ 观察者管理

    public void addListener(Listener l) {
        if (l != null && !listeners.contains(l)) listeners.add(l);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public List<String> logSnapshot() {
        return new java.util.ArrayList<>(logLines);
    }

    public void clearLog() {
        logLines.clear();
    }

    // ------------------------------------------------------ 回调实现

    @Override
    public void onLog(String line) {
        if (line == null) return;
        String stamped = timeText() + "  " + line;
        logLines.add(stamped);
        while (logLines.size() > 300) logLines.remove(0);
        main.post(() -> {
            for (Listener l : listeners) {
                try {
                    l.onLog(stamped);
                } catch (Throwable ignore) {
                }
            }
        });
    }

    @Override
    public void onState(boolean running, int clients, String hint) {
        main.post(() -> {
            for (Listener l : listeners) {
                try {
                    l.onServerState(running, clients, hint);
                } catch (Throwable ignore) {
                }
            }
        });
    }

    /** 由 Activity 在 NFC 回调里调用，转发给引擎并通知 UI。 */
    public void onTag(android.nfc.Tag tag) {
        if (engine == null) return;
        engine.setTag(tag);
        main.post(() -> {
            for (Listener l : listeners) {
                try {
                    l.onCardState();
                } catch (Throwable ignore) {
                }
            }
        });
    }

    /** 卡片是否还在（用于 UI 判定）。 */
    public boolean hasCard() {
        return engine != null && engine.getTag() != null;
    }

    // ------------------------------------------------------ 内部

    private void acquireMulticast() {
        if (multicastLock != null) return;
        try {
            WifiManager wm = (WifiManager) appCtx.getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return;
            WifiManager.MulticastLock lock = wm.createMulticastLock("nfcm-reader");
            lock.setReferenceCounted(false);
            lock.acquire();
            multicastLock = lock;
        } catch (Throwable ignore) {
        }
    }

    private void releaseMulticast() {
        WifiManager.MulticastLock lock = multicastLock;
        multicastLock = null;
        if (lock != null) {
            try {
                if (lock.isHeld()) lock.release();
            } catch (Throwable ignore) {
            }
        }
    }

    private static String timeText() {
        long t = System.currentTimeMillis();
        java.util.Calendar c = java.util.Calendar.getInstance();
        c.setTimeInMillis(t);
        return String.format("%02d:%02d:%02d",
                c.get(java.util.Calendar.HOUR_OF_DAY),
                c.get(java.util.Calendar.MINUTE),
                c.get(java.util.Calendar.SECOND));
    }
}
