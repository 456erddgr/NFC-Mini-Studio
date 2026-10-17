package net.nfcreader.minireader;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.BlurMaskFilter;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.net.Uri;
import android.util.LruCache;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

/**
 * 背景壁纸工厂。
 *
 * 内置背景不是图片资源（那样 APK 会变大），而是在运行时用 Canvas 画出来的渐变 + 光斑，
 * 因此可以按屏幕尺寸生成、也不会拖慢首包体积。自定义背景则从相册选图后复制到
 * 应用私有目录，之后每次启动直接读文件。
 */
public final class WallpaperFactory {

    public static final String DEFAULT_ID = "night";

    /** 内置背景 id。 */
    public static final String[] IDS = {
            "night", "aurora", "sunset", "ocean", "forest", "mono", "rose"
    };

    /** 内置背景中文名。 */
    public static final String[] NAMES = {
            "午夜蓝", "极光", "落日", "深海", "森林", "石墨", "雾玫"
    };

    private static final int[][] COLORS = {
            // night
            {0xFF0B1020, 0xFF151C33, 0xFF2A3A6B, 0xFF6E4FA8, 0xFF123B5A},
            // aurora
            {0xFF04131A, 0xFF0A2230, 0xFF12E0A0, 0xFF2C7BE5, 0xFF7C4DFF},
            // sunset
            {0xFF2A0E1A, 0xFF3D1220, 0xFFFF6B6B, 0xFFFFA94D, 0xFFFF3D81},
            // ocean
            {0xFF061A2B, 0xFF0A2740, 0xFF2AB0FF, 0xFF00E0C6, 0xFF0A4A6B},
            // forest
            {0xFF091A10, 0xFF12271A, 0xFF4CAF50, 0xFF8BC34A, 0xFF1B5E20},
            // mono
            {0xFF0E0E11, 0xFF1A1A1F, 0xFF3A3A44, 0xFF55555F, 0xFF26262C},
            // rose
            {0xFF1B0F14, 0xFF2A1620, 0xFFE0609A, 0xFF8E5BD9, 0xFF3D1B33}
    };

    private static final LruCache<String, Bitmap> CACHE = new LruCache<String, Bitmap>(6) {
        @Override
        protected int sizeOf(String key, Bitmap value) {
            return Math.max(1, value.getByteCount() / 1024);
        }
    };

    private WallpaperFactory() {
    }

    public static String nameOf(String id) {
        for (int i = 0; i < IDS.length; i++) if (IDS[i].equals(id)) return NAMES[i];
        return "自定义";
    }

    public static int indexOf(String id) {
        for (int i = 0; i < IDS.length; i++) if (IDS[i].equals(id)) return i;
        return -1;
    }

    /** 取某个预设的两端主色，供 UI 画色板圆点用。 */
    public static int[] swatchColors(String id) {
        int idx = indexOf(id);
        if (idx < 0) idx = 0;
        int[] c = COLORS[idx];
        return new int[]{c[0], c[2]};
    }

    /** 生成/取缓存内置背景。 */
    public static Bitmap preset(String id, int w, int h) {
        if (w <= 0) w = 1080;
        if (h <= 0) h = 1920;
        String key = id + "@" + w + "x" + h;
        Bitmap cached = CACHE.get(key);
        if (cached != null && !cached.isRecycled()) return cached;
        int idx = indexOf(id);
        if (idx < 0) idx = 0;
        Bitmap bmp = drawPreset(idx, w, h);
        CACHE.put(key, bmp);
        return bmp;
    }

    private static Bitmap drawPreset(int idx, int w, int h) {
        int[] c = COLORS[Math.max(0, Math.min(COLORS.length - 1, idx))];
        Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        Canvas cv = new Canvas(bmp);
        Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        p.setShader(new LinearGradient(
                0, 0, w * 0.42f, h,
                new int[]{c[0], c[1], c[0]},
                new float[]{0f, 0.55f, 1f},
                Shader.TileMode.CLAMP));
        cv.drawRect(0, 0, w, h, p);

        float min = Math.min(w, h);
        // 三团柔光，位置按比例，任何屏幕比例都好看
        drawBlob(cv, c[2], w * 0.18f, h * 0.16f, min * 0.62f, 0x66);
        drawBlob(cv, c[3], w * 0.86f, h * 0.34f, min * 0.55f, 0x55);
        drawBlob(cv, c[4], w * 0.30f, h * 0.86f, min * 0.68f, 0x4C);

        // 顶部压暗 + 底部压暗，保证文字对比度
        Paint shade = new Paint(Paint.ANTI_ALIAS_FLAG);
        shade.setShader(new LinearGradient(
                0, 0, 0, h,
                new int[]{0x66000000, 0x11000000, 0x00000000, 0x77000000},
                new float[]{0f, 0.22f, 0.62f, 1f},
                Shader.TileMode.CLAMP));
        cv.drawRect(0, 0, w, h, shade);
        return bmp;
    }

    private static void drawBlob(Canvas cv, int color, float x, float y, float r, int alpha) {
        int a = (color >>> 24) & 0xFF;
        int rgb = color & 0x00FFFFFF;
        Paint bp = new Paint(Paint.ANTI_ALIAS_FLAG);
        bp.setColor((Math.min(255, (a == 0 ? 255 : a) * alpha / 100) << 24) | rgb);
        bp.setMaskFilter(new BlurMaskFilter(r * 0.55f, BlurMaskFilter.Blur.NORMAL));
        cv.drawCircle(x, y, r, bp);
    }

