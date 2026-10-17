package net.nfcreader.core;

import android.content.Context;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;

/**
 * 统一震动反馈。
 *
 * 旧版只有零星几处 performHapticFeedback，而且强度用的是系统默认，
 * 在多数机器上几乎感觉不到。这里直接走 VibrationEffect 自定义振幅，
 * 并且把强度做成三档（关 / 中 / 强），用户可在设置里调。
 */
public final class Haptics {

    public static final int OFF = 0;
    public static final int LIGHT = 1;
    public static final int MEDIUM = 2;
    public static final int STRONG = 3;

    private static int level = STRONG;

    private Haptics() {
    }

    public static void setLevel(int lv) {
        level = Math.max(OFF, Math.min(STRONG, lv));
    }

    public static int getLevel() {
        return level;
    }

    private static Vibrator vibrator(Context ctx) {
        if (ctx == null) return null;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                VibratorManager vm = (VibratorManager) ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
                return vm == null ? null : vm.getDefaultVibrator();
            }
            return (Vibrator) ctx.getSystemService(Context.VIBRATOR_SERVICE);
        } catch (Throwable ignore) {
            return null;
        }
    }

    private static void buzz(Context ctx, long ms, int amplitude) {
        if (level == OFF) return;
        Vibrator v = vibrator(ctx);
        if (v == null || !v.hasVibrator()) return;
        int amp = amplitude;
        if (level == LIGHT) amp = Math.max(1, (amp * 55) / 100);
        else if (level == MEDIUM) amp = amp;
        else if (level == STRONG) amp = Math.max(1, (amp * 135) / 100);
        amp = Math.max(1, Math.min(255, amp));
        ms = level == LIGHT ? Math.max(1, (ms * 7) / 10) : (level == STRONG ? (ms * 5) / 4 : ms);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                v.vibrate(VibrationEffect.createOneShot(Math.max(1, ms), amp));
            } else {
                v.vibrate(Math.max(1, ms));
            }
        } catch (Throwable ignore) {
        }
    }

    private static void pattern(Context ctx, long[] timings, int[] amps) {
        if (level == OFF) return;
        Vibrator v = vibrator(ctx);
        if (v == null || !v.hasVibrator()) return;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                int[] scaled = new int[amps.length];
                for (int i = 0; i < amps.length; i++) {
                    int a = amps[i];
                    if (level == LIGHT) a = Math.max(1, (a * 55) / 100);
                    else if (level == STRONG) a = Math.max(1, (a * 135) / 100);
                    scaled[i] = Math.max(1, Math.min(255, a));
                }
                v.vibrate(VibrationEffect.createWaveform(timings, scaled, -1));
            } else {
                v.vibrate(timings, -1);
            }
        } catch (Throwable ignore) {
        }
    }

    /** 轻触：点按钮、切换开关。 */
    public static void tap(Context ctx) {
        buzz(ctx, 14, 150);
    }

    /** 中等：选中、连接成功。 */
    public static void select(Context ctx) {
        buzz(ctx, 24, 225);
    }

    /** 成功：操作完成。 */
    public static void success(Context ctx) {
        pattern(ctx, new long[]{0, 20, 36, 28}, new int[]{0, 205, 0, 255});
    }

    /** 失败 / 警告。 */
    public static void error(Context ctx) {
        pattern(ctx, new long[]{0, 32, 54, 32, 54, 32}, new int[]{0, 255, 0, 225, 0, 255});
    }

    /** 读卡器发现卡片时的「咔哒」。 */
    public static void cardDetected(Context ctx) {
        pattern(ctx, new long[]{0, 14, 26, 20}, new int[]{0, 225, 0, 255});
    }

    /** 连续滑动（滑杆）的刻度反馈。 */
    public static void tick(Context ctx) {
        buzz(ctx, 8, 120);
    }
}
