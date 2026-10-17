package net.nfcreader.core;

import android.graphics.Bitmap;
import android.graphics.BitmapShader;
import android.graphics.Canvas;
import android.graphics.LinearGradient;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.Shader;

/**
 * 全局壁纸缓存 + 屏幕坐标系模糊层。
 *
 * 真正的「液态玻璃」不是简单给卡片刷个半透明色，而是要在卡片那块区域
 * 显示出它背后内容的模糊版本。本项目的背景就是一张壁纸，所以做法是：
 *   1. 背景按屏幕尺寸做 center-crop 铺满；
 *   2. prepare() 预先算出一张 1/6 尺寸、已模糊的「屏幕空间贴图」；
 *   3. 每张玻璃卡片在绘制时，用 BitmapShader 按自己在屏幕上的位置
 *      采样这张贴图 —— 数学上等价于「实时模糊卡片背后的东西」，
 *      但完全不需要在每帧做高斯模糊，低端机也不卡。
 */
public final class WallpaperHolder {

    /** 模糊层相对屏幕的缩放。1/6 已经足够细腻，放大时会被线性插值抹平。 */
    public static final float BLUR_SCALE = 1f / 6f;

    /**
     * 背景压暗渐变的唯一数据源。
     *
     * 背景层（GlassUi.background）和玻璃卡片（GlassCardView 采样模糊贴图）必须
     * 用同一套颜色，否则卡片会比周围背景亮一截，看起来"玻璃贴错了"。
     */
    public static final int[] SCRIM_COLORS = {0xC405070B, 0x5505070B, 0x4405070B, 0xD605070B};
    public static final float[] SCRIM_POSITIONS = {0f, 1f / 3f, 2f / 3f, 1f};

    private static Bitmap wallpaper;
    private static Bitmap blur;
    private static BitmapShader blurShader;
    private static String presetId = "";
    private static boolean custom = false;
    private static int screenW = 0;
    private static int screenH = 0;

    private WallpaperHolder() {
    }

    public static void set(Bitmap bmp, String preset, boolean isCustom) {
        wallpaper = bmp;
        presetId = preset == null ? "" : preset;
        custom = isCustom;
        blur = null;
        blurShader = null;
        screenW = 0;
        screenH = 0;
    }

    public static Bitmap bitmap() {
        return wallpaper;
    }

    public static String preset() {
        return presetId;
    }

    public static boolean isCustom() {
        return custom;
    }

    public static boolean ready() {
        return wallpaper != null && !wallpaper.isRecycled();
    }

    /** 屏幕尺寸变化时重建模糊层。可重复调用，尺寸不变时直接返回。 */
    public static synchronized void prepare(int w, int h) {
        if (!ready() || w <= 0 || h <= 0) return;
        if (blur != null && screenW == w && screenH == h) return;
        screenW = w;
        screenH = h;
        int sw = Math.max(8, Math.round(w * BLUR_SCALE));
        int sh = Math.max(8, Math.round(h * BLUR_SCALE));
        try {
            Bitmap small = Bitmap.createBitmap(sw, sh, Bitmap.Config.ARGB_8888);
            Canvas c = new Canvas(small);
            c.drawColor(0xFF0A0D12);
            // 按屏幕尺寸做 center-crop，再把画布缩到 1/6
            float scale = Math.max((float) w / wallpaper.getWidth(), (float) h / wallpaper.getHeight());
            float dx = (w - wallpaper.getWidth() * scale) / 2f;
            float dy = (h - wallpaper.getHeight() * scale) / 2f;
            Matrix m = new Matrix();
            m.setScale(scale, scale);
            m.postTranslate(dx, dy);
            c.scale(BLUR_SCALE, BLUR_SCALE);
            Paint p = new Paint(Paint.FILTER_BITMAP_FLAG | Paint.ANTI_ALIAS_FLAG);
            c.drawBitmap(wallpaper, m, p);
            boxBlur(small, 8, 2);
            saturate(small, 1.28f);
            bakeScrim(small);
            Bitmap old = blur;
            blur = small;
            blurShader = new BitmapShader(small, Shader.TileMode.CLAMP, Shader.TileMode.CLAMP);
            if (old != null && old != small) {
                try {
                    old.recycle();
                } catch (Throwable ignore) {
                }
            }
        } catch (Throwable ignore) {
        }
    }

