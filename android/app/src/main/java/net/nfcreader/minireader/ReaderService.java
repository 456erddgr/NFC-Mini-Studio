package net.nfcreader.minireader;

import net.nfcreader.core.*;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;

/**
 * 前台服务：让「手机当读卡器」的 TCP 服务在切到后台后继续存活。
 *
 * 旧版把服务放在 Activity 里，onPause/onDestroy 就断，桌面端会莫名其妙掉线。
 */
public final class ReaderService extends Service implements ReaderCore.Listener {

    public static final String ACTION_START = "net.nfcreader.minireader.action.START";
    public static final String ACTION_STOP = "net.nfcreader.minireader.action.STOP";
    public static final String ACTION_TOGGLE = "net.nfcreader.minireader.action.TOGGLE";

    private static final String CHANNEL_ID = "nfcm_reader_service";
    private static final int NOTIF_ID = 0x4E43;

    private ReaderCore core;
    private String hint = "正在启动…";
    private PowerManager.WakeLock wakeLock;
    private static final int PROGRESS_ID = 0x4E44;

    /**
     * 状态栏进度：读整卡 / 破解时调用，done/total 变化就刷新一次进度条。
     * 这样用户把手机放在卡上等结果时，切到桌面也能看到还剩多少。
     */
    public static void progress(Context ctx, String title, int done, int total) {
        if (ctx == null) return;
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            NotificationCompat.Builder b = new NotificationCompat.Builder(ctx, CHANNEL_ID)
                    .setSmallIcon(R.drawable.ic_stat_nfc)
                    .setContentTitle(title)
                    .setContentText(Math.max(0, done) + " / " + Math.max(done, total))
                    .setProgress(Math.max(1, total), Math.max(0, done), false)
                    .setOngoing(true)
                    .setOnlyAlertOnce(true)
                    .setPriority(NotificationCompat.PRIORITY_LOW);
            nm.notify(PROGRESS_ID, b.build());
        } catch (Throwable ignore) {
        }
    }

    /** 结束进度条。 */
    public static void clearProgress(Context ctx) {
        if (ctx == null) return;
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(PROGRESS_ID);
        } catch (Throwable ignore) {
        }
    }

    private void holdScreenOn() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) return;
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm == null) return;
            wakeLock = pm.newWakeLock(
                    PowerManager.SCREEN_DIM_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP, "nfcm:server");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        } catch (Throwable ignore) {
        }
    }

    private void releaseScreenOn() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Throwable ignore) {
        }
        wakeLock = null;
    }

    /** 启动服务（Activity 调用）。 */
    public static void start(Context ctx) {
        Intent i = new Intent(ctx, ReaderService.class);
        i.setAction(ACTION_START);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i);
            } else {
                ctx.startService(i);
            }
        } catch (Throwable ignore) {
        }
    }

    /** 停止服务（Activity 调用）。 */
    public static void stop(Context ctx) {
        Intent i = new Intent(ctx, ReaderService.class);
        i.setAction(ACTION_STOP);
        try {
            ctx.startService(i);
        } catch (Throwable ignore) {
        }
        try {
            ctx.stopService(new Intent(ctx, ReaderService.class));
        } catch (Throwable ignore) {
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        core = ReaderCore.get();
        core.init(this);
        core.addListener(this);
        createChannel();
        startForegroundCompat(buildNotification());
        holdScreenOn();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            core.stop();
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }
        // 默认（含 ACTION_START / 系统重启 / null）都确保服务在跑
        String err = core.start();
        if (err != null) {
            hint = err;
        } else {
            hint = "读卡服务运行中";
        }
        updateNotification();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        try {
            if (core != null) {
                core.removeListener(this);
                core.stop();
            }
        } catch (Throwable ignore) {
        }
        releaseScreenOn();
        clearProgress(this);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ------------------------------------------------------- 通知

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "读卡服务", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("手机当读卡器时保持后台连接");
            ch.setShowBadge(false);
            ch.setSound(null, null);
            nm.createNotificationChannel(ch);
        } catch (Throwable ignore) {
        }
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(this, 1, open, flags);

        Intent stopIntent = new Intent(this, ReaderService.class);
        stopIntent.setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 2, stopIntent, flags);

        String ip = DeviceInfo.localIp();
        String line1 = (ip == null ? "未连接局域网" : ip + ":" + core.server().getPort())
                + " · " + core.clientCount() + " 个连接";

        NotificationCompat.Builder b = new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_nfc)
                .setContentTitle("NFC 手机读卡器")
                .setContentText(line1)
                .setSubText(hint)
                .setOngoing(true)
                .setShowWhen(false)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setContentIntent(pi)
                .addAction(0, "停止", stopPi);
        return b.build();
    }

    private void updateNotification() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.notify(NOTIF_ID, buildNotification());
        } catch (Throwable ignore) {
        }
    }

    private void startForegroundCompat(Notification n) {
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            } else {
                startForeground(NOTIF_ID, n);
            }
        } catch (Throwable t) {
            try {
                startForeground(NOTIF_ID, n);
            } catch (Throwable ignore) {
            }
        }
    }

    private void stopForegroundCompat() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE);
            } else {
                //noinspection deprecation
                stopForeground(true);
            }
        } catch (Throwable ignore) {
        }
    }

    // ------------------------------------------------------- 回调

    @Override
    public void onLog(String line) {
        // 日志面板由 Activity 自己处理，这里不重复
    }

    @Override
    public void onServerState(boolean running, int clients, String h) {
        if (h != null) hint = h;
        updateNotification();
    }

    @Override
    public void onCardState() {
        // 卡片变化不影响通知内容
    }
}
