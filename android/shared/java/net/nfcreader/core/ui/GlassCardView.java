package net.nfcreader.core.ui;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.util.AttributeSet;
import android.widget.FrameLayout;

/**
 * iOS 风格卡片（v2.2）。
 *
 * 之前的「液态玻璃」采样壁纸模糊图，在亮壁纸、深色壁纸、自定义照片上表现不稳定，
 * 用户直接反馈「错乱、不好看」。这里改用 Apple 设置页 / 控制中心那种最稳的做法：
 * 纯色圆角面板 + 0.5dp 发丝描边，不做任何背景采样 —— 永远干净、永远一致。
 *
 * 颜色取 iOS 深色系：secondarySystemBackground #1C1C1E，
 * 描边 white 12%，圆角 18dp（贴近 iOS 卡片的 16~20dp）。
 */
public class GlassCardView extends FrameLayout {

    /** iOS secondarySystemBackground（近不透明，壁纸只透出一点点氛围）。 */
    private static final int CARD_FILL = 0x1C1C1E;
    private static final int CARD_BORDER = 0x1FFFFFFF;

    private final RectF rect = new RectF();
    private final Paint fillPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint borderPaint = new Paint(Paint.ANTI_ALIAS_FLAG);

    private float radiusDp = 18f;
    private float glassAlpha = 1f;

    public GlassCardView(Context c) {
        this(c, null);
    }

    public GlassCardView(Context c, AttributeSet attrs) {
        super(c, attrs);
        setWillNotDraw(false);
        fillPaint.setStyle(Paint.Style.FILL);
        borderPaint.setStyle(Paint.Style.STROKE);
    }

    public void setCornerRadiusDp(float dp) {
        radiusDp = Math.max(0f, dp);
        invalidate();
    }

    public void setGlassAlpha(float a) {
        glassAlpha = Math.max(0.3f, Math.min(1f, a));
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        float d = getResources().getDisplayMetrics().density;
        float r = radiusDp * d;
        rect.set(0f, 0f, getWidth(), getHeight());

        fillPaint.setColor(CARD_FILL);
        fillPaint.setAlpha((int) (255 * (0.88f + 0.12f * glassAlpha)));
        canvas.drawRoundRect(rect, r, r, fillPaint);

        borderPaint.setStrokeWidth(Math.max(1f, 0.6f * d));
        borderPaint.setColor(CARD_BORDER);
        canvas.drawRoundRect(rect, r, r, borderPaint);
    }
}
