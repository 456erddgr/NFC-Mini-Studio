package net.nfcreader.core;

import android.os.Handler;
import android.os.Looper;

/**
 * 极简后台任务工具：读整卡 / 破解都要几秒，绝不能放在主线程。
 */
public final class Task {

    public interface Work<T> {
        T run() throws Exception;
    }

    public interface Done<T> {
        void on(T value, Exception error);
    }

    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private Task() {
    }

    public static <T> void bg(final Work<T> work, final Done<T> done) {
        Thread t = new Thread(() -> {
            T value = null;
            Exception err = null;
            try {
                value = work.run();
            } catch (Exception e) {
                err = e;
            }
            final T v = value;
            final Exception e2 = err;
            MAIN.post(() -> {
                if (done != null) done.on(v, e2);
            });
        }, "nfcm-task");
        t.setDaemon(true);
        t.start();
    }

    public static void bg(final Runnable work) {
        bg(() -> {
            work.run();
            return null;
        }, null);
    }

    public static void post(Runnable r) {
        MAIN.post(r);
    }
}
