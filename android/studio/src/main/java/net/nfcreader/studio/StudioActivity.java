package net.nfcreader.studio;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.graphics.drawable.GradientDrawable;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
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

import java.util.List;

/**
 * NFC 完整工具箱（studio）。
 *
 * 这是「把电脑端全部功能搬到手机」的版本：读卡、写卡、破解、卡夹、设置五个页签，
 * 全部在本机完成，不需要连接电脑；同时也能像轻量版一样把自己变成电脑的读卡器。
 *
 * 与轻量版（net.nfcreader.minireader）共用 shared/java 里的同一套核心代码，
 * 所以协议、引擎、玻璃 UI、震动反馈完全一致。
 */
public final class StudioActivity extends AppCompatActivity
        implements NfcAdapter.ReaderCallback, ReaderCore.Listener {

    private static final String[] TABS = {"读卡", "写卡", "破解", "卡夹", "设置"};

    private ReaderCore core;
    private NfcEngine engine;
    private AppSettings settings;
    private NfcAdapter nfc;
    private boolean readerOn = false;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final StringBuilder logBuf = new StringBuilder();

    private View topSpace, bottomSpace;
    private TextView pillCard, tvCardInfo;
    private FrameLayout pageHost;
    private LinearLayout tabRow;
    private TextView[] tabViews = new TextView[TABS.length];

    private TextView outView;
    private LinearLayout sectorBox;
    private CardOps.Dump lastDump;
    private int tab = 0;

    private EditText edBlock, edData, edKey;
    private int writeKeyType = 0;
    private TextView btnKeyType;
    private EditText edUid, edUidSak, edUidKey;

    // ------------------------------------------------------------ 生命周期

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        GlassUi.fullscreen(this);
        core = ReaderCore.get();
        core.init(this);
        core.addListener(this);
        engine = core.engine();
        settings = core.settings();
        nfc = NfcAdapter.getDefaultAdapter(this);
        loadWallpaper();
        buildShell();
        showTab(0);
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
        refreshPill();
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

    private void enableReader() {
        if (nfc == null || !nfc.isEnabled() || readerOn) return;
        try {
            Bundle opts = new Bundle();
            opts.putInt(NfcAdapter.EXTRA_READER_PRESENCE_CHECK_DELAY, 250);
            int flags = NfcAdapter.FLAG_READER_NFC_A
                    | NfcAdapter.FLAG_READER_NFC_B
                    | NfcAdapter.FLAG_READER_NFC_F
                    | NfcAdapter.FLAG_READER_NFC_V
                    | NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS;
            nfc.enableReaderMode(this, this, flags, opts);
            readerOn = true;
        } catch (Throwable t) {
            core.onLog("开启 NFC 读卡模式失败：" + t.getMessage());
        }
    }

    private void disableReader() {
        if (nfc == null || !readerOn) return;
        try {
            nfc.disableReaderMode(this);
        } catch (Throwable ignore) {
        }
        readerOn = false;
    }

    @Override
    public void onTagDiscovered(Tag tag) {
        if (tag == null) return;
        core.onTag(tag);
        Haptics.cardDetected(this);
        ui.post(this::refreshPill);
    }

    // ------------------------------------------------------------ 外壳

    private void buildShell() {
        FrameLayout root = GlassUi.background(this);

        LinearLayout vert = GlassUi.column(this);
        int sidePad = GlassUi.dpi(this, 14f);
        vert.setPadding(sidePad, 0, sidePad, 0);

        topSpace = new View(this);
        bottomSpace = new View(this);
        vert.addView(topSpace, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, GlassUi.dpi(this, 10f)));

        LinearLayout header = GlassUi.row(this);
        LinearLayout hcol = GlassUi.column(this);
        hcol.addView(GlassUi.h1(this, "NFC 完整工具箱"));
        hcol.addView(GlassUi.label(this, "v" + BuildConfig.VERSION_NAME + " · 手机上完成读卡 / 写卡 / 破解"));
        header.addView(hcol, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        header.addView(GlassUi.button(this, "识别", GlassUi.STYLE_SOFT, this::doIdentify));
        vert.addView(header, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        LinearLayout pillRow = GlassUi.row(this);
        pillCard = GlassUi.pill(this, "无卡", GlassUi.DIM, 0x1AFFFFFF);
        pillRow.addView(pillCard);
        vert.addView(pillRow, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));

        pageHost = new FrameLayout(this);
        LinearLayout.LayoutParams hostLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        vert.addView(pageHost, hostLp);

        GlassCardView bar = GlassUi.card(this, 10f);
        tabRow = GlassUi.row(this);
        tabRow.setGravity(Gravity.CENTER);
        for (int i = 0; i < TABS.length; i++) {
            final int idx = i;
            TextView t = GlassUi.text(this, TABS[i], 14f, GlassUi.DIM);
            t.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
            t.setGravity(Gravity.CENTER);
            t.setPadding(0, GlassUi.dpi(this, 10f), 0, GlassUi.dpi(this, 10f));
            t.setOnClickListener(v -> {
                Haptics.select(this);
                showTab(idx);
            });
            tabViews[i] = t;
            tabRow.addView(t, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
        bar.addView(tabRow);
        vert.addView(bar, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));

        vert.addView(bottomSpace, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, GlassUi.dpi(this, 8f)));

        root.addView(vert, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        GlassUi.watchInsets(root, (top, bottom, left, right) -> {
            topSpace.getLayoutParams().height = top + GlassUi.dpi(this, 6f);
            bottomSpace.getLayoutParams().height = bottom + GlassUi.dpi(this, 6f);
            topSpace.requestLayout();
            bottomSpace.requestLayout();
        });
    }

    private void showTab(int idx) {
        tab = idx;
        for (int i = 0; i < tabViews.length; i++) {
            tabViews[i].setTextColor(i == idx ? GlassUi.ACCENT : GlassUi.DIM);
        }
        LinearLayout col = GlassUi.column(this);
        ScrollView sv = new ScrollView(this);
        sv.setFillViewport(true);
        sv.setVerticalScrollBarEnabled(false);
        sv.setOverScrollMode(View.OVER_SCROLL_NEVER);
        sv.addView(col, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        pageHost.removeAllViews();
        pageHost.addView(sv, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        outView = null;
        sectorBox = null;
        switch (idx) {
            case 0:
                pageRead(col);
                break;
            case 1:
                pageWrite(col);
                break;
            case 2:
                pageCrack(col);
                break;
            case 3:
                pageCards(col);
                break;
            default:
                pageSettings(col);
                break;
        }
        refreshPill();
    }

    /** 输出区：切页后重新指向新的控件，但内容保留。 */
    private void attachOut(LinearLayout col) {
        boolean empty = logBuf.length() == 0;
        outView = GlassUi.mono(this, empty ? "（操作结果会显示在这里）" : logBuf.toString());
        outView.setTextColor(empty ? GlassUi.DIM : 0xFFC9D6E6);
        outView.setPadding(GlassUi.dpi(this, 10f), GlassUi.dpi(this, 10f),
                GlassUi.dpi(this, 10f), GlassUi.dpi(this, 10f));
        GradientDrawable g = GlassUi.inputBg(this);
        g.setColor(0x33000000);
        outView.setBackground(g);
        outView.setMinLines(6);
        col.addView(outView, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
    }

    private void out(String s) {
        logBuf.setLength(0);
        logBuf.append(s);
        if (outView != null) {
            outView.setTextColor(0xFFC9D6E6);
            outView.setText(s);
        }
    }

    private void append(String line) {
        boolean wasEmpty = logBuf.length() == 0;
        if (!wasEmpty) logBuf.append('\n');
        logBuf.append(line);
        if (outView != null) {
            if (wasEmpty) outView.setTextColor(0xFFC9D6E6);
            outView.setText(logBuf.toString());
        }
    }

    // ------------------------------------------------------------ 页：读卡

    private void pageRead(LinearLayout col) {
        GlassCardView card = GlassUi.card(this);
        LinearLayout c = GlassUi.column(this);
        c.addView(GlassUi.h2(this, "卡片信息"));
        tvCardInfo = GlassUi.label(this, "把卡贴到手机背面 NFC 区域，然后点「识别」。");
        tvCardInfo.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        c.addView(tvCardInfo, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 6f));

        LinearLayout row = GlassUi.row(this);
        TextView b1 = GlassUi.button(this, "识别", GlassUi.STYLE_SOFT, this::doIdentify);
        TextView b2 = GlassUi.button(this, "读整卡", GlassUi.STYLE_PRIMARY, this::doReadAll);
        TextView b3 = GlassUi.button(this, "存入卡夹", GlassUi.STYLE_GHOST, this::saveDump);
        LinearLayout.LayoutParams l1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        l1.rightMargin = GlassUi.dpi(this, 8f);
        LinearLayout.LayoutParams l2 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        l2.rightMargin = GlassUi.dpi(this, 8f);
        row.addView(b1, l1);
        row.addView(b2, l2);
        row.addView(b3, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        c.addView(row, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));
        card.addView(c);
        col.addView(card, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 0f));

        GlassCardView sec = GlassUi.card(this);
        LinearLayout s = GlassUi.column(this);
        s.addView(GlassUi.h2(this, "扇区视图"));
        sectorBox = GlassUi.column(this);
        TextView secHint = GlassUi.mono(this, "读整卡后这里会列出每个扇区的密钥与 16 个块的数据。");
        secHint.setTextColor(GlassUi.DIM);
        sectorBox.addView(secHint);
        s.addView(sectorBox, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
        sec.addView(s);
        col.addView(sec, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        attachOut(col);
        if (lastDump != null) renderSectors(lastDump);
    }

    private void renderSectors(CardOps.Dump d) {
        if (sectorBox == null) return;
        sectorBox.removeAllViews();
        if (d == null || d.blockCount <= 0) return;
        for (int sec = 0; sec < d.sectorCount; sec++) {
            String key = d.keyFor(sec);
            String kt = d.keyTypeFor(sec) == 0 ? "KEY A" : "KEY B";
            TextView h = GlassUi.mono(this, "扇区 " + sec + "  "
                    + (key == null ? "密钥未知（该扇区打不开）" : kt + " " + key));
            h.setTextColor(key == null ? 0xFFFF8A8A : GlassUi.ACCENT2);
            sectorBox.addView(h);
            int first = Protocol.firstBlockOfSector(sec);
            int count = Protocol.blocksOfSector(sec);
            for (int i = 0; i < count; i++) {
                int b = first + i;
                if (b >= d.blockCount) break;
                boolean ok = b < d.blockOk.length && d.blockOk[b];
                String line = "  块 " + b + "  " + (ok ? d.hexBlock(b).toUpperCase() : "读取失败");
                if (Protocol.isTrailerBlock(b)) line += "   （尾块）";
                TextView t = GlassUi.mono(this, line);
                t.setTextColor(ok ? 0xFF9FB3C8 : 0xFF6B7A8C);
                sectorBox.addView(t);
            }
        }
    }

    private void doIdentify() {
        Haptics.select(this);
        Task.bg(() -> engine.readCardInfo(), (ci, err) -> {
            if (err != null || ci == null) {
                Haptics.error(this);
                refreshPill();
                return;
            }
            if (!ci.present) {
                if (tvCardInfo != null) tvCardInfo.setText(ci.note == null ? "没有检测到卡片" : ci.note);
                Haptics.error(this);
                refreshPill();
                return;
            }
            StringBuilder sb = new StringBuilder();
            sb.append(ci.className).append(" / ").append(ci.mifareType).append("\n");
            sb.append("UID  ").append(ci.uid).append("   ATQA ").append(ci.atqa)
                    .append("   SAK ").append(ci.sak < 0 ? "—" : String.format("%02X", ci.sak)).append("\n");
            sb.append(ci.sectorCount).append(" 扇区 · ").append(ci.blockCount)
                    .append(" 块 · ").append(ci.sizeBytes).append(" 字节\n");
            sb.append("技术 ").append(ci.techs).append("\n");
            sb.append(ci.label);
            if (tvCardInfo != null) tvCardInfo.setText(sb.toString());
            Haptics.success(this);
            refreshPill();
        });
    }

    private void doReadAll() {
        Haptics.select(this);
        out("开始读整卡（逐扇区试密钥，可能要十几秒）…");
        Task.bg(() -> CardOps.readAll(engine, null, (done, total2, line) -> ui.post(() -> append(line))),
                (d, err) -> {
                    if (err != null || d == null) {
                        append("读卡异常：" + (err == null ? "未知" : err.getMessage()));
                        Haptics.error(this);
                        return;
                    }
                    lastDump = d;
                    if (d.blockCount <= 0) {
                        append(d.note);
                        Haptics.error(this);
                        return;
                    }
                    StringBuilder sb = new StringBuilder();
                    sb.append("UID ").append(d.uid).append(" · ").append(d.kind).append("\n");
                    sb.append("成功 ").append(d.okCount).append(" 块 / 失败 ")
                            .append(d.failCount).append(" 块 / 共 ")
                            .append(d.blockCount).append(" 块");
                    out(sb.toString());
                    renderSectors(d);
                    Haptics.success(this);
                });
    }

    private void saveDump() {
        if (lastDump == null || lastDump.uid == null || lastDump.uid.isEmpty()) {
            GlassUi.toast(this, "先读一次整卡再保存");
            return;
        }
        CardStore.Card cc = new CardStore.Card();
        cc.uid = lastDump.uid;
        cc.atqa = lastDump.atqa;
        cc.sak = lastDump.sak;
        cc.blockCount = lastDump.blockCount;
        cc.kind = lastDump.kind;
        cc.key = lastDump.keyFor(0) == null ? "" : lastDump.keyFor(0);
        String[] blocks = new String[lastDump.blockCount];
        for (int i = 0; i < lastDump.blockCount; i++) {
            blocks[i] = (i < lastDump.blockOk.length && lastDump.blockOk[i]) ? lastDump.hexBlock(i) : "";
        }
        cc.blocks = blocks;
        cc.name = "手机读卡 " + lastDump.uid;
        String id = CardStore.save(this, cc);
        if (id != null) {
            GlassUi.toast(this, "已存入卡夹");
            Haptics.success(this);
        } else {
            GlassUi.toast(this, "保存失败");
            Haptics.error(this);
        }
    }

    // ------------------------------------------------------------ 页：写卡

    private void pageWrite(LinearLayout col) {
        GlassCardView c1 = GlassUi.card(this);
        LinearLayout a = GlassUi.column(this);
        a.addView(GlassUi.h2(this, "写单个块"));
        a.addView(GlassUi.label(this, "数据 32 位十六进制，密钥 12 位十六进制"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));

        LinearLayout r1 = GlassUi.row(this);
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
        r1.addView(edBlock, w1);
        r1.addView(btnKeyType, new LinearLayout.LayoutParams(
                GlassUi.dpi(this, 92f), ViewGroup.LayoutParams.WRAP_CONTENT));
        a.addView(r1, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        edData = GlassUi.field(this, "数据 32 位十六进制");
        edData.setText("00000000000000000000000000000000");
        a.addView(edData, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
        edKey = GlassUi.field(this, "密钥 12 位十六进制");
        edKey.setText("FFFFFFFFFFFF");
        a.addView(edKey, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        a.addView(GlassUi.button(this, "写入这一块", GlassUi.STYLE_PRIMARY, this::doWriteBlock),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
        TextView warn = GlassUi.label(this, "块 0 是 UID/厂商块，写坏可能变砖；尾块里的密钥位写错会锁死卡片。");
        warn.setTextColor(0xFFFFB020);
        warn.setLineSpacing(GlassUi.dp(this, 2f), 1f);
        a.addView(warn, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 8f));
        c1.addView(a);
        col.addView(c1, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 0f));

        GlassCardView c2 = GlassUi.card(this);
        LinearLayout b = GlassUi.column(this);
        b.addView(GlassUi.h2(this, "改 UID（块 0）"));
        b.addView(GlassUi.label(this, "只对可写 UID 的卡（UID 卡 / CUID / FUID）有效。8 位十六进制 UID。"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));
        edUid = GlassUi.field(this, "新 UID，例如 11223344");
        edUid.setText("11223344");
        b.addView(edUid, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
        LinearLayout r2 = GlassUi.row(this);
        edUidSak = GlassUi.field(this, "SAK");
        edUidSak.setText("08");
        edUidKey = GlassUi.field(this, "密钥");
        edUidKey.setText("FFFFFFFFFFFF");
        LinearLayout.LayoutParams w2 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        w2.rightMargin = GlassUi.dpi(this, 8f);
        LinearLayout.LayoutParams w3 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 2f);
        r2.addView(edUidSak, w2);
        r2.addView(edUidKey, w3);
        b.addView(r2, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
        b.addView(GlassUi.button(this, "写入 UID", GlassUi.STYLE_DANGER, this::doWriteUid),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
        c2.addView(b);
        col.addView(c2, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        GlassCardView c3 = GlassUi.card(this);
        LinearLayout cc = GlassUi.column(this);
        cc.addView(GlassUi.h2(this, "整卡 / 卡片操作"));
        cc.addView(GlassUi.label(this, "把卡夹里的卡写回空白卡，或把数据块清空。"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));
        LinearLayout r3 = GlassUi.row(this);
        TextView bf = GlassUi.button(this, "格式化数据块", GlassUi.STYLE_GHOST, this::doFormat);
        TextView bg = GlassUi.button(this, "去卡夹写入", GlassUi.STYLE_SOFT, () -> {
            Haptics.select(this);
            showTab(3);
        });
        LinearLayout.LayoutParams w4 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        w4.rightMargin = GlassUi.dpi(this, 8f);
        r3.addView(bf, w4);
        r3.addView(bg, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        cc.addView(r3, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));
        c3.addView(cc);
        col.addView(c3, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        attachOut(col);
    }

    private void doWriteBlock() {
        int block;
        try {
            block = Integer.parseInt(edBlock.getText().toString().trim());
        } catch (Throwable t) {
            GlassUi.toast(this, "块号必须是数字");
            Haptics.error(this);
            return;
        }
        String data = edData.getText().toString().trim();
        String key = edKey.getText().toString().trim();
        final int kt = writeKeyType;
        if (data.replaceAll("[^0-9a-fA-F]", "").length() != 32) {
            GlassUi.toast(this, "数据必须 32 位十六进制");
            Haptics.error(this);
            return;
        }
        Haptics.select(this);
        out("写入块 " + block + " …");
        Task.bg(() -> CardOps.writeBlockHex(engine, block, data, key, kt), (ok, err) -> {
            if (err != null) {
                append("写入异常：" + err.getMessage());
                Haptics.error(this);
                return;
            }
            append(Boolean.TRUE.equals(ok) ? ("块 " + block + " 写入成功")
                    : ("块 " + block + " 写入失败（密钥不对 / 块被保护）"));
            if (Boolean.TRUE.equals(ok)) Haptics.success(this);
            else Haptics.error(this);
        });
    }

    private void doWriteUid() {
        String uidHex = edUid.getText().toString().replaceAll("[^0-9a-fA-F]", "");
        if (uidHex.length() != 8) {
            GlassUi.toast(this, "UID 必须是 8 位十六进制（4 字节）");
            Haptics.error(this);
            return;
        }
        int sak;
        try {
            sak = Integer.parseInt(edUidSak.getText().toString().trim(), 16);
        } catch (Throwable t) {
            sak = 0x08;
        }
        final int fSak = sak & 0xFF;
        final byte[] uid4 = Protocol.unhex(uidHex);
        final byte[] key = CardOps.hex(edUidKey.getText().toString());
        if (key == null || key.length != 6) {
            GlassUi.toast(this, "密钥必须是 12 位十六进制");
            Haptics.error(this);
            return;
        }
        GlassUi.confirm(this, "改 UID", "即将把块 0 的 UID 改成 " + uidHex.toUpperCase()
                + "。写错会导致卡片无法识别，确定继续？", "确定写入", () -> {
            Haptics.select(this);
            out("写入 UID " + uidHex.toUpperCase() + " …");
            Task.bg(() -> {
                byte[] block0 = Protocol.buildBlockZero(uid4, fSak);
                return engine.writeBlockData(0, block0, key);
            }, (ok, err) -> {
                if (err != null) {
                    append("写入 UID 异常：" + err.getMessage());
                    Haptics.error(this);
                    return;
                }
                append(Boolean.TRUE.equals(ok) ? "UID 写入成功，重新贴卡生效"
                        : "UID 写入失败（这张卡可能不允许改 UID）");
                if (Boolean.TRUE.equals(ok)) Haptics.success(this);
                else Haptics.error(this);
            });
        });
    }

    private void doFormat() {
        GlassUi.confirm(this, "格式化数据块",
                "会把 1~N 块的数据全部写成 0（跳过块 0 和尾块）。此操作不可撤销，确定继续？",
                "确定格式化", () -> {
                    Haptics.select(this);
                    out("开始格式化…");
                    Task.bg(() -> {
                        byte[] key = Protocol.DEFAULT_KEY;
                        NfcEngine.CardInfo ci = engine.readCardInfo();
                        int n = 0;
                        if (!ci.present || ci.blockCount <= 0) return 0;
                        for (int b = 1; b < ci.blockCount; b++) {
                            if (Protocol.isTrailerBlock(b)) continue;
                            if (engine.writeBlockData(b, new byte[16], key)) n++;
                        }
                        return n;
                    }, (n, err) -> {
                        if (err != null) {
                            append("格式化异常：" + err.getMessage());
                            Haptics.error(this);
                            return;
                        }
                        append("格式化完成，写入 " + n + " 块（用默认密钥 FFFFFFFFFFFF）");
                        Haptics.success(this);
                    });
                });
    }

    // ------------------------------------------------------------ 页：破解

    private void pageCrack(LinearLayout col) {
        GlassCardView card = GlassUi.card(this);
        LinearLayout c = GlassUi.column(this);
        c.addView(GlassUi.h2(this, "密钥破解"));
        c.addView(GlassUi.label(this, "用内置字典（40 条常见密钥）逐扇区尝试认证。不需要原厂硬件，"
                + "纯手机就能跑。"), GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));

        LinearLayout r = GlassUi.row(this);
        TextView b1 = GlassUi.button(this, "自动破解", GlassUi.STYLE_PRIMARY, this::doCrack);
        TextView b2 = GlassUi.button(this, "保存结果", GlassUi.STYLE_GHOST, this::saveDump);
        LinearLayout.LayoutParams l1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        l1.rightMargin = GlassUi.dpi(this, 8f);
        r.addView(b1, l1);
        r.addView(b2, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        c.addView(r, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        TextView tip = GlassUi.label(this, "字典只覆盖常见出厂密钥与国内门禁常用值。"
                + "全加密卡需要原厂读卡器的硬件随机数采集（HardNested）才能破，手机做不到。");
        tip.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        c.addView(tip, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 12f));
        card.addView(c);
        col.addView(card, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 0f));

        attachOut(col);
    }

    private void doCrack() {
        Haptics.select(this);
        out("开始破解（逐个扇区尝试字典密钥）…");
        Task.bg(() -> CardOps.crack(engine, null, (done, total2, line) -> ui.post(() -> append(line))),
                (d, err) -> {
                    if (err != null || d == null) {
                        append("破解异常：" + (err == null ? "未知" : err.getMessage()));
                        Haptics.error(this);
                        return;
                    }
                    if (d.sectorCount <= 0) {
                        append(d.note);
                        Haptics.error(this);
                        return;
                    }
                    lastDump = d;
                    StringBuilder sb = new StringBuilder();
                    sb.append("命中 ").append(d.okCount).append(" / ").append(d.sectorCount)
                            .append(" 个扇区\n");
                    for (int s = 0; s < d.sectorCount; s++) {
                        String k = d.keyFor(s);
                        sb.append("扇区 ").append(s).append("：")
                                .append(k == null ? "未破解" : ((d.keyTypeFor(s) == 0 ? "KEY A " : "KEY B ") + k))
                                .append("\n");
                    }
                    append(sb.toString().trim());
                    if (d.okCount > 0) Haptics.success(this);
                    else Haptics.error(this);
                });
    }

    // ------------------------------------------------------------ 页：卡夹

    private void pageCards(LinearLayout col) {
        List<CardStore.Card> cards = CardStore.list(this);
        GlassCardView head = GlassUi.card(this);
        LinearLayout h = GlassUi.column(this);
        h.addView(GlassUi.h2(this, "卡夹 · " + cards.size() + " 张"));
        h.addView(GlassUi.label(this, "整卡快照存在手机本地，可以随时写回空白卡。"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));
        h.addView(GlassUi.button(this, "刷新", GlassUi.STYLE_GHOST, () -> {
            Haptics.tap(this);
            showTab(3);
        }), GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 10f));
        head.addView(h);
        col.addView(head, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 0f));

        if (cards.isEmpty()) {
            TextView empty = GlassUi.label(this, "卡夹是空的。先在「读卡」里读一张卡。");
            empty.setGravity(Gravity.CENTER);
            empty.setPadding(0, GlassUi.dpi(this, 24f), 0, GlassUi.dpi(this, 24f));
            col.addView(empty, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
            return;
        }
        for (CardStore.Card card : cards) {
            GlassCardView cw = GlassUi.card(this, 14f);
            LinearLayout w = GlassUi.column(this);
            w.addView(GlassUi.text(this, card.displayName(), 15f, GlassUi.TEXT));
            TextView meta = GlassUi.label(this, "UID " + card.uid + " · " + card.blockCount
                    + " 块 · " + (card.kind == null || card.kind.isEmpty() ? "未知类型" : card.kind));
            w.addView(meta, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 4f));
            LinearLayout r = GlassUi.row(this);
            TextView wr = GlassUi.button(this, "写入这张卡", GlassUi.STYLE_SOFT,
                    () -> confirmWriteCard(card));
            TextView rm = GlassUi.button(this, "删除", GlassUi.STYLE_GHOST, () -> {
                GlassUi.confirm(this, "删除卡片", "确定从卡夹删除「" + card.displayName() + "」？",
                        "删除", () -> {
                            CardStore.remove(this, card.id);
                            Haptics.success(this);
                            showTab(3);
                        });
            });
            LinearLayout.LayoutParams c1 = new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 2f);
            c1.rightMargin = GlassUi.dpi(this, 8f);
            r.addView(wr, c1);
            r.addView(rm, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            w.addView(r, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
            cw.addView(w);
            col.addView(cw, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
        }
        attachOut(col);
    }

    private void confirmWriteCard(CardStore.Card card) {
        GlassUi.confirm(this, "写入卡片",
                "把「" + card.displayName() + "」的 " + card.blockCount
                        + " 块写回当前卡片（跳过尾块）。请确认贴的是对应的空白卡。",
                "开始写入", () -> {
                    Haptics.select(this);
                    out("开始写入「" + card.displayName() + "」…");
                    Task.bg(() -> {
                        CardOps.Dump d = new CardOps.Dump();
                        d.uid = card.uid;
                        d.blockCount = card.blocks == null ? 0 : card.blocks.length;
                        d.blocks = card.blocks == null ? new String[0] : card.blocks;
                        d.blockOk = new boolean[d.blocks.length];
                        for (int i = 0; i < d.blocks.length; i++) {
                            d.blockOk[i] = d.blocks[i] != null && !d.blocks[i].isEmpty();
                        }
                        byte[] key = CardOps.hex(card.key);
                        if (key == null || key.length != 6) key = Protocol.DEFAULT_KEY;
                        return CardOps.writeAll(engine, d, Protocol.hex(key), true,
                                (done, total2, line) -> ui.post(() -> append(line)));
                    }, (n, err) -> {
                        if (err != null) {
                            append("写入异常：" + err.getMessage());
                            Haptics.error(this);
                            return;
                        }
                        append("写入完成，成功 " + n + " 块");
                        Haptics.success(this);
                    });
                });
    }

    // ------------------------------------------------------------ 页：设置

    private void pageSettings(LinearLayout col) {
        GlassCardView c1 = GlassUi.card(this);
        LinearLayout a = GlassUi.column(this);
        a.addView(GlassUi.h2(this, "体验"));

        final TextView vib = GlassUi.button(this, "震动强度：" + vibLabel(), GlassUi.STYLE_GHOST,
                null);
        vib.setOnClickListener(v -> {
            Haptics.setLevel((Haptics.getLevel() + 1) % 4);
            vib.setText("震动强度：" + vibLabel());
            settings.vibrate = Haptics.getLevel() != Haptics.OFF;
            settings.save();
            Haptics.select(this);
        });
        a.addView(vib, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        final TextView keep = GlassUi.button(this, "保持屏幕常亮：" + (settings.keepScreenOn ? "开" : "关"),
                GlassUi.STYLE_GHOST, null);
        keep.setOnClickListener(v -> {
            settings.keepScreenOn = !settings.keepScreenOn;
            keep.setText("保持屏幕常亮：" + (settings.keepScreenOn ? "开" : "关"));
            settings.save();
            Haptics.tap(this);
        });
        a.addView(keep, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));

        final TextView wall = GlassUi.button(this, "背景：" + WallpaperFactory.nameOf(settings.wallpaperId),
                GlassUi.STYLE_GHOST, null);
        wall.setOnClickListener(v -> {
            int idx = WallpaperFactory.indexOf(settings.wallpaperId);
            idx = (idx + 1) % WallpaperFactory.IDS.length;
            settings.wallpaperId = WallpaperFactory.IDS[idx];
            settings.customWallpaper = "";
            settings.save();
            Haptics.tap(this);
            loadWallpaper();
            recreate();
        });
        a.addView(wall, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 8f));
        c1.addView(a);
        col.addView(c1, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 0f));

        GlassCardView c2 = GlassUi.card(this);
        LinearLayout b = GlassUi.column(this);
        b.addView(GlassUi.h2(this, "当电脑的读卡器"));
        b.addView(GlassUi.label(this, "打开后，电脑端「手机读卡器」通道就能连上这台手机。"),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 3f));
        LinearLayout r = GlassUi.row(this);
        TextView start = GlassUi.button(this, "启动服务", GlassUi.STYLE_PRIMARY, () -> {
            String err = core.start();
            GlassUi.toast(this, err == null ? "服务已启动" : err);
            Haptics.select(this);
            showTab(4);
        });
        TextView stop = GlassUi.button(this, "停止", GlassUi.STYLE_GHOST, () -> {
            core.stop();
            GlassUi.toast(this, "服务已停止");
            Haptics.tap(this);
            showTab(4);
        });
        LinearLayout.LayoutParams s1 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        s1.rightMargin = GlassUi.dpi(this, 8f);
        r.addView(start, s1);
        r.addView(stop, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        b.addView(r, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        String addr = core.isRunning()
                ? (DeviceInfo.localIp() + ":" + core.server().getPort() + "（有线 127.0.0.1:"
                + core.server().getPort() + "）")
                : "服务未启动";
        TextView addrView = GlassUi.value(this, addr);
        b.addView(addrView, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 8f));
        b.addView(GlassUi.button(this, "复制有线命令", GlassUi.STYLE_SOFT,
                () -> copy("adb", "adb reverse tcp:" + core.server().getPort()
                        + " tcp:" + core.server().getPort())),
                GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 10f));
        c2.addView(b);
        col.addView(c2, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));

        GlassCardView c3 = GlassUi.card(this);
        LinearLayout cc = GlassUi.column(this);
        cc.addView(GlassUi.h2(this, "关于"));
        TextView about = GlassUi.label(this, "NFC 完整工具箱 v" + BuildConfig.VERSION_NAME + "\n"
                + "和电脑端 NFC Mini Studio 3.x 共用同一套私有帧协议（FF LEN MSGID DATA SUM FE）。\n"
                + "能力：识别 / 读整卡 / 写块 / 改 UID / 字典破解 / 卡夹 / 当电脑读卡器。\n"
                + "限制：全加密卡的 HardNested、原厂设备的固件级功能（封卡、侦测、读 ID 卡）"
                + "需要原厂 NFC Mini 读卡器硬件，手机无法替代。");
        about.setLineSpacing(GlassUi.dp(this, 3f), 1f);
        cc.addView(about, GlassUi.lpMr(this, ViewGroup.LayoutParams.WRAP_CONTENT, 8f));
        c3.addView(cc);
        col.addView(c3, GlassUi.lpMr(this, ViewGroup.LayoutParams.MATCH_PARENT, 12f));
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

    private void copy(String label, String text) {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) cm.setPrimaryClip(ClipData.newPlainText(label, text));
            GlassUi.toast(this, "已复制：" + text);
            Haptics.select(this);
        } catch (Throwable ignore) {
        }
    }

    private void refreshPill() {
        if (pillCard == null) return;
        NfcEngine.CardInfo ci = null;
        try {
            ci = engine == null ? null : engine.readCardInfo();
        } catch (Throwable ignore) {
        }
        if (ci == null || !ci.present) {
            GlassUi.setPill(pillCard, nfc == null ? "无 NFC" : (nfc.isEnabled() ? "等待贴卡" : "NFC 未开"),
                    GlassUi.DIM, 0x1AFFFFFF);
            if (tvCardInfo != null && tvCardInfo.getText().toString().isEmpty()) {
                tvCardInfo.setText("把卡贴到手机背面 NFC 区域");
            }
            return;
        }
        GlassUi.setPill(pillCard, ci.uid == null || ci.uid.isEmpty() ? "有卡" : ci.uid,
                GlassUi.ACCENT2, 0x3322D3A6);
    }

    // ------------------------------------------------------------ ReaderCore 回调

    @Override
    public void onLog(String line) {
        ui.post(() -> {
            if (tab == 0 || tab == 2 || tab == 3) append(line);
        });
    }

    @Override
    public void onServerState(boolean running, int clients, String hint) {
        ui.post(this::refreshPill);
    }

    @Override
    public void onCardState() {
        ui.post(this::refreshPill);
    }
}
