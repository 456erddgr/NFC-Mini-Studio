package net.nfcreader.core.ui;

import android.content.Context;
import android.graphics.BitmapShader;
import android.graphics.Canvas;
import android.graphics.LinearGradient;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Shader;
import android.util.AttributeSet;
import android.view.View;
import android.widget.FrameLayout;

import net.nfcreader.core.WallpaperHolder;

/**
 * 液态玻璃卡片（v2.1 重写）。
 *
 * 上一版看起来「错乱」，是因为它采样的是**未经压暗的原壁纸模糊**，而背景层
 * 在壁纸之上还盖着一层压暗渐变 —— 于是卡片比周围背景亮一截，像贴了块半透明白纸。
 *
 * 现在的做法（顺序即绘制顺序）：
 *   1. 采样 WallpaperHolder 的模糊贴图 —— 那张图里已经把压暗渐变烤进去了
 *      （见 WallpaperHolder.bakeScrim），所以卡片里看到的就是「它背后那块背景」
 *      的模糊版，和四周自然连成一片；
 *   2. 极轻的白色提亮（约 7%），只用来表达玻璃的「厚度」，不靠它遮挡内容；
 *   3. 上亮下暗的 1px 描边 + 底部内阴影 → 玻璃边缘的厚度感；
 *   4. 顶部只留一条 1px 的镜面亮线（上一版那 30% 高的白色渐变正是发白的元凶）。
 *
 * 触感交给按钮自己做（Haptics.select），卡片不再拦截触摸，避免一次点击震两下。
 */
public class GlassCardView extends FrameLayout {

    private final RectF rect = new RectF();
    private final Paint glassPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint tintPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint shadePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint borderPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint edgePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Matrix shaderMatrix = new Matrix();
    private final int[] loc = new int[2];

    private float radiusDp = 22f;
    private float glassAlpha = 1f;
    private BitmapShader usedShader;

    public GlassCardView(Context c) {
        this(c, null);
    }

    public GlassCardView(Context c, AttributeSet attrs) {
        super(c, attrs);
        setWillNotDraw(false);
        setLayerType(View.LAYER_TYPE_HARDWARE, null);
    }

    public void setCornerRadiusDp(float dp) {
        radiusDp = Math.max(0f, dp);
        invalidate();
    }

    public void setGlassAlpha(float a) {
        glassAlpha = Math.max(0.15f, Math.min(1f, a));
        invalidate();
    }

    private float px() {
        return getResources().getDisplayMetrics().density;
    }

    @Override
    protected void onDraw(Canvas canvas) {
        float d = px();
        float r = radiusDp * d;
        rect.set(0f, 0f, getWidth(), getHeight());

        // 1) 背后内容的模糊版（已压暗），按卡片在窗口里的位置对齐采样
        BitmapShader shader = WallpaperHolder.blurShader();
        boolean hasGlass = shader != null && WallpaperHolder.blurBitmap() != null;
        if (hasGlass) {
            if (usedShader != shader) {
                usedShader = shader;
                glassPaint.setShader(shader);
            }
            getLocationInWindow(loc);
            float k = WallpaperHolder.BLUR_SCALE;
            shaderMatrix.setScale(k, k);
            shaderMatrix.postTranslate(loc[0] * k, loc[1] * k);
            shader.setLocalMatrix(shaderMatrix);
            glassPaint.setAlpha((int) (255 * glassAlpha));
            canvas.drawRoundRect(rect, r, r, glassPaint);
        } else {
            glassPaint.setShader(null);
            glassPaint.setColor(0xFF171C24);
            glassPaint.setAlpha((int) (255 * (0.72f + 0.28f * glassAlpha)));
            canvas.drawRoundRect(rect, r, r, glassPaint);
        }

        // 2) 玻璃本体：一点点白 + 上亮下暗，模拟斜上方打光
        tintPaint.setShader(new LinearGradient(0f, rect.top, 0f, rect.bottom,
                new int[]{0x1CFFFFFF, 0x12FFFFFF, 0x0AFFFFFF, 0x0AFFFFFF},
                new float[]{0f, 0.30f, 0.65f, 1f}, Shader.TileMode.CLAMP));
        tintPaint.setAlpha((int) (255 * glassAlpha));
        canvas.drawRoundRect(rect, r, r, tintPaint);

        // 3) 底部内阴影：让下沿有一点"压下去"的立体感
        shadePaint.setShader(new LinearGradient(0f, rect.bottom - rect.height() * 0.28f, 0f, rect.bottom,
                new int[]{0x00000000, 0x1C000000}, new float[]{0f, 1f}, Shader.TileMode.CLAMP));
        shadePaint.setAlpha((int) (255 * glassAlpha));
        canvas.drawRoundRect(rect, r, r, shadePaint);

        // 4) 描边：上亮下暗，制造玻璃厚度
        borderPaint.setStyle(Paint.Style.STROKE);
        borderPaint.setStrokeWidth(1f * d);
        borderPaint.setShader(new LinearGradient(0f, rect.top, 0f, rect.bottom,
                new int[]{0x59FFFFFF, 0x1FFFFFFF, 0x12000000},
                new float[]{0f, 0.5f, 1f}, Shader.TileMode.CLAMP));
        borderPaint.setAlpha((int) (255 * glassAlpha));
        canvas.drawRoundRect(rect, r, r, borderPaint);

        // 5) 顶部 1px 镜面亮线（只在上面，不像上一版盖住 30% 高度）
        edgePaint.setStyle(Paint.Style.STROKE);
        edgePaint.setStrokeWidth(1f * d);
        edgePaint.setShader(new LinearGradient(0f, 0f, 0f, Math.max(1f, rect.height() * 0.06f),
                new int[]{0x8CFFFFFF, 0x00FFFFFF}, new float[]{0f, 1f}, Shader.TileMode.CLAMP));
        edgePaint.setAlpha((int) (255 * glassAlpha));
        canvas.drawRoundRect(rect, r, r, edgePaint);
    }
}
