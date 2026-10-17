package net.nfcreader.minireader;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import net.nfcreader.core.AppSettings;
import net.nfcreader.core.CardOps;
import net.nfcreader.core.CardStore;
import net.nfcreader.core.DeviceInfo;
import net.nfcreader.core.Haptics;
import net.nfcreader.core.NfcEngine;
import net.nfcreader.core.Protocol;
import net.nfcreader.core.ReaderCore;
import net.nfcreader.core.Task;
import net.nfcreader.core.WallpaperFactory;
import net.nfcreader.core.WallpaperHolder;
import net.nfcreader.core.ui.GlassCardView;
import net.nfcreader.core.ui.GlassUi;

import java.util.ArrayList;
import java.util.List;

/**
 * NFC 手机读卡器 —— 把手机变成电脑的外置 NFC 读卡器。
 *
 * v2.0.0 重做要点：
 *   1. 真全屏：内容画到状态栏/导航栏/刘海下面（旧版上下留白）。
 *   2. 真液态玻璃：GlassCardView 采样预模糊的屏幕空间贴图，
 *      卡片里透出的就是它正下方那块壁纸的模糊版本，并带高光描边。
 *   3. 震动全覆盖：每个按钮、每次读卡成功/失败都有不同强度反馈。
 *   4. 本机读卡：识别 / 读整卡 / 破解 / 写块，手机不接电脑也能用。
 *   5. 有线连接：屏幕常亮 + 不依赖 Wi-Fi 的 adb reverse 通道。
 *   6. 诊断：显示收到多少帧、最近一条命令字，用来区分「网络没通」和「有卡没读到」。
 */