    /** 自定义背景文件所在目录。 */
    public static File customDir(Context ctx) {
        File d = new File(ctx.getApplicationContext().getFilesDir(), "wallpapers");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    /** 把用户选中的图片复制进私有目录，返回文件名（失败返回 null）。 */
    public static String importCustom(Context ctx, Uri uri) {
        if (uri == null) return null;
        InputStream in = null;
        FileOutputStream out = null;
        try {
            String name = "custom_" + System.currentTimeMillis() + ".jpg";
            File dst = new File(customDir(ctx), name);
            in = ctx.getContentResolver().openInputStream(uri);
            if (in == null) return null;
            out = new FileOutputStream(dst);
            byte[] buf = new byte[64 * 1024];
            int n;
            long total = 0;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                total += n;
                if (total > 32L * 1024 * 1024) break; // 32MB 上限，防呆
            }
            out.flush();
            // 删除旧的自定义背景，避免越攒越多
            File[] olds = customDir(ctx).listFiles();
            if (olds != null) {
                for (File f : olds) {
                    if (!f.getName().equals(name)) {
                        //noinspection ResultOfMethodCallIgnored
                        f.delete();
                    }
                }
            }
            return name;
        } catch (Throwable t) {
            return null;
        } finally {
            closeQuietly(in);
            closeQuietly(out);
        }
    }

    public static void clearCustom(Context ctx) {
        File[] olds = customDir(ctx).listFiles();
        if (olds != null) {
            for (File f : olds) {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            }
        }
    }

    /** 读取自定义背景（按屏幕尺寸居中裁剪）。 */
    public static Bitmap loadCustom(Context ctx, String fileName, int w, int h) {
        if (fileName == null || fileName.isEmpty()) return null;
        File f = new File(customDir(ctx), fileName);
        if (!f.exists()) return null;
        String key = "custom:" + fileName + "@" + w + "x" + h;
        Bitmap cached = CACHE.get(key);
        if (cached != null && !cached.isRecycled()) return cached;
        try {
            BitmapFactory.Options o = new BitmapFactory.Options();
            o.inJustDecodeBounds = true;
            BitmapFactory.decodeFile(f.getAbsolutePath(), o);
            int sw = o.outWidth, sh = o.outHeight;
            if (sw <= 0 || sh <= 0) return null;
            int sample = 1;
            while (sw / (sample * 2) >= w && sh / (sample * 2) >= h) sample *= 2;
            BitmapFactory.Options o2 = new BitmapFactory.Options();
            o2.inSampleSize = sample;
            o2.inPreferredConfig = Bitmap.Config.ARGB_8888;
            Bitmap src = BitmapFactory.decodeFile(f.getAbsolutePath(), o2);
            if (src == null) return null;
            Bitmap out = centerCrop(src, w, h);
            if (out != src) src.recycle();
            CACHE.put(key, out);
            return out;
        } catch (Throwable t) {
            return null;
        }
    }

    /** 居中裁剪到目标尺寸（cover 语义）。 */
    public static Bitmap centerCrop(Bitmap src, int w, int h) {
        if (src == null) return null;
        if (w <= 0 || h <= 0) return src;
        float scale = Math.max((float) w / src.getWidth(), (float) h / src.getHeight());
        int nw = Math.round(src.getWidth() * scale);
        int nh = Math.round(src.getHeight() * scale);
        if (nw < w) nw = w;
        if (nh < h) nh = h;
        Bitmap scaled = Bitmap.createScaledBitmap(src, nw, nh, true);
        int x = Math.max(0, (nw - w) / 2);
        int y = Math.max(0, (nh - h) / 2);
        Bitmap out = Bitmap.createBitmap(scaled, x, y, Math.min(w, nw), Math.min(h, nh));
        if (out != scaled && scaled != src) scaled.recycle();
        return out;
    }

    /**
     * API < 31 的降级模糊：先把图缩到很小再放大，
     * 视觉上是柔和糊化，性能几乎无损。
     */
    public static Bitmap blurFallback(Bitmap src, int radius) {
        if (src == null) return null;
        if (radius <= 0) return src;
        try {
            int div = Math.max(2, Math.min(16, 2 + radius / 3));
            int w = Math.max(1, src.getWidth() / div);
            int h = Math.max(1, src.getHeight() / div);
            Bitmap small = Bitmap.createScaledBitmap(src, w, h, true);
            Bitmap back = Bitmap.createScaledBitmap(small, src.getWidth(), src.getHeight(), true);
            if (small != back) small.recycle();
            return back;
        } catch (Throwable t) {
            return src;
        }
    }

    /** 生成纯色 / 半透明遮罩用的颜色（浅色主题下用）。 */
    public static int scrim(boolean light) {
        return light ? Color.argb(0x99, 0xF2, 0xF4, 0xF8) : Color.argb(0x59, 0x05, 0x07, 0x0B);
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c == null) return;
        try {
            c.close();
        } catch (Throwable ignore) {
        }
    }
}
