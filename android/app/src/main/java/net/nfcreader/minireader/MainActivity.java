package net.nfcreader.minireader;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.content.res.ColorStateList;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

/**
 * 主界面（液态玻璃风格）。
 *
 * 相对旧版的改动：
 *  - 全新的玻璃质感 UI：模糊背景 + 半透明卡片 + 高光描边
 *  - 内置 7 套渐变背景，支持从相册选自定义背景
 *  - NFC 硬件检测与「去开启」引导；Android 13+ 请求通知权限（前台服务必需）
 *  - 新增「本机读卡」工具：不连电脑也能读卡片信息 / 逐个扇区看数据
 *  - 服务改由前台 Service 托管，切后台不再掉线
 *  - 修掉「8.5 秒后强制认为卡片已离开」的致命 bug
 */
public final class MainActivity extends Activity implements ReaderCore.Listener {

    private static final int REQ_PICK_WALLPAPER = 2001;
    private static final int REQ_NOTIFICATION = 2002;

    private final Handler ui = new Handler(Looper.getMainLooper());

    private ReaderCore core;
    private AppSettings settings;
    private NfcAdapter nfcAdapter;
    private boolean readerOn = false;
    private int pickRunning = 0;

    // 视图
    private FrameLayout rootLayout;
    private ImageView imgWallpaper;
    private View scrimView;
    private TextView tvVersion;
    private TextView pillNfc, pillNet, pillCard;
    private TextView tvServiceState, tvEndpoint, tvClients;
    private TextView btnService;
    private TextView chipPort;
    private TextView tvCardUid, tvCardType, tvCardTech, tvCardSak, tvCardSize;
    private TextView tvBlurLabel, tvLog;
    private LinearLayout wallpaperStrip;
    private ScrollView logScroll;
    private SeekBar seekBlur;
    private Switch swGlass, swLight, swKeepScreen, swVibrate, swAutoStart;
    private EditText etPort;
    private TextView btnReadInfo, btnReadSector, btnCopyUid, btnPickWallpaper;
    private TextView btnCopyLog, btnClearLog, btnNfcSettings, btnReprobe;

    // ==================================================================== 生命周期

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        core = ReaderCore.get();
        core.init(this);
        settings = core.settings();
        DeviceInfo.init(this);
        nfcAdapter = NfcAdapter.getDefaultAdapter(this);

        setContentView(R.layout.activity_main);
        bindViews();
        buildWallpaperStrip();
        styleAll();
        applyWallpaper();
        bindEvents();
        applySettingsToViews();

        core.addListener(this);