public final class MainActivity extends AppCompatActivity
        implements NfcAdapter.ReaderCallback, ReaderCore.Listener {

    private static final int REQ_NOTIFY = 1001;

    private ReaderCore core;
    private AppSettings settings;
    private NfcAdapter nfc;
    private boolean readerMode = false;
    private boolean autoStarted = false;

    private final Handler ui = new Handler(Looper.getMainLooper());

    private TextView pillNfc, pillSrv, pillLink, pillCard;
    private TextView tvAddr, tvDiag, tvCardInfo;
    private TextView outView, logView;
    private TextView btnStart, btnStop;
    private EditText edBlock, edData, edKey;
    private TextView btnKeyType;
    private int writeKeyType = 0;
    private CardOps.Dump lastDump;
    private LinearLayout content;
    private View topSpace, bottomSpace;

    // ------------------------------------------------------------ 生命周期

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        GlassUi.fullscreen(this);
        core = ReaderCore.get();
        core.init(this);
        core.addListener(this);
        settings = core.settings();
        nfc = NfcAdapter.getDefaultAdapter(this);

        loadWallpaper();
        buildUi();
        askNotifyPermission();
        refreshAll();
    }

    private void loadWallpaper() {
        try {
            int w = getResources().getDisplayMetrics().widthPixels;
            int h = getResources().getDisplayMetrics().heightPixels;
            int bw = Math.max(360, Math.min(1080, w));
            int bh = Math.max(640, Math.min(1920, h));
            if (settings.customWallpaper != null && !settings.customWallpaper.isEmpty()) {
                android.graphics.Bitmap cb = WallpaperFactory.loadCustom(this, settings.customWallpaper, bw, bh);
                if (cb != null) {
                    WallpaperHolder.set(cb, "custom", true);
                } else {
                    WallpaperHolder.set(WallpaperFactory.preset(settings.wallpaperId, bw, bh),
                            settings.wallpaperId, false);
                }
            } else {
                WallpaperHolder.set(WallpaperFactory.preset(settings.wallpaperId, bw, bh),
                        settings.wallpaperId, false);
            }
            WallpaperHolder.prepare(w, h);
        } catch (Throwable ignore) {
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        enableReader();
        if (settings.keepScreenOn) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        if (settings.autoStart) {
            // 每次前台化都重新拉一次服务：MIUI / 省电策略经常把前台服务杀掉，
            // 旧版只在进程第一次启动时拉一次，用户切回来之后其实早就没在服务了。
            autoStarted = true;
            ReaderService.start(this);
        }
        refreshAll();
    }

    @Override
    protected void onPause() {
        super.onPause();
        disableReader();
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    @Override
    protected void onDestroy() {
        try {
            core.removeListener(this);
        } catch (Throwable ignore) {
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------ NFC reader mode

    private void enableReader() {
        if (nfc == null || !nfc.isEnabled() || readerMode) return;
        try {
            Bundle opts = new Bundle();
            opts.putInt(NfcAdapter.EXTRA_READER_PRESENCE_CHECK_DELAY, 250);
            int flags = NfcAdapter.FLAG_READER_NFC_A
                    | NfcAdapter.FLAG_READER_NFC_B
                    | NfcAdapter.FLAG_READER_NFC_F
                    | NfcAdapter.FLAG_READER_NFC_V
                    | NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS;
            nfc.enableReaderMode(this, this, flags, opts);
            readerMode = true;
            core.onLog("NFC 读卡模式已开启");
        } catch (Throwable t) {
            core.onLog("开启 NFC 读卡模式失败：" + t.getMessage());
        }
    }

    private void disableReader() {
        if (nfc == null || !readerMode) return;
        try {
            nfc.disableReaderMode(this);
        } catch (Throwable ignore) {
        }
        readerMode = false;
    }

    @Override
    public void onTagDiscovered(Tag tag) {
        if (tag == null) return;
        core.onTag(tag);
        Haptics.cardDetected(this);
        ui.post(this::refreshCardLine);
    }

    // ------------------------------------------------------------ UI 搭建

    private void buildUi() {
        FrameLayout root = GlassUi.background(this);

        ScrollView sv = new ScrollView(this);
        sv.setFillViewport(true);
        sv.setVerticalScrollBarEnabled(false);
        sv.setOverScrollMode(View.OVER_SCROLL_NEVER);

        content = GlassUi.column(this);
        int p = GlassUi.dpi(this, 14f);
        content.setPadding(p, 0, p, 0);
        sv.addView(content, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(sv, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        topSpace = new View(this);
        bottomSpace = new View(this);
        content.addView(topSpace, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, GlassUi.dpi(this, 12f)));
        content.addView(bottomSpace, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, GlassUi.dpi(this, 18f)));

        GlassUi.watchInsets(root, (top, bottom, left, right) -> {
            topSpace.getLayoutParams().height = top + GlassUi.dpi(this, 6f);
            bottomSpace.getLayoutParams().height = bottom + GlassUi.dpi(this, 16f);
            topSpace.requestLayout();
            bottomSpace.requestLayout();
        });

        buildHeader();
        buildStatusCard();
        buildServiceCard();
        buildLocalCard();
        buildWriteCard();
        buildLogCard();
    }

    private void add(View v) {
        content.addView(v, content.indexOfChild(bottomSpace));
    }

    private void addGap(float dp) {
        add(GlassUi.gap(this, dp));
    }

    private void buildHeader() {
        LinearLayout row = GlassUi.row(this);
        LinearLayout col = GlassUi.column(this);
        TextView t = GlassUi.h1(this, "NFC 手机读卡器");
        col.addView(t);
        TextView v = GlassUi.label(this, "v" + BuildConfig.VERSION_NAME + " · 把手机变成电脑的读卡器");
        col.addView(v, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));
        LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        row.addView(col, clp);
        TextView gear = GlassUi.button(this, "设置", GlassUi.STYLE_GHOST, this::showSettings);
        row.addView(gear);
        add(row);
        addGap(12f);
    }

    private void buildStatusCard() {
        GlassCardView card = GlassUi.card(this);
        LinearLayout col = GlassUi.column(this);

        LinearLayout pills = GlassUi.row(this);
        pillNfc = GlassUi.pill(this, "NFC", GlassUi.DIM, 0x1AFFFFFF);
        pillSrv = GlassUi.pill(this, "服务", GlassUi.DIM, 0x1AFFFFFF);
        pillLink = GlassUi.pill(this, "连接", GlassUi.DIM, 0x1AFFFFFF);
        pillCard = GlassUi.pill(this, "卡片", GlassUi.DIM, 0x1AFFFFFF);
        LinearLayout.LayoutParams plp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        plp.rightMargin = GlassUi.dpi(this, 7f);
        pills.addView(pillNfc, plp);
        LinearLayout.LayoutParams plp2 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        plp2.rightMargin = GlassUi.dpi(this, 7f);
        pills.addView(pillSrv, plp2);
        LinearLayout.LayoutParams plp3 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        plp3.rightMargin = GlassUi.dpi(this, 7f);
        pills.addView(pillLink, plp3);
        pills.addView(pillCard);
        col.addView(pills);

        tvAddr = GlassUi.value(this, "地址 —");
        tvAddr.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f);
        col.addView(tvAddr, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 12f));

        tvCardInfo = GlassUi.label(this, "把卡贴到手机背面 NFC 区域");
        tvCardInfo.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        col.addView(tvCardInfo, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 6f));

        tvDiag = GlassUi.mono(this, "诊断：等待连接");
        col.addView(tvDiag, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 8f));

        card.addView(col);
        add(card);
        addGap(12f);
    }

    private void buildServiceCard() {
        GlassCardView card = GlassUi.card(this);
        LinearLayout col = GlassUi.column(this);
        col.addView(GlassUi.h2(this, "电脑连接"));

        LinearLayout btns = GlassUi.row(this);
        btnStart = GlassUi.button(this, "启动服务", GlassUi.STYLE_PRIMARY, () -> {
            ReaderService.start(this);
            ui.postDelayed(this::refreshAll, 400);
        });
        btnStop = GlassUi.button(this, "停止", GlassUi.STYLE_GHOST, () -> {
            ReaderService.stop(this);
            ui.postDelayed(this::refreshAll, 400);
        });
        LinearLayout.LayoutParams b1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        b1.rightMargin = GlassUi.dpi(this, 8f);
        LinearLayout.LayoutParams b2 = new LinearLayout.LayoutParams(
                GlassUi.dpi(this, 88f), ViewGroup.LayoutParams.WRAP_CONTENT);
        btns.addView(btnStart, b1);
        btns.addView(btnStop, b2);
        col.addView(btns, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        LinearLayout copyRow = GlassUi.row(this);
        TextView copyIp = GlassUi.button(this, "复制地址", GlassUi.STYLE_SOFT,
                () -> copy("nfc-reader-addr", addressText()));
        TextView copyAdb = GlassUi.button(this, "复制有线命令", GlassUi.STYLE_SOFT,
                () -> copy("nfc-reader-adb", "adb reverse tcp:" + settings.tcpPort + " tcp:" + settings.tcpPort));
        LinearLayout.LayoutParams c1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        c1.rightMargin = GlassUi.dpi(this, 8f);
        copyRow.addView(copyIp, c1);
        copyRow.addView(copyAdb, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        col.addView(copyRow, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        TextView tip = GlassUi.label(this, "无线：手机和电脑连同一个 Wi-Fi，电脑上选「手机读卡器」搜索。\n"
                + "有线更稳：手机插 USB → 开启「USB 调试」→ 电脑执行\n"
                + "  adb reverse tcp:" + settings.tcpPort + " tcp:" + settings.tcpPort + "\n"
                + "然后电脑连 127.0.0.1:" + settings.tcpPort + " 即可，完全不依赖 Wi-Fi。");
        tip.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        col.addView(tip, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 12f));

        card.addView(col);
        add(card);
        addGap(12f);
    }

    private void buildLocalCard() {
        GlassCardView card = GlassUi.card(this);
        LinearLayout col = GlassUi.column(this);
        col.addView(GlassUi.h2(this, "本机读卡（不用电脑）"));
        col.addView(GlassUi.label(this, "手机自己也能识别、读整卡、破解密钥"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));

        LinearLayout r1 = GlassUi.row(this);
        TextView bId = GlassUi.button(this, "识别", GlassUi.STYLE_SOFT, this::doIdentify);
        TextView bAll = GlassUi.button(this, "读整卡", GlassUi.STYLE_SOFT, this::doReadAll);
        TextView bCrack = GlassUi.button(this, "破解", GlassUi.STYLE_SOFT, this::doCrack);
        LinearLayout.LayoutParams q1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        q1.rightMargin = GlassUi.dpi(this, 8f);
        LinearLayout.LayoutParams q2 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        q2.rightMargin = GlassUi.dpi(this, 8f);
        r1.addView(bId, q1);
        r1.addView(bAll, q2);
        r1.addView(bCrack, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        col.addView(r1, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        LinearLayout r2 = GlassUi.row(this);
        TextView bSave = GlassUi.button(this, "存入卡夹", GlassUi.STYLE_GHOST,
                () -> saveDump());
        TextView bClear = GlassUi.button(this, "清空输出", GlassUi.STYLE_GHOST, () -> {
            outView.setText("");
            Haptics.tap(this);
        });
        LinearLayout.LayoutParams s1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        s1.rightMargin = GlassUi.dpi(this, 8f);
        r2.addView(bSave, s1);
        r2.addView(bClear, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        col.addView(r2, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        outView = GlassUi.mono(this, "");
        outView.setTextColor(0xFFC9D6E6);
        outView.setPadding(GlassUi.dpi(this, 10f), GlassUi.dpi(this, 10f),
                GlassUi.dpi(this, 10f), GlassUi.dpi(this, 10f));
        android.graphics.drawable.GradientDrawable g = GlassUi.inputBg(this);
        g.setColor(0x33000000);
        outView.setBackground(g);
        outView.setMinLines(6);
        col.addView(outView, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));

        card.addView(col);
        add(card);
        addGap(12f);
    }

    private void buildWriteCard() {
        GlassCardView card = GlassUi.card(this);
        LinearLayout col = GlassUi.column(this);
        col.addView(GlassUi.h2(this, "手动写块"));
        col.addView(GlassUi.label(this, "十六进制，数据 32 位、密钥 12 位"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));

        LinearLayout r = GlassUi.row(this);
        edBlock = GlassUi.field(this, "块号");
        edBlock.setInputType(InputType.TYPE_CLASS_NUMBER);
        edBlock.setText("4");
        btnKeyType = GlassUi.button(this, "KEY A", GlassUi.STYLE_SOFT, () -> {
            writeKeyType = writeKeyType == 0 ? 1 : 0;
            btnKeyType.setText(writeKeyType == 0 ? "KEY A" : "KEY B");
            Haptics.tap(this);
        });
        LinearLayout.LayoutParams w1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        w1.rightMargin = GlassUi.dpi(this, 8f);
        r.addView(edBlock, w1);
        r.addView(btnKeyType, new LinearLayout.LayoutParams(
                GlassUi.dpi(this, 92f), ViewGroup.LayoutParams.WRAP_CONTENT));
        col.addView(r, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        edData = GlassUi.field(this, "数据 32 位十六进制");
        edData.setText("00000000000000000000000000000000");
        edData.setInputType(InputType.TYPE_CLASS_TEXT);
        col.addView(edData, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        edKey = GlassUi.field(this, "密钥 12 位十六进制");
        edKey.setText("FFFFFFFFFFFF");
        col.addView(edKey, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        TextView go = GlassUi.button(this, "写入这一块", GlassUi.STYLE_PRIMARY, this::doWriteBlock);
        go.setLayoutParams(GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
        col.addView(go);
        TextView warn = GlassUi.label(this, "注意：块 0 是 UID/厂商块，写坏可能变砖；尾块的密钥位写错会锁卡。");
        warn.setTextColor(0xFFFFB020);
        warn.setLineSpacing(GlassUi.dp(this, 2f), 1f);
        col.addView(warn, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 8f));

        card.addView(col);
        add(card);
        addGap(12f);
    }

    private void buildLogCard() {
        GlassCardView card = GlassUi.card(this);
        LinearLayout col = GlassUi.column(this);
        LinearLayout head = GlassUi.row(this);
        TextView t = GlassUi.h2(this, "运行日志");
        head.addView(t, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        head.addView(GlassUi.button(this, "清空", GlassUi.STYLE_GHOST, () -> {
            core.clearLog();
            logView.setText("");
            Haptics.tap(this);
        }));
        col.addView(head);

        logView = GlassUi.mono(this, "");
        logView.setPadding(GlassUi.dpi(this, 8f), GlassUi.dpi(this, 8f),
                GlassUi.dpi(this, 8f), GlassUi.dpi(this, 8f));
        android.graphics.drawable.GradientDrawable g = GlassUi.inputBg(this);
        g.setColor(0x33000000);
        logView.setBackground(g);
        logView.setMinLines(8);
        col.addView(logView, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));

        card.addView(col);
        add(card);
    }

    // ------------------------------------------------------------ 状态刷新

    private void refreshAll() {
        refreshCardLine();
        refreshService();
    }

    private void refreshService() {
        boolean nfcOn;
        if (nfc == null) {
            nfcOn = false;
            GlassUi.setPill(pillNfc, "无 NFC", GlassUi.ERR, 0x33FF5C6C);
        } else if (!nfc.isEnabled()) {
            nfcOn = false;
            GlassUi.setPill(pillNfc, "NFC 关", GlassUi.WARN, 0x33FFB020);
        } else {
            nfcOn = true;
            GlassUi.setPill(pillNfc, "NFC 开", GlassUi.OK, 0x3334D399);
        }

        boolean running = core.isRunning();
        GlassUi.setPill(pillSrv, running ? "服务开" : "服务关",
                running ? GlassUi.OK : GlassUi.DIM, running ? 0x3334D399 : 0x1AFFFFFF);

        int clients = core.clientCount();
        GlassUi.setPill(pillLink, clients > 0 ? ("已连 " + clients) : "未连",
                clients > 0 ? GlassUi.ACCENT : GlassUi.DIM,
                clients > 0 ? 0x334F9CF9 : 0x1AFFFFFF);

        tvAddr.setText("地址 " + addressText());
        btnStart.setAlpha(running ? 0.45f : 1f);
        btnStop.setAlpha(running ? 1f : 0.45f);

        String diag = "诊断：收到帧 " + core.server().frameCount()
                + " · 最近命令 " + (core.server().lastCmd() < 0 ? "—"
                : String.format("0x%02X", core.server().lastCmd()))
                + " · 卡片 " + (core.hasCard() ? "在位" : "不在位");
        if (nfcOn && running && clients > 0 && core.server().frameCount() == 0) {
            diag += "\n电脑连上了但一条命令都没收到 —— 检查电脑端是否选对了「手机读卡器」通道。";
        } else if (clients > 0 && core.server().frameCount() > 0 && !core.hasCard()) {
            diag += "\n命令能收到，但手机没读到卡 —— 把卡贴到手机背面 NFC 天线附近（多数在摄像头周围）。";
        }
        tvDiag.setText(diag);
    }

    private void refreshCardLine() {
        NfcEngine.CardInfo ci = null;
        try {
            ci = core.engine() == null ? null : core.engine().readCardInfo();
        } catch (Throwable ignore) {
        }
        if (ci == null || !ci.present) {
            GlassUi.setPill(pillCard, "无卡", GlassUi.DIM, 0x1AFFFFFF);
            tvCardInfo.setText("把卡贴到手机背面 NFC 区域");
            return;
        }
        GlassUi.setPill(pillCard, ci.uid == null || ci.uid.isEmpty() ? "有卡" : ci.uid,
                GlassUi.ACCENT2, 0x3322D3A6);
        StringBuilder sb = new StringBuilder();
        sb.append(ci.className == null || ci.className.isEmpty() ? "未知卡" : ci.className);
        if (ci.mifareType != null && !ci.mifareType.isEmpty()) sb.append(" · ").append(ci.mifareType);
        sb.append("\nUID ").append(ci.uid);
        if (ci.atqa != null && !ci.atqa.isEmpty()) sb.append(" · ATQA ").append(ci.atqa);
        if (ci.sak >= 0) sb.append(" · SAK ").append(String.format("%02X", ci.sak));
        if (ci.blockCount > 0) {
            sb.append("\n").append(ci.sectorCount).append(" 个扇区 · ")
                    .append(ci.blockCount).append(" 块 · ").append(ci.sizeBytes).append(" 字节");
        }
        if (ci.note != null && !ci.note.isEmpty()) sb.append("\n").append(ci.note);
        tvCardInfo.setText(sb.toString());
        refreshService();
    }

    private String addressText() {
        if (!core.isRunning()) return "服务未启动";
        String ip = DeviceInfo.localIp();
        int port = core.server().getPort();
        String wifi = (ip == null ? "无局域网地址" : ip + ":" + port);
        return "无线  " + wifi + "\n有线  127.0.0.1:" + port + "（USB 调试）";
    }

    // ------------------------------------------------------------ 本机读卡

    private void doIdentify() {
        Haptics.select(this);
        out("识别中…");
        Task.bg(() -> core.engine().readCardInfo(), (ci, err) -> {
            if (err != null || ci == null) {
                out("识别失败：" + (err == null ? "未知错误" : err.getMessage()));
                Haptics.error(this);
                return;
            }
            if (!ci.present) {
                out(ci.note);
                Haptics.error(this);
                return;
            }
            StringBuilder sb = new StringBuilder();
            sb.append("卡类型   ").append(ci.className).append(" / ").append(ci.mifareType).append("\n");
            sb.append("UID      ").append(ci.uid).append("\n");
            sb.append("ATQA     ").append(ci.atqa).append("\n");
            sb.append("SAK      ").append(ci.sak < 0 ? "—" : String.format("%02X", ci.sak)).append("\n");
            sb.append("容量     ").append(ci.sectorCount).append(" 扇区 / ")
                    .append(ci.blockCount).append(" 块 / ").append(ci.sizeBytes).append(" 字节\n");
            sb.append("技术     ").append(ci.techs).append("\n");
            sb.append("说明     ").append(ci.label);
            out(sb.toString());
            Haptics.success(this);
            refreshCardLine();
        });
    }

    private void doReadAll() {
        if (core.engine() == null) return;
        Haptics.select(this);
        out("开始读整卡（会逐扇区试密钥，可能要十几秒）…");
        Task.bg(() -> CardOps.readAll(core.engine(), null, (done, total, line) -> ui.post(() -> {
            outView.append(line + "\n");
            scrollOut();
            ReaderService.progress(this, "正在读整卡", done, total);
        })), (d, err) -> {
            ReaderService.clearProgress(this);
            if (err != null || d == null) {
                out("读卡异常：" + (err == null ? "未知" : err.getMessage()));
                Haptics.error(this);
                return;
            }
            lastDump = d;
            if (d.blockCount <= 0) {
                out(d.note);
                Haptics.error(this);
                return;
            }
            StringBuilder sb = new StringBuilder();
            sb.append("UID ").append(d.uid).append(" · ").append(d.kind).append("\n");
            sb.append("成功 ").append(d.okCount).append(" 块 / 失败 ").append(d.failCount).append(" 块\n");
            for (int s = 0; s < d.sectorCount; s++) {
                String k = d.keyFor(s);
                sb.append("扇区 ").append(s).append(" 密钥 ")
                        .append(k == null ? "未知（该扇区读不到）" : k).append("\n");
            }
            out(sb.toString());
            Haptics.success(this);
            if (d.failCount > 0) {
                out("有 " + d.failCount + " 个块没打开，接着自动破解（先查官方云端密钥库，再试内置字典）…");
                doCrack();
            }
        });
    }

    private void doCrack() {
        if (core.engine() == null) return;
        Haptics.select(this);
        out("开始破解：先查官方云端密钥库（按 UID），拿到候选密钥后逐把真机认证；查不到再用内置字典。");
        Task.bg(() -> CardOps.crack(core.engine(), null, (done, total, line) -> ui.post(() -> {
            outView.append(line + "\n");
            scrollOut();
            ReaderService.progress(this, "正在破解", done, total);
        })), (d, err) -> {
            ReaderService.clearProgress(this);
            if (err != null || d == null) {
                out("破解异常：" + (err == null ? "未知" : err.getMessage()));
                Haptics.error(this);
                return;
            }
            lastDump = d;
            if (d.sectorCount <= 0) {
                out(d.note);
                Haptics.error(this);
                return;
            }
            StringBuilder sb = new StringBuilder();
            sb.append("命中 ").append(d.okCount).append(" / ").append(d.sectorCount).append(" 个扇区\n");
            for (int s = 0; s < d.sectorCount; s++) {
                String k = d.keyFor(s);
                sb.append("扇区 ").append(s).append("：").append(k == null ? "未破解" : k).append("\n");
            }
            out(sb.toString());
            if (d.okCount > 0) Haptics.success(this);
            else Haptics.error(this);
        });
    }

    private void doWriteBlock() {
        if (core.engine() == null) return;
        int block;
        try {
            block = Integer.parseInt(edBlock.getText().toString().trim());
        } catch (Throwable t) {
            out("块号必须是数字");
            Haptics.error(this);
            return;
        }
        final String data = edData.getText().toString().trim();
        final String key = edKey.getText().toString().trim();
        final int kt = writeKeyType;
        if (data.replaceAll("[^0-9a-fA-F]", "").length() != 32) {
            out("数据必须正好 32 位十六进制（16 字节）");
            Haptics.error(this);
            return;
        }
        if (block < 0) {
            out("块号不能为负");
            Haptics.error(this);
            return;
        }
        Haptics.select(this);
        out("写入块 " + block + " …");
        Task.bg(() -> CardOps.writeBlockHex(core.engine(), block, data, key, kt), (ok, err) -> {
            if (err != null) {
                out("写入异常：" + err.getMessage());
                Haptics.error(this);
                return;
            }
            out(Boolean.TRUE.equals(ok) ? ("块 " + block + " 写入成功") : ("块 " + block + " 写入失败（密钥不对或块被保护）"));
            if (Boolean.TRUE.equals(ok)) Haptics.success(this);
            else Haptics.error(this);
        });
    }

    private void saveDump() {
        if (lastDump == null || lastDump.uid == null || lastDump.uid.isEmpty()) {
            GlassUi.toast(this, "先读一次整卡再保存");
            return;
        }
        CardStore.Card c = new CardStore.Card();
        c.uid = lastDump.uid;
        c.atqa = lastDump.atqa;
        c.sak = lastDump.sak;
        c.blockCount = lastDump.blockCount;
        c.kind = lastDump.kind;
        c.key = lastDump.keyFor(0);
        List<String> blocks = new ArrayList<>();
        for (int i = 0; i < lastDump.blocks.length; i++) {
            blocks.add(lastDump.blockOk[i] ? lastDump.hexBlock(i) : "");
        }
        c.blocks = blocks.toArray(new String[0]);
        c.name = "手机读卡 " + lastDump.uid;
        String id = CardStore.save(this, c);
        if (id != null) {
            GlassUi.toast(this, "已存入卡夹（" + lastDump.blockCount + " 块）");
            Haptics.success(this);
        } else {
            GlassUi.toast(this, "保存失败");
            Haptics.error(this);
        }
    }

    private void out(String s) {
        outView.setText(s);
    }

    private void scrollOut() {
        // 输出区不需要单独滚动，外层 ScrollView 跟着走
    }

    // ------------------------------------------------------------ 设置

    private void showSettings() {
        final android.app.Dialog d = new android.app.Dialog(this);
        d.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0x99000000);
        GlassCardView card = GlassUi.card(this, 18f);
        LinearLayout col = GlassUi.column(this);
        col.addView(GlassUi.h2(this, "设置"));

        final TextView vib = GlassUi.button(this, "震动：" + vibLabel(), GlassUi.STYLE_GHOST, null);
        vib.setOnClickListener(v -> {
            int lv = (Haptics.getLevel() + 1) % 4;
            Haptics.setLevel(lv);
            vib.setText("震动：" + vibLabel());
            Haptics.select(this);
            settings.vibrate = lv != Haptics.OFF;
            settings.save();
        });
        col.addView(vib, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        final TextView keep = GlassUi.button(this,
                "保持屏幕常亮：" + (settings.keepScreenOn ? "开" : "关"), GlassUi.STYLE_GHOST, null);
        keep.setOnClickListener(v -> {
            settings.keepScreenOn = !settings.keepScreenOn;
            keep.setText("保持屏幕常亮：" + (settings.keepScreenOn ? "开" : "关"));
            settings.save();
            Haptics.tap(this);
        });
        col.addView(keep, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        TextView alive = GlassUi.button(this, "后台保活：允许不被省电杀掉", GlassUi.STYLE_SOFT, null);
        alive.setOnClickListener(v -> {
            Haptics.tap(this);
            requestBatteryWhitelist();
        });
        col.addView(alive, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        final TextView port = GlassUi.button(this, "端口：" + settings.tcpPort, GlassUi.STYLE_GHOST, null);
        port.setOnClickListener(v -> {
            int p = settings.tcpPort + 1;
            if (p > 9000) p = 8765;
            settings.tcpPort = p;
            port.setText("端口：" + settings.tcpPort);
            settings.save();
            Haptics.tap(this);
            GlassUi.toast(this, "改端口后要重启服务");
        });
        col.addView(port, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        final TextView wp = GlassUi.button(this, "背景：" + WallpaperFactory.nameOf(settings.wallpaperId),
                GlassUi.STYLE_GHOST, null);
        wp.setOnClickListener(v -> {
            int idx = WallpaperFactory.indexOf(settings.wallpaperId);
            idx = (idx + 1) % WallpaperFactory.IDS.length;
            settings.wallpaperId = WallpaperFactory.IDS[idx];
            settings.customWallpaper = "";
            settings.save();
            wp.setText("背景：" + WallpaperFactory.nameOf(settings.wallpaperId));
            Haptics.tap(this);
            d.dismiss();
            loadWallpaper();
            recreate();
        });
        col.addView(wp, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        LinearLayout wpRow = GlassUi.row(this);
        TextView pick = GlassUi.button(this, "从相册选背景", GlassUi.STYLE_SOFT, null);
        pick.setOnClickListener(v -> {
            Haptics.tap(this);
            pickWallpaper();
        });
        wpRow.addView(pick, GlassUi.lpMr(this, 0, 0f));
        LinearLayout.LayoutParams plp = (LinearLayout.LayoutParams) pick.getLayoutParams();
        plp.width = 0;
        plp.weight = 1f;
        pick.setLayoutParams(plp);

        TextView reset = GlassUi.button(this, "恢复默认", GlassUi.STYLE_GHOST, null);
        reset.setOnClickListener(v -> {
            Haptics.tap(this);
            WallpaperFactory.clearCustom(this);
            settings.customWallpaper = "";
            settings.wallpaperId = WallpaperFactory.DEFAULT_ID;
            settings.save();
            d.dismiss();
            loadWallpaper();
            recreate();
        });
        wpRow.addView(reset, GlassUi.lpMr(this, 0, 0f));
        LinearLayout.LayoutParams rlp = (LinearLayout.LayoutParams) reset.getLayoutParams();
        rlp.width = 0;
        rlp.weight = 1f;
        rlp.leftMargin = GlassUi.dpi(this, 8f);
        reset.setLayoutParams(rlp);
        col.addView(wpRow, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        TextView about = GlassUi.label(this, "桌面端：NFC Mini Studio 3.x\n"
                + "本机：v" + BuildConfig.VERSION_NAME + "\n"
                + "协议：NFC Mini 2.0 私有帧协议（FF LEN MSGID DATA SUM FE）");
        about.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        col.addView(about, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 14f));

        TextView close = GlassUi.button(this, "关闭", GlassUi.STYLE_PRIMARY, d::dismiss);
        close.setLayoutParams(GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 14f));
        col.addView(close);

        card.addView(col);
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER);
        int m = GlassUi.dpi(this, 24f);
        clp.leftMargin = m;
        clp.rightMargin = m;
        root.addView(card, clp);
        d.setContentView(root);
        android.view.Window w = d.getWindow();
        if (w != null) {
            w.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0x00000000));
            w.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        }
        d.show();
    }

    /** 申请「忽略电池优化」，MIUI 上还建议手动把本应用设为「无限制」。 */
    private static final int REQ_PICK_WALLPAPER = 0x51;

    /** 从系统相册选一张图作为玻璃背景。 */
    private void pickWallpaper() {
        try {
            Intent it = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            it.addCategory(Intent.CATEGORY_OPENABLE);
            it.setType("image/*");
            startActivityForResult(it, REQ_PICK_WALLPAPER);
        } catch (Throwable t) {
            try {
                Intent it = new Intent(Intent.ACTION_GET_CONTENT);
                it.setType("image/*");
                startActivityForResult(it, REQ_PICK_WALLPAPER);
            } catch (Throwable t2) {
                GlassUi.toast(this, "打不开相册：" + t2.getMessage());
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_WALLPAPER) return;
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            GlassUi.toast(this, "没有选择图片");
            return;
        }
        String name = WallpaperFactory.importCustom(this, data.getData());
        if (name == null) {
            GlassUi.toast(this, "这张图读不出来，换一张试试");
            return;
        }
        settings.customWallpaper = name;
        settings.save();
        loadWallpaper();
        Haptics.success(this);
        GlassUi.toast(this, "背景已换成你选的图");
        recreate();
    }

    private void requestBatteryWhitelist() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                android.os.PowerManager pm =
                        (android.os.PowerManager) getSystemService(POWER_SERVICE);
                if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    GlassUi.toast(this, "已经在白名单里了，不会被省电杀掉");
                    return;
                }
                android.content.Intent i = new android.content.Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                i.setData(android.net.Uri.parse("package:" + getPackageName()));
                startActivity(i);
                GlassUi.toast(this, "同意「允许后台运行」后，服务就不会被省电策略杀掉");
            }
        } catch (Throwable t) {
            GlassUi.toast(this, "请到系统设置里把本应用设为「无限制」");
        }
    }

    private String vibLabel() {
        switch (Haptics.getLevel()) {
            case Haptics.OFF:
                return "关";
            case Haptics.LIGHT:
                return "轻";
            case Haptics.STRONG:
                return "强";
            case Haptics.MEDIUM:
            default:
                return "中";
        }
    }

    private void askNotifyPermission() {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
                }
            }
        } catch (Throwable ignore) {
        }
    }

    private void copy(String label, String text) {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) cm.setPrimaryClip(ClipData.newPlainText(label, text));
            GlassUi.toast(this, "已复制：" + text);
            Haptics.select(this);
        } catch (Throwable ignore) {
        }
    }

    // ------------------------------------------------------------ ReaderCore 回调

    @Override
    public void onLog(String line) {
        ui.post(() -> {
            if (logView == null) return;
            String old = logView.getText().toString();
            String merged = old.isEmpty() ? line : (old + "\n" + line);
            String[] lines = merged.split("\n");
            if (lines.length > 120) {
                StringBuilder sb = new StringBuilder();
                for (int i = lines.length - 120; i < lines.length; i++) {
                    if (sb.length() > 0) sb.append('\n');
                    sb.append(lines[i]);
                }
                merged = sb.toString();
            }
            logView.setText(merged);
        });
    }

    @Override
    public void onServerState(boolean running, int clients, String hint) {
        ui.post(() -> {
            refreshService();
            if (hint != null && running && clients > 0) {
                GlassUi.toast(this, hint);
                Haptics.select(this);
            }
        });
    }

    @Override
    public void onCardState() {
        ui.post(this::refreshCardLine);
    }
}