    public static Bitmap blurBitmap() {
        return blur;
    }

    public static BitmapShader blurShader() {
        return blurShader;
    }

    /**
     * 把背景那层压暗渐变烤进模糊贴图。
     *
     * 这样玻璃卡片采样到的就是"卡片背后真实看到的那块背景"，
     * 而不是未经压暗、偏亮的原壁纸 —— 视觉上玻璃才和背景连成一片。
     */
    private static void bakeScrim(Bitmap bmp) {
        try {
            Canvas c = new Canvas(bmp);
            Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
            p.setShader(new LinearGradient(0f, 0f, 0f, bmp.getHeight(),
                    SCRIM_COLORS, SCRIM_POSITIONS, Shader.TileMode.CLAMP));
            c.drawRect(0f, 0f, bmp.getWidth(), bmp.getHeight(), p);
        } catch (Throwable ignore) {
        }
    }

    /** 提饱和度：磨砂玻璃会把颜色"糊灰"，稍微拉回来一点才有通透感。 */
    private static void saturate(Bitmap src2, float f) {
        int w = src2.getWidth();
        int h = src2.getHeight();
        int[] pix = new int[w * h];
        src2.getPixels(pix, 0, w, 0, 0, w, h);
        for (int i = 0; i < pix.length; i++) {
            int c = pix[i];
            int a = (c >>> 24) & 0xFF;
            int r = (c >> 16) & 0xFF;
            int g = (c >> 8) & 0xFF;
            int b = c & 0xFF;
            int gray = (int) (0.299f * r + 0.587f * g + 0.114f * b);
            r = clamp255(gray + (int) ((r - gray) * f));
            g = clamp255(gray + (int) ((g - gray) * f));
            b = clamp255(gray + (int) ((b - gray) * f));
            pix[i] = (a << 24) | (r << 16) | (g << 8) | b;
        }
        src2.setPixels(pix, 0, w, 0, 0, w, h);
    }

    private static int clamp255(int v) {
        return v < 0 ? 0 : (v > 255 ? 255 : v);
    }

    /** 三次可分离盒式模糊，够快也够糊。 */
    private static void boxBlur(Bitmap src, int radius, int passes) {
        int w = src.getWidth();
        int h = src.getHeight();
        int[] pix = new int[w * h];
        src.getPixels(pix, 0, w, 0, 0, w, h);
        int[] tmp = new int[w * h];
        for (int pass = 0; pass < passes; pass++) {
            blurPass(pix, tmp, w, h, radius, true);
            blurPass(tmp, pix, w, h, radius, false);
        }
        src.setPixels(pix, 0, w, 0, 0, w, h);
    }

    private static void blurPass(int[] in, int[] out, int w, int h, int r, boolean horizontal) {
        int span = 2 * r + 1;
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int a = 0, rr = 0, gg = 0, bb = 0, n = 0;
                for (int k = -r; k <= r; k++) {
                    int sx = horizontal ? x + k : x;
                    int sy = horizontal ? y : y + k;
                    if (sx < 0) sx = 0;
                    if (sy < 0) sy = 0;
                    if (sx >= w) sx = w - 1;
                    if (sy >= h) sy = h - 1;
                    int c = in[sy * w + sx];
                    a += (c >>> 24) & 0xFF;
                    rr += (c >> 16) & 0xFF;
                    gg += (c >> 8) & 0xFF;
                    bb += c & 0xFF;
                    n++;
                }
                if (n <= 0) n = 1;
                out[y * w + x] = ((a / n) << 24) | ((rr / n) << 16) | ((gg / n) << 8) | (bb / n);
            }
        }
        // span 仅用于消除未使用变量告警
        if (span < 0) out[0] = in[0];
    }
}