        ui.postDelayed(tick, 400);
        // 自动启动服务
        if (settings.autoStart) {
            ui.postDelayed(() -> {
                ReaderService.start(this);
                ui.postDelayed(this::refreshAll, 500);
            }, 700);
        }
        requestNotificationPermission();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (settings.keepScreenOn) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        checkNfc();
        enableReaderMode();
        refreshAll();
    }

    @Override
    protected void onPause() {
        super.onPause();
        disableReaderMode();
    }

    @Override
    protected void onDestroy() {
        core.removeListener(this);
        ui.removeCallbacksAndMessages(null);
        super.onDestroy();
    }

    // ==================================================================== 视图绑定

    private void bindViews() {
        rootLayout = findViewById(R.id.rootLayout);
        imgWallpaper = findViewById(R.id.imgWallpaper);
        scrimView = findViewById(R.id.scrimView);
        tvVersion = findViewById(R.id.tvVersion);
        pillNfc = findViewById(R.id.pillNfc);
        pillNet = findViewById(R.id.pillNet);
        pillCard = findViewById(R.id.pillCard);
        tvServiceState = findViewById(R.id.tvServiceState);
        tvEndpoint = findViewById(R.id.tvEndpoint);
        tvClients = findViewById(R.id.tvClients);
        btnService = findViewById(R.id.btnService);
        chipPort = findViewById(R.id.chipPort);
        tvCardUid = findViewById(R.id.tvCardUid);
        tvCardType = findViewById(R.id.tvCardType);
        tvCardTech = findViewById(R.id.tvCardTech);
        tvCardSak = findViewById(R.id.tvCardSak);
        tvCardSize = findViewById(R.id.tvCardSize);
        tvBlurLabel = findViewById(R.id.tvBlurLabel);
        tvLog = findViewById(R.id.tvLog);
        wallpaperStrip = findViewById(R.id.wallpaperStrip);
        logScroll = findViewById(R.id.logScroll);
        seekBlur = findViewById(R.id.seekBlur);
        swGlass = findViewById(R.id.swGlass);
        swLight = findViewById(R.id.swLight);
        swKeepScreen = findViewById(R.id.swKeepScreen);
        swVibrate = findViewById(R.id.swVibrate);
        swAutoStart = findViewById(R.id.swAutoStart);
        etPort = findViewById(R.id.etPort);
        btnReadInfo = findViewById(R.id.btnReadInfo);
        btnReadSector = findViewById(R.id.btnReadSector);
        btnCopyUid = findViewById(R.id.btnCopyUid);
        btnPickWallpaper = findViewById(R.id.btnPickWallpaper);
        btnCopyLog = findViewById(R.id.btnCopyLog);
        btnClearLog = findViewById(R.id.btnClearLog);
        btnNfcSettings = findViewById(R.id.btnNfcSettings);
        btnReprobe = findViewById(R.id.btnReprobe);
    }

    // ==================================================================== 事件

    private void bindEvents() {
        btnService.setOnClickListener(v -> toggleService());
        chipPort.setOnClickListener(v -> {
            etPort.requestFocus();
            etPort.setSelection(etPort.getText().length());
        });
        btnNfcSettings.setOnClickListener(v -> openNfcSettings());
        btnReprobe.setOnClickListener(v -> {
            checkNfc();
            enableReaderMode();
            refreshAll();
            toast("已重新检测");
        });

        btnReadInfo.setOnClickListener(v -> readCardInfoAsync());
        btnReadSector.setOnClickListener(v -> showReadSectorDialog());
        btnCopyUid.setOnClickListener(v -> copyText("UID", tvCardUid.getText().toString()));

        btnPickWallpaper.setOnClickListener(v -> pickWallpaper());
        btnCopyLog.setOnClickListener(v -> copyText("日志", tvLog.getText().toString()));
        btnClearLog.setOnClickListener(v -> {
            core.clearLog();
            tvLog.setText("");
        });

        seekBlur.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                tvBlurLabel.setText("背景模糊 " + progress);
                if (fromUser) settings.blurRadius = progress;
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
                settings.save();
                applyWallpaper();
            }
        });

        swGlass.setOnCheckedChangeListener((b, checked) -> {
            settings.glass = checked;
            settings.save();
            applyWallpaper();
        });
        swLight.setOnCheckedChangeListener((b, checked) -> {
            if (settings.lightTheme == checked) return;
            settings.lightTheme = checked;
            settings.save();
            recreate();
        });
        swKeepScreen.setOnCheckedChangeListener((b, checked) -> {
            settings.keepScreenOn = checked;
            settings.save();
            if (checked) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        });
        swVibrate.setOnCheckedChangeListener((b, checked) -> {
            settings.vibrate = checked;
            settings.save();
        });
        swAutoStart.setOnCheckedChangeListener((b, checked) -> {
            settings.autoStart = checked;
            settings.save();
        });

        findViewById(R.id.btnApplyPort).setOnClickListener(v -> applyPort());
    }

    private void applyPort() {
        String s = etPort.getText().toString().trim();
        int p;
        try {
            p = Integer.parseInt(s);
        } catch (Throwable t) {
            p = -1;
        }
        if (p < 1024 || p > 65535) {
            toast("端口需在 1024 ~ 65535 之间");
            return;
        }
        boolean wasRunning = core.isRunning();
        settings.tcpPort = p;
        settings.save();
        chipPort.setText("端口 " + p);
        if (wasRunning) {
            ReaderService.stop(this);
            ui.postDelayed(() -> {
                ReaderService.start(this);
                ui.postDelayed(this::refreshAll, 600);
            }, 400);
            toast("端口已改为 " + p + "，正在重启服务");
        } else {
            toast("端口已改为 " + p);
        }
    }

    // ==================================================================== 服务

    private void toggleService() {
        if (core.isRunning()) {
            ReaderService.stop(this);
            ui.postDelayed(this::refreshAll, 300);
        } else {
            ReaderService.start(this);
            ui.postDelayed(this::refreshAll, 500);
        }
    }

    @Override
    public void onLog(String line) {
        tvLog.append(line + "\n");
        // 只保留最后 200 行，避免文本过长卡顿
        CharSequence all = tvLog.getText();
        int lines = countLines(all);
        if (lines > 200) {
            int cut = 0;
            int seen = 0;
            int target = lines - 200;
            while (cut < all.length() && seen < target) {
                if (all.charAt(cut) == '\n') seen++;
                cut++;
            }
            tvLog.setText(all.subSequence(Math.min(cut, all.length()), all.length()));
        }
        if (logScroll != null) {
            logScroll.post(() -> logScroll.fullScroll(ScrollView.FOCUS_DOWN));
        }
    }

    @Override
    public void onServerState(boolean running, int clients, String hint) {
        updateServiceUi(running, clients);
    }

    @Override
    public void onCardState() {
        refreshCardPill();
        readCardInfoAsync();
    }

    private static int countLines(CharSequence s) {
        int n = 1;
        for (int i = 0; i < s.length(); i++) if (s.charAt(i) == '\n') n++;
        return n;
    }

    // ==================================================================== 定时刷新

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            refreshAll();
            ui.postDelayed(this, 1500);
        }
    };

    private void refreshAll() {
        try {
            DeviceInfo.refreshBattery();
            refreshPills();
            updateServiceUi(core.isRunning(), core.clientCount());
        } catch (Throwable ignore) {
        }
    }

    private void refreshPills() {
        String ip = DeviceInfo.localIp();
        boolean onWifi = DeviceInfo.onWifi(this);
        if (ip == null) {
            setPill(pillNet, "网络：未连接", false);
        } else if (!onWifi) {
            setPill(pillNet, "网络：非 Wi-Fi", false);
        } else {
            setPill(pillNet, "网络：" + ip, true);
        }
        refreshCardPill();
        int bat = DeviceInfo.batteryCache();
        // 电量信息合并进标题栏副标题
        tvVersion.setText("v" + DeviceInfo.appVersion()
                + (bat >= 0 ? "  ·  " + bat + "%" : ""));
    }

    private void refreshCardPill() {
        Tag t = core.engine() == null ? null : core.engine().getTag();
        if (t == null) {
            setPill(pillCard, "卡片：无", false);
            tvCardUid.setText("把卡贴到手机背面 NFC 区域…");
            tvCardType.setText("—");
            tvCardTech.setText("—");
            tvCardSak.setText("—");
            tvCardSize.setText("—");
        } else {
            setPill(pillCard, "卡片：" + shortUid(t.getId()), true);
        }
    }

    private static String shortUid(byte[] uid) {
        if (uid == null) return "?";
        String h = Protocol.hex(uid);
        return h.length() > 8 ? h.substring(h.length() - 8) : h;
    }

    private void updateServiceUi(boolean running, int clients) {
        if (running) {
            tvServiceState.setText("运行中");
            tvServiceState.setTextColor(settings.lightTheme ? 0xFF0F7B3F : 0xFF4ADE80);
            btnService.setText("停止服务");
            styleButton(btnService, true);
            String ip = DeviceInfo.localIp();
            tvEndpoint.setText(ip == null ? "未连接局域网" : ip + ":" + core.server().getPort());
            tvClients.setText(clients + " 个电脑连接");
        } else {
            tvServiceState.setText("未启动");
            tvServiceState.setTextColor(settings.lightTheme ? 0xFF6B7280 : 0xFF9AA4B2);
            btnService.setText("启动服务");
            styleButton(btnService, false);
            String ip = DeviceInfo.localIp();
            tvEndpoint.setText(ip == null ? "未连接局域网" : ip + ":" + settings.tcpPort);
            tvClients.setText("0 个电脑连接");
        }
        chipPort.setText("端口 " + settings.tcpPort);
    }

    // ==================================================================== NFC

    private void checkNfc() {
        if (nfcAdapter == null) {
            setPill(pillNfc, "NFC：本机不支持", false);
            btnNfcSettings.setVisibility(View.GONE);
            return;
        }
        boolean on = false;
        try {
            on = nfcAdapter.isEnabled();
        } catch (Throwable ignore) {
        }
        if (on) {
            setPill(pillNfc, "NFC：已开启", true);
            btnNfcSettings.setVisibility(View.GONE);
        } else {
            setPill(pillNfc, "NFC：未开启", false);
            btnNfcSettings.setVisibility(View.VISIBLE);
            btnNfcSettings.setText("去开启 NFC");
        }
    }

    /**
     * 打开系统 NFC 设置。不同 ROM 支持的 Action 不一样，逐级降级：
     * NFC 专属设置 → 无线设置 → 应用详情。
     */
    private void openNfcSettings() {
        String[] actions = {
                Settings.ACTION_NFC_SETTINGS,
                "android.settings.NFC_SETTINGS",
                Settings.ACTION_WIRELESS_SETTINGS,
                Settings.ACTION_SETTINGS
        };
        for (String action : actions) {
            try {
                Intent it = new Intent(action);
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
                return;
            } catch (Throwable ignore) {
            }
        }
        try {
            Intent it = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", getPackageName(), null));
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(it);
        } catch (Throwable t) {
            toast("无法打开设置，请手动到「设置 → 更多连接 → NFC」开启");
        }
    }

    private void enableReaderMode() {
        if (nfcAdapter == null || readerOn) return;
        try {
            if (!nfcAdapter.isEnabled()) return;
        } catch (Throwable t) {
            return;
        }
        try {
            Bundle opts = new Bundle();
            opts.putInt(NfcAdapter.EXTRA_READER_PRESENCE_CHECK_DELAY, 250);
            int flags = NfcAdapter.FLAG_READER_NFC_A
                    | NfcAdapter.FLAG_READER_NFC_B
                    | NfcAdapter.FLAG_READER_NFC_F
                    | NfcAdapter.FLAG_READER_NFC_V
                    | NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK
                    | NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS;
            nfcAdapter.enableReaderMode(this, this::onTagDiscovered, flags, opts);
            readerOn = true;
        } catch (Throwable t) {
            toast("开启 NFC 读卡模式失败：" + t.getMessage());
        }
    }

    private void disableReaderMode() {
        if (nfcAdapter == null || !readerOn) return;
        try {
            nfcAdapter.disableReaderMode(this);
        } catch (Throwable ignore) {
        }
        readerOn = false;
    }

    private void onTagDiscovered(Tag tag) {
        // 注意：这里绝对不能「N 秒后强制 setTag(null)」——
        // reader mode 对同一张卡只回调一次，那样会在整卡导出途中把卡判成离开。
        core.onTag(tag);
        if (settings.vibrate) {
            try {
                android.os.Vibrator v = (android.os.Vibrator)
                        getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null) v.vibrate(35);
            } catch (Throwable ignore) {
            }
        }
        runOnUiThread(() -> {
            refreshCardPill();
            readCardInfoAsync();
        });
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return;
        try {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    == PackageManager.PERMISSION_GRANTED) return;
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATION);
        } catch (Throwable ignore) {
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_NOTIFICATION) {
            // 无论是否授予都不影响核心功能，只是收不到前台服务通知
        }
    }

    // ==================================================================== 本机读卡

    private void readCardInfoAsync() {
        NfcEngine engine = core.engine();
        if (engine == null) return;
        if (pickRunning > 0) return;
        if (engine.getTag() == null) {
            refreshCardPill();
            return;
        }
        pickRunning++;
        new Thread(() -> {
            NfcEngine.CardInfo info = null;
            try {
                info = engine.readCardInfo();
            } catch (Throwable ignore) {
            }
            final NfcEngine.CardInfo f = info;
            runOnUiThread(() -> {
                pickRunning--;
                if (f == null) return;
                tvCardUid.setText(f.uid.isEmpty() ? "—" : f.uid);
                tvCardType.setText(f.label == null || f.label.isEmpty() ? "—" : f.label);
                tvCardTech.setText(f.mifareType == null || f.mifareType.isEmpty() ? "—" : f.mifareType);
                StringBuilder sb = new StringBuilder();
                if (f.sak >= 0) sb.append("SAK 0x").append(String.format("%02X", f.sak));
                if (!f.atqa.isEmpty()) sb.append(sb.length() > 0 ? " · " : "").append("ATQA ").append(f.atqa);
                tvCardSak.setText(sb.length() == 0 ? "—" : sb.toString());
                if (f.blockCount > 0) {
                    tvCardSize.setText(f.sectorCount + " 扇区 / " + f.blockCount + " 块"
                            + (f.sizeBytes > 0 ? " · " + f.sizeBytes + " 字节" : ""));
                } else {
                    tvCardSize.setText("—");
                }
            });
        }, "nfcm-cardinfo").start();
    }

    private void showReadSectorDialog() {
        NfcEngine engine = core.engine();
        if (engine == null || engine.getTag() == null) {
            toast("请先把卡贴到手机背面");
            return;
        }
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        box.setPadding(pad, dp(8), pad, 0);

        TextView t1 = new TextView(this);
        t1.setText("扇区号（0-39）");
        t1.setTextColor(textColor());
        box.addView(t1);

        EditText etSector = new EditText(this);
        etSector.setInputType(InputType.TYPE_CLASS_NUMBER);
        etSector.setText("0");
        etSector.setTextColor(textColor());
        box.addView(etSector);

        TextView t2 = new TextView(this);
        t2.setText("密钥（12 位十六进制，留空用 FFFFFFFFFFFF）");
        t2.setTextColor(textColor());
        box.addView(t2);

        EditText etKey = new EditText(this);
        etKey.setInputType(InputType.TYPE_CLASS_TEXT);
        etKey.setHint("FFFFFFFFFFFF");
        etKey.setTextColor(textColor());
        box.addView(etKey);

        new AlertDialog.Builder(this)
                .setTitle("读扇区")
                .setView(box)
                .setNegativeButton("取消", null)
                .setPositiveButton("读取", (d, w) -> {
                    int sector;
                    try {
                        sector = Integer.parseInt(etSector.getText().toString().trim());
                    } catch (Throwable t) {
                        sector = 0;
                    }
                    String ks = etKey.getText().toString().trim();
                    byte[] key = ks.isEmpty() ? Protocol.DEFAULT_KEY.clone() : Protocol.unhex(ks);
                    if (key.length != 6) {
                        toast("密钥必须是 6 字节（12 位十六进制）");
                        return;
                    }
                    doReadSector(sector, key);
                })
                .show();
    }

    private void doReadSector(int sector, byte[] key) {
        NfcEngine engine = core.engine();
        if (engine == null) return;
        final int sec = Math.max(0, Math.min(39, sector));
        new Thread(() -> {
            List<String> lines = null;
            try {
                lines = engine.readSectorLines(sec, key);
            } catch (Throwable ignore) {
            }
            final List<String> f = lines;
            runOnUiThread(() -> {
                if (f == null || f.isEmpty()) {
                    toast("认证失败或读取失败。请确认扇区号与密钥是否正确");
                    return;
                }
                StringBuilder sb = new StringBuilder();
                for (String s : f) sb.append(s).append('\n');
                showTextDialog("扇区 " + sec + " 数据", sb.toString());
            });
        }, "nfcm-readsector").start();
    }

    private void showTextDialog(String title, String body) {
        TextView tv = new TextView(this);
        tv.setText(body);
        tv.setTextSize(12f);
        tv.setTypeface(android.graphics.Typeface.MONOSPACE);
        tv.setTextColor(textColor());
        tv.setPadding(dp(20), dp(12), dp(20), dp(4));
        tv.setTextIsSelectable(true);
        android.widget.ScrollView sv = new android.widget.ScrollView(this);
        sv.addView(tv);
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setView(sv)
                .setPositiveButton("关闭", null)
                .setNeutralButton("复制", (d, w) -> copyText(title, body))
                .show();
    }

    // ==================================================================== 背景

    private void buildWallpaperStrip() {
        wallpaperStrip.removeAllViews();
        int size = dp(52);
        int gap = dp(10);
        for (int i = 0; i < WallpaperFactory.IDS.length; i++) {
            final String id = WallpaperFactory.IDS[i];
            View v = new View(this);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(size, size);
            lp.rightMargin = gap;
            v.setLayoutParams(lp);
            v.setBackground(swatchDrawable(id, false));
            v.setOnClickListener(view -> {
                settings.wallpaperId = id;
                settings.save();
                applyWallpaper();
                updateStripSelection();
                toast("背景已切换为「" + WallpaperFactory.nameOf(id) + "」");
            });
            v.setTag(id);
            wallpaperStrip.addView(v);
        }
        // 自定义入口
        TextView custom = new TextView(this);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(72), size);
        custom.setLayoutParams(lp);
        custom.setText("相册");
        custom.setGravity(Gravity.CENTER);
        custom.setTextSize(12f);
        custom.setTextColor(textColor());
        custom.setBackground(chipDrawable(true));
        custom.setOnClickListener(v -> pickWallpaper());
        wallpaperStrip.addView(custom);
        updateStripSelection();
    }

    private void updateStripSelection() {
        for (int i = 0; i < wallpaperStrip.getChildCount(); i++) {
            View c = wallpaperStrip.getChildAt(i);
            Object tag = c.getTag();
            if (tag instanceof String) {
                boolean sel = tag.equals(settings.wallpaperId);
                c.setBackground(swatchDrawable((String) tag, sel));
            }
        }
    }

    private GradientDrawable swatchDrawable(String id, boolean selected) {
        GradientDrawable gd = new GradientDrawable();
        gd.setShape(GradientDrawable.OVAL);
        int idx = WallpaperFactory.indexOf(id);
        int c0 = 0xFF0B1020, c1 = 0xFF2A3A6B;
        if (idx >= 0) {
            int[] pair = WallpaperFactory.swatchColors(id);
            c0 = pair[0];
            c1 = pair[1];
        }
        gd.setOrientation(GradientDrawable.Orientation.TL_BR);
        gd.setColors(new int[]{c0, c1});
        gd.setStroke(dp(selected ? 3 : 1), selected ? accentColor() : strokeColor());
        return gd;
    }

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
                toast("没有可用的图片选择器");
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_WALLPAPER) return;
        if (resultCode != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        toast("正在处理图片…");
        new Thread(() -> {
            String saved = WallpaperFactory.importCustom(this, uri);
            runOnUiThread(() -> {
                if (saved == null) {
                    toast("图片导入失败，请换一张试试");
                    return;
                }
                settings.customWallpaper = saved;
                settings.wallpaperId = "custom";
                settings.save();
                applyWallpaper();
                updateStripSelection();
                toast("自定义背景已应用");
            });
        }, "nfcm-wallpaper").start();
    }

    private void applyWallpaper() {
        int w = getResources().getDisplayMetrics().widthPixels;
        int h = getResources().getDisplayMetrics().heightPixels;
        if (w <= 0) w = 1080;
        if (h <= 0) h = 1920;
        Bitmap bmp = null;
        if ("custom".equals(settings.wallpaperId)) {
            if (settings.customWallpaper != null && !settings.customWallpaper.isEmpty()) {
                bmp = WallpaperFactory.loadCustom(this, settings.customWallpaper, w, h);
            }
            if (bmp == null) {
                bmp = WallpaperFactory.preset(WallpaperFactory.DEFAULT_ID, w, h);
            }
        } else {
            bmp = WallpaperFactory.preset(settings.wallpaperId, w, h);
        }
        if (bmp == null) return;

        int radius = settings.glass ? Math.max(0, Math.min(40, settings.blurRadius)) : 0;
        if (Build.VERSION.SDK_INT >= 31) {
            imgWallpaper.setImageBitmap(bmp);
            if (radius > 0) {
                try {
                    android.graphics.RenderEffect fx = android.graphics.RenderEffect.createBlurEffect(
                            radius, radius, android.graphics.Shader.TileMode.CLAMP);
                    imgWallpaper.setRenderEffect(fx);
                } catch (Throwable t) {
                    imgWallpaper.setRenderEffect(null);
                }
            } else {
                imgWallpaper.setRenderEffect(null);
            }
        } else {
            Bitmap shown = radius > 0 ? WallpaperFactory.blurFallback(bmp, radius) : bmp;
            imgWallpaper.setImageBitmap(shown);
        }
        scrimView.setBackgroundColor(WallpaperFactory.scrim(settings.lightTheme));
        tvBlurLabel.setText("背景模糊 " + radius);
        seekBlur.setProgress(radius);
    }

    // ==================================================================== 样式

    private void applySettingsToViews() {
        swGlass.setChecked(settings.glass);
        swLight.setChecked(settings.lightTheme);
        swKeepScreen.setChecked(settings.keepScreenOn);
        swVibrate.setChecked(settings.vibrate);
        swAutoStart.setChecked(settings.autoStart);
        etPort.setText(String.valueOf(settings.tcpPort));
        chipPort.setText("端口 " + settings.tcpPort);
        seekBlur.setProgress(settings.blurRadius);
        tvBlurLabel.setText("背景模糊 " + settings.blurRadius);
        tvLog.setText("");
        for (String line : core.logSnapshot()) tvLog.append(line + "\n");
        // 文本色跟随主题
        int t1 = textColor();
        int t2 = subTextColor();
        for (int id : new int[]{
                R.id.tvTitle, R.id.tvSubtitle, R.id.tvVersion,
                R.id.tvServiceState, R.id.tvEndpoint, R.id.tvClients,
                R.id.tvCardUid, R.id.tvCardType, R.id.tvCardTech, R.id.tvCardSak, R.id.tvCardSize,
                R.id.tvBlurLabel, R.id.tvLog, R.id.tvFooter, R.id.tvCardLabel,
                R.id.secService, R.id.secCard, R.id.secWall, R.id.secSettings, R.id.secLog
        }) {
            TextView tv = findViewById(id);
            if (tv != null) tv.setTextColor(id == R.id.tvSubtitle || id == R.id.tvVersion
                    || id == R.id.tvCardLabel ? t2 : t1);
        }
        etPort.setTextColor(t1);
    }

    private void styleAll() {
        // 玻璃卡片
        for (int id : new int[]{R.id.cardService, R.id.cardCard, R.id.cardWall,
                R.id.cardSettings, R.id.cardLog}) {
            View v = findViewById(id);
            if (v != null) v.setBackground(glassCardDrawable());
        }
        // 胶囊标签
        setPillBg(pillNfc);
        setPillBg(pillNet);
        setPillBg(pillCard);
        // 小按钮
        for (int id : new int[]{R.id.btnReadInfo, R.id.btnReadSector, R.id.btnCopyUid,
                R.id.btnPickWallpaper, R.id.btnCopyLog, R.id.btnClearLog,
                R.id.btnNfcSettings, R.id.btnReprobe, R.id.btnApplyPort, R.id.chipPort}) {
            View v = findViewById(id);
            if (v != null) v.setBackground(pillButtonDrawable());
        }
        styleButton(btnService, false);
        // 开关颜色
        ColorStateList thumb = ColorStateList.valueOf(accentColor());
        swGlass.setThumbTintList(thumb);
        swLight.setThumbTintList(thumb);
        swKeepScreen.setThumbTintList(thumb);
        swVibrate.setThumbTintList(thumb);
        swAutoStart.setThumbTintList(thumb);
        seekBlur.setProgressTintList(ColorStateList.valueOf(accentColor()));
        seekBlur.setThumbTintList(ColorStateList.valueOf(accentColor()));
        tvLog.setBackground(pillButtonDrawable());
        etPort.setBackground(pillButtonDrawable());
    }

    private void setPillBg(TextView v) {
        if (v != null) v.setBackground(pillDrawable());
    }

    private void setPill(TextView v, String text, boolean ok) {
        if (v == null) return;
        v.setText(text);
        v.setTextColor(ok ? okColor() : warnColor());
        setPillBg(v);
    }

    private GradientDrawable glassCardDrawable() {
        GradientDrawable gd = new GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                settings.lightTheme
                        ? new int[]{0xD9FFFFFF, 0xBFFFFFFF}
                        : new int[]{0x40FFFFFF, 0x1FFFFFFF});
        gd.setShape(GradientDrawable.RECTANGLE);
        gd.setCornerRadius(dp(22));
        gd.setStroke(dp(1), settings.lightTheme ? 0x22000000 : 0x33FFFFFF);
        return gd;
    }

    private GradientDrawable pillDrawable() {
        return pillDrawable(settings.lightTheme ? 0x1A000000 : 0x1FFFFFFF);
    }

    private GradientDrawable pillDrawable(int color) {
        GradientDrawable gd = new GradientDrawable();
        gd.setShape(GradientDrawable.RECTANGLE);
        gd.setCornerRadius(dp(20));
        gd.setColor(color);
        gd.setStroke(dp(1), settings.lightTheme ? 0x1F000000 : 0x2EFFFFFF);
        return gd;
    }

    private GradientDrawable chipDrawable(boolean pill) {
        GradientDrawable gd = pillDrawable();
        gd.setCornerRadius(dp(pill ? 20 : 14));
        return gd;
    }

    private RippleDrawable pillButtonDrawable() {
        GradientDrawable content = new GradientDrawable();
        content.setShape(GradientDrawable.RECTANGLE);
        content.setCornerRadius(dp(14));
        content.setColor(settings.lightTheme ? 0x14000000 : 0x1FFFFFFF);
        content.setStroke(dp(1), settings.lightTheme ? 0x1F000000 : 0x2EFFFFFF);
        return new RippleDrawable(
                ColorStateList.valueOf(settings.lightTheme ? 0x22000000 : 0x33FFFFFF),
                content, null);
    }

    private void styleButton(TextView btn, boolean running) {
        if (btn == null) return;
        GradientDrawable gd = new GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                running
                        ? new int[]{0xFFF0503C, 0xFFE0392B}
                        : new int[]{accentColor(), accentColorDark()});
        gd.setCornerRadius(dp(16));
        RippleDrawable rd = new RippleDrawable(
                ColorStateList.valueOf(0x33FFFFFF), gd, null);
        btn.setBackground(rd);
        btn.setTextColor(Color.WHITE);
    }

    private int accentColor() {
        return settings.lightTheme ? 0xFF2F6BFF : 0xFF6EA8FF;
    }

    private int accentColorDark() {
        return settings.lightTheme ? 0xFF1D4FD8 : 0xFF3D6FD9;
    }

    private int textColor() {
        return settings.lightTheme ? 0xFF111827 : 0xFFE9EFF7;
    }

    private int subTextColor() {
        return settings.lightTheme ? 0xFF5B6577 : 0xFF9AA6B8;
    }

    private int strokeColor() {
        return settings.lightTheme ? 0x33000000 : 0x40FFFFFF;
    }

    private int okColor() {
        return settings.lightTheme ? 0xFF0F7B3F : 0xFF4ADE80;
    }

    private int warnColor() {
        return settings.lightTheme ? 0xFFB45309 : 0xFFFFC46B;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ==================================================================== 杂项

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    private void copyText(String label, String text) {
        if (text == null || text.isEmpty()) {
            toast("没有可复制的内容");
            return;
        }
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText(label, text));
                toast("已复制");
            }
        } catch (Throwable t) {
            toast("复制失败");
        }
    }
}
