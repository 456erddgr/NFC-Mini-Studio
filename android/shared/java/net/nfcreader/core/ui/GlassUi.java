package net.nfcreader.core.ui;

import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.view.animation.DecelerateInterpolator;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import net.nfcreader.core.Haptics;
import net.nfcreader.core.WallpaperHolder;

/**
 * 一套不依赖任何第三方库的「液态玻璃」控件工厂。
 *
 * 为什么不用 XML 布局：两个 App（读卡器 / 完整版）共几百个控件，
 * 用代码生成可以完全避免 findViewById 找不到、id 冲突、密度适配这些坑，
 * 也方便在这里统一做全屏、圆角、玻璃、震动。
 */
public final class GlassUi {

    public static final int BG = 0xFF05070B;
    public static final int TEXT = 0xFFEAF0F7;
    public static final int DIM = 0xFF93A1B5;
    public static final int FAINT = 0xFF5D6B7D;
    public static final int ACCENT = 0xFF4F9CF9;
    public static final int ACCENT2 = 0xFF22D3A6;
    public static final int WARN = 0xFFFFB020;
    public static final int ERR = 0xFFFF5C6C;
    public static final int OK = 0xFF34D399;

    public static final int STYLE_PRIMARY = 0;
    public static final int STYLE_GHOST = 1;
    public static final int STYLE_DANGER = 2;
    public static final int STYLE_SOFT = 3;

    private GlassUi() {
    }

    // ------------------------------------------------------------ 尺寸

    public static float dp(Context ctx, float v) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                ctx.getResources().getDisplayMetrics());
    }

    public static int dpi(Context ctx, float v) {
        return Math.round(dp(ctx, v));
    }

    // ------------------------------------------------------------ 全屏 / 刘海

    /**
     * 真正铺满整屏：内容延伸到状态栏与导航栏下面，并且允许画进刘海区域。
     * 旧版用 setDecorFitsSystemWindows 默认值，上下都留白，所以「不全屏」。
     */
    @SuppressWarnings("deprecation")
    public static void fullscreen(Activity a) {
        if (a == null) return;
        try {
            Window w = a.getWindow();
            w.clearFlags(WindowManager.LayoutParams.FLAG_TRANSLUCENT_STATUS);
            w.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
            w.setStatusBarColor(Color.TRANSPARENT);
            w.setNavigationBarColor(Color.TRANSPARENT);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                w.setNavigationBarDividerColor(Color.TRANSPARENT);
                WindowManager.LayoutParams lp = w.getAttributes();
                lp.layoutInDisplayCutoutMode =
                        WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
                w.setAttributes(lp);
                w.getDecorView().setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                                | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
            } else {
                w.getDecorView().setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                w.setStatusBarContrastEnforced(false);
                w.setNavigationBarContrastEnforced(false);
            }
        } catch (Throwable ignore) {
        }
    }

    /** 给根 View 加上状态栏/导航栏内边距，保证内容不被刘海和手势条挡住。 */
    @SuppressWarnings("deprecation")
    public static void applyInsets(final View root, final int extraTopDp, final int extraBottomDp) {
        if (root == null) return;
        final int et = dpi(root.getContext(), extraTopDp);
        final int eb = dpi(root.getContext(), extraBottomDp);
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets b = insets.getInsets(
                        android.view.WindowInsets.Type.systemBars()
                                | android.view.WindowInsets.Type.displayCutout());
                top = b.top;
                bottom = b.bottom;
                left = b.left;
                right = b.right;
            } else {
                top = insets.getSystemWindowInsetTop();
                bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft();
                right = insets.getSystemWindowInsetRight();
            }
            v.setPadding(left, top + et, right, bottom + eb);
            return insets;
        });
        root.requestApplyInsets();
    }

    /** 窗口 inset 回调。 */
    public interface InsetsCb {
        void on(int top, int bottom, int left, int right);
    }

    /**
     * 监听窗口 inset（状态栏/导航栏/刘海），把结果交给调用方自己决定怎么用。
     * 比直接在根 View 上 setPadding 更灵活：背景可以铺满，内容留白由各自负责。
     */
    @SuppressWarnings("deprecation")
    public static void watchInsets(View anyViewInWindow, final InsetsCb cb) {
        if (anyViewInWindow == null || cb == null) return;
        anyViewInWindow.setOnApplyWindowInsetsListener((v, insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets b = insets.getInsets(
                        android.view.WindowInsets.Type.systemBars()
                                | android.view.WindowInsets.Type.displayCutout());
                top = b.top;
                bottom = b.bottom;
                left = b.left;
                right = b.right;
            } else {
                top = insets.getSystemWindowInsetTop();
                bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft();
                right = insets.getSystemWindowInsetRight();
            }
            cb.on(top, bottom, left, right);
            return insets;
        });
        anyViewInWindow.requestApplyInsets();
    }

    // ------------------------------------------------------------ 背景

    /**
     * 铺一层壁纸 + 渐变压暗，返回承载内容的 FrameLayout。
     * 壁纸必须先设置进 WallpaperHolder，玻璃卡片才能采样到它。
     */
    public static FrameLayout background(Activity a) {
        FrameLayout root = new FrameLayout(a);
        root.setBackgroundColor(BG);

        ImageView img = new ImageView(a);
        img.setScaleType(ImageView.ScaleType.CENTER_CROP);
        if (WallpaperHolder.ready()) {
            img.setImageBitmap(WallpaperHolder.bitmap());
        }
        root.addView(img, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        View scrim = new View(a);
        // 与玻璃卡片采样用的压暗渐变保持同一份数据源（见 WallpaperHolder.SCRIM_COLORS）
        GradientDrawable g = new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
                WallpaperHolder.SCRIM_COLORS);
        scrim.setBackground(g);
        root.addView(scrim, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        return root;
    }

    /** 换背景后刷新（壁纸换了就重建背景层）。 */
    public static void refreshBackground(Activity a) {
        try {
            a.getWindow().getDecorView().post(() -> {
                try {
                    a.recreate();
                } catch (Throwable ignore) {
                }
            });
        } catch (Throwable ignore) {
        }
    }

    // ------------------------------------------------------------ 文本

    public static TextView text(Context ctx, String s, float sp, int color) {
        TextView t = new TextView(ctx);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        return t;
    }

    public static TextView h1(Context ctx, String s) {
        TextView t = text(ctx, s, 24f, TEXT);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    public static TextView h2(Context ctx, String s) {
        TextView t = text(ctx, s, 16f, TEXT);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    public static TextView label(Context ctx, String s) {
        return text(ctx, s, 12.5f, DIM);
    }

    public static TextView value(Context ctx, String s) {
        TextView t = text(ctx, s, 13.5f, TEXT);
        t.setTypeface(Typeface.MONOSPACE);
        return t;
    }

    public static TextView mono(Context ctx, String s) {
        TextView t = text(ctx, s, 11.5f, 0xFFB9C6D6);
        t.setTypeface(Typeface.MONOSPACE);
        t.setLineSpacing(dp(ctx, 2f), 1f);
        return t;
    }

    // ------------------------------------------------------------ 布局

    public static LinearLayout column(Context ctx) {
        LinearLayout l = new LinearLayout(ctx);
        l.setOrientation(LinearLayout.VERTICAL);
        return l;
    }

    public static LinearLayout row(Context ctx) {
        LinearLayout l = new LinearLayout(ctx);
        l.setOrientation(LinearLayout.HORIZONTAL);
        l.setGravity(Gravity.CENTER_VERTICAL);
        return l;
    }

    public static LinearLayout.LayoutParams lp(int w, int h) {
        return new LinearLayout.LayoutParams(w, h);
    }

    public static LinearLayout.LayoutParams lpMr(Context ctx, int w, float topDp) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                w, ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = dpi(ctx, topDp);
        return p;
    }

    public static ScrollView scroll(Context ctx, View content) {
        ScrollView s = new ScrollView(ctx);
        s.setFillViewport(true);
        s.setOverScrollMode(View.OVER_SCROLL_NEVER);
        s.setVerticalScrollBarEnabled(false);
        s.addView(content, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return s;
    }

    public static GlassCardView card(Context ctx) {
        GlassCardView c = new GlassCardView(ctx);
        int p = dpi(ctx, 16f);
        c.setPadding(p, p, p, p);
        return c;
    }

    public static GlassCardView card(Context ctx, float padDp) {
        GlassCardView c = new GlassCardView(ctx);
        int p = dpi(ctx, padDp);
        c.setPadding(p, p, p, p);
        return c;
    }

    public static View divider(Context ctx) {
        View v = new View(ctx);
        v.setBackgroundColor(0x1AFFFFFF);
        v.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Math.max(1, dpi(ctx, 0.7f))));
        return v;
    }

    public static View gap(Context ctx, float hDp) {
        View v = new View(ctx);
        v.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dpi(ctx, hDp)));
        return v;
    }

    // ------------------------------------------------------------ 按钮

    public static TextView button(Context ctx, String labelText, final int style, final Runnable onClick) {
        TextView b = text(ctx, labelText, 14.5f, TEXT);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        int ph = dpi(ctx, 16f);
        int pv = dpi(ctx, 12f);
        b.setPadding(ph, pv, ph, pv);
        applyButtonBg(ctx, b, style);
        b.setClickable(true);
        b.setFocusable(true);
        b.setOnClickListener(v -> {
            Haptics.select(ctx);
            if (onClick != null) onClick.run();
        });
        return b;
    }

    public static void applyButtonBg(Context ctx, TextView b, int style) {
        int r = dpi(ctx, 15f);
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(r);
        switch (style) {
            case STYLE_PRIMARY:
                g.setColors(new int[]{ACCENT, ACCENT2});
                g.setOrientation(GradientDrawable.Orientation.LEFT_RIGHT);
                b.setTextColor(0xFF04121C);
                break;
            case STYLE_DANGER:
                g.setColors(new int[]{ERR, 0xFFFF8A5C});
                g.setOrientation(GradientDrawable.Orientation.LEFT_RIGHT);
                b.setTextColor(0xFF20060A);
                break;
            case STYLE_SOFT:
                g.setColor(0x2E6EA8FF);
                g.setStroke(Math.max(1, dpi(ctx, 0.8f)), 0x554F9CF9);
                b.setTextColor(0xFFBFD8FF);
                break;
            case STYLE_GHOST:
            default:
                g.setColor(0x14FFFFFF);
                g.setStroke(Math.max(1, dpi(ctx, 0.8f)), 0x30FFFFFF);
                b.setTextColor(TEXT);
                break;
        }
        b.setBackground(g);
    }

    /** 小胶囊标签。 */
    public static TextView pill(Context ctx, String s, int fg, int bg) {
        TextView t = text(ctx, s, 11f, fg);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setGravity(Gravity.CENTER);
        int ph = dpi(ctx, 9f);
        int pv = dpi(ctx, 4f);
        t.setPadding(ph, pv, ph, pv);
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dpi(ctx, 20f));
        g.setColor(bg);
        t.setBackground(g);
        return t;
    }

    public static void setPill(TextView t, String s, int fg, int bg) {
        t.setText(s);
        t.setTextColor(fg);
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dpi(t.getContext(), 20f));
        g.setColor(bg);
        t.setBackground(g);
    }

    // ------------------------------------------------------------ 输入

    public static EditText field(Context ctx, String hint) {
        EditText e = new EditText(ctx);
        e.setHint(hint);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f);
        e.setTextColor(TEXT);
        e.setHintTextColor(FAINT);
        e.setBackground(inputBg(ctx));
        int ph = dpi(ctx, 13f);
        int pv = dpi(ctx, 10f);
        e.setPadding(ph, pv, ph, pv);
        e.setSingleLine(false);
        return e;
    }

    public static GradientDrawable inputBg(Context ctx) {
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dpi(ctx, 13f));
        g.setColor(0x18FFFFFF);
        g.setStroke(Math.max(1, dpi(ctx, 0.8f)), 0x28FFFFFF);
        return g;
    }

    // ------------------------------------------------------------ 弹层

    /** 玻璃 Toast（不用系统 Toast，避免那套灰底方框）。 */
    public static void toast(final Activity a, final String msg) {
        if (a == null || a.isFinishing() || msg == null) return;
        try {
            final ViewGroup decor = (ViewGroup) a.getWindow().getDecorView();
            final TextView t = text(a, msg, 13.5f, TEXT);
            t.setGravity(Gravity.CENTER);
            int ph = dpi(a, 18f);
            int pv = dpi(a, 11f);
            t.setPadding(ph, pv, ph, pv);
            GradientDrawable g = new GradientDrawable();
            g.setCornerRadius(dpi(a, 16f));
            g.setColor(0xE6141A22);
            g.setStroke(Math.max(1, dpi(a, 0.8f)), 0x3DFFFFFF);
            t.setBackground(g);
            t.setAlpha(0f);
            t.setTranslationY(dp(a, 18f));
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
            lp.bottomMargin = dpi(a, 108f);
            decor.addView(t, lp);
            t.animate().alpha(1f).translationY(0f).setDuration(170)
                    .setInterpolator(new DecelerateInterpolator()).start();
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                t.animate().alpha(0f).translationY(dp(a, 12f)).setDuration(220)
                        .withEndAction(() -> {
                            try {
                                decor.removeView(t);
                            } catch (Throwable ignore) {
                            }
                        }).start();
            }, 1900);
        } catch (Throwable ignore) {
        }
    }

    /** 玻璃确认框。 */
    public static void confirm(final Activity a, String title, String msg,
                               String okText, final Runnable onOk) {
        if (a == null || a.isFinishing()) return;
        try {
            final Dialog d = new Dialog(a);
            d.requestWindowFeature(Window.FEATURE_NO_TITLE);
            FrameLayout root = new FrameLayout(a);
            root.setBackgroundColor(0x99000000);

            GlassCardView card = card(a, 20f);
            LinearLayout col = column(a);
            TextView t1 = h2(a, title == null ? "" : title);
            col.addView(t1);
            if (msg != null && !msg.isEmpty()) {
                TextView t2 = text(a, msg, 13.5f, DIM);
                t2.setLineSpacing(dp(a, 3f), 1f);
                col.addView(t2, lpMr(a, ViewGroup.LayoutParams.WRAP_CONTENT, 10f));
            }
            LinearLayout btns = row(a);
            btns.setGravity(Gravity.END);
            TextView cancel = button(a, "取消", STYLE_GHOST, () -> d.dismiss());
            TextView ok = button(a, okText == null ? "确定" : okText, STYLE_PRIMARY, () -> {
                d.dismiss();
                if (onOk != null) onOk.run();
            });
            LinearLayout.LayoutParams p1 = lp(ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            LinearLayout.LayoutParams p2 = lp(ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            p1.rightMargin = dpi(a, 10f);
            p1.weight = 1f;
            p2.weight = 1f;
            cancel.setLayoutParams(p1);
            ok.setLayoutParams(p2);
            btns.addView(cancel);
            btns.addView(ok);
            col.addView(btns, lpMr(a, ViewGroup.LayoutParams.MATCH_PARENT, 16f));
            card.addView(col);

            FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.CENTER);
            int m = dpi(a, 26f);
            clp.leftMargin = m;
            clp.rightMargin = m;
            root.addView(card, clp);
            d.setContentView(root);
            d.setCanceledOnTouchOutside(true);
            Window w = d.getWindow();
            if (w != null) {
                w.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
                w.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
            }
            d.show();
        } catch (Throwable ignore) {
        }
    }

    /** 给控件挂一个「点了就震动」的监听。 */
    public static void onClick(View v, final Runnable r) {
        v.setOnClickListener(x -> {
            Haptics.tap(v.getContext());
            if (r != null) r.run();
        });
    }
}
