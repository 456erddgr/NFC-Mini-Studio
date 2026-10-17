package net.nfcreader.minireader;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.method.ScrollingMovementMethod;
import android.view.WindowManager;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.google.android.material.button.MaterialButton;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Collections;
import java.util.Locale;
import java.util.UUID;

/**
 * 手机 NFC 读卡器主界面。
 *
 * 打开后：检测 NFC → 启动 TCP 读卡服务 + UDP 信标 → 等待桌面端连接。
 * 桌面端（NFC Mini Studio）在「手机读卡器」标签页里搜到本机并连接后，
 * 就可以把手机当成一台通用 NFC 读卡器来读卡/写卡。
 */
public class MainActivity extends AppCompatActivity {

    private static final int REQ_NFC = 1001;

    private NfcAdapter nfcAdapter;
    private NfcEngine engine;
    private ReaderServer server;

    private TextView tvNfcState;
    private TextView tvCardState;
    private TextView tvServiceState;
    private TextView tvIp;
    private TextView tvClients;
    private TextView tvLog;
    private MaterialButton btnService;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private static String cachedIp = "—";
    private static int cachedBattery = 100;
    private static String cachedSn = "";

    private WifiManager.MulticastLock multicastLock;
    private boolean readerModeOn = false;

    // ---------------------------------------------------------------- 生命周期

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        tvNfcState = findViewById(R.id.tvNfcState);
        tvCardState = findViewById(R.id.tvCardState);
        tvServiceState = findViewById(R.id.tvServiceState);
        tvIp = findViewById(R.id.tvIp);
        tvClients = findViewById(R.id.tvClients);
        tvLog = findViewById(R.id.tvLog);
        btnService = findViewById(R.id.btnService);
        tvLog.setMovementMethod(new ScrollingMovementMethod());

        cachedSn = deviceSerial();
        engine = new NfcEngine(this::appendLog);
        server = new ReaderServer(engine, new ReaderServer.Listener() {
            @Override
            public void onState(boolean running, int clients, String hint) {
                ui.post(() -> renderServiceState(running, clients, hint));
            }

            @Override
            public void onLog(String line) {
                ui.post(() -> appendLog(line));
            }
        });

        nfcAdapter = NfcAdapter.getDefaultAdapter(this);
        checkNfc();

        findViewById(R.id.btnNfcSettings).setOnClickListener(v -> {
            try {
                startActivity(new Intent(Settings.ACTION_NFC_SETTINGS));
            } catch (Exception e) {
                startActivity(new Intent(Settings.ACTION_SETTINGS));
            }
        });

        btnService.setOnClickListener(v -> {
            if (server.isRunning()) {
                server.stop();
            } else {
                acquireMulticastLock();
                server.start();
                enableReaderMode();
            }
            renderServiceState(server.isRunning(), server.clientCount(), null);
        });

        refreshIp();
        updateBattery();

        // 默认自动启动，手机打开即待命
        ui.postDelayed(() -> {
            if (nfcAdapter != null && nfcAdapter.isEnabled()) {
                acquireMulticastLock();
                server.start();
                enableReaderMode();
                renderServiceState(true, 0, null);
            }
        }, 600);

        // 定时刷新电量 / IP / 客户端数
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                updateBattery();
                refreshIp();
                if (server.isRunning()) {
                    tvClients.setText(String.valueOf(server.clientCount()));
                }
                ui.postDelayed(this, 3000);
            }
        }, 3000);
    }

    @Override
    protected void onResume() {
        super.onResume();
        checkNfc();
        if (server != null && server.isRunning()) {
            enableReaderMode();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        disableReaderMode();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ui.removeCallbacksAndMessages(null);
        if (server != null) server.stop();
        releaseMulticastLock();
    }

    // ---------------------------------------------------------------- NFC

    private void checkNfc() {
        if (nfcAdapter == null) {
            tvNfcState.setText(R.string.nfc_unsupported);
            btnService.setEnabled(false);
            return;
        }
        if (!nfcAdapter.isEnabled()) {
            tvNfcState.setText(R.string.nfc_disabled);
            return;
        }
        tvNfcState.setText(R.string.nfc_ready);
    }

    private void enableReaderMode() {
        if (nfcAdapter == null || readerModeOn) return;
        if (!nfcAdapter.isEnabled()) return;
        try {
            Bundle opts = new Bundle();
            opts.putInt(NfcAdapter.EXTRA_READER_PRESENCE_CHECK_DELAY, 250);
            nfcAdapter.enableReaderMode(this,
                    this::onTagDiscovered,
                    NfcAdapter.FLAG_READER_NFC_A
                            | NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK
                            | NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS,
                    opts);
            readerModeOn = true;
            appendLog("NFC 读卡模式已开启");
        } catch (Exception e) {
            appendLog("开启读卡模式失败: " + e.getMessage());
        }
    }

    private void disableReaderMode() {
        if (nfcAdapter == null || !readerModeOn) return;
        try {
            nfcAdapter.disableReaderMode(this);
        } catch (Exception ignore) { }
        readerModeOn = false;
    }

    private void onTagDiscovered(Tag tag) {
        engine.setTag(tag);
        String uid = Protocol.hex(tag.getId());
        ui.post(() -> {
            tvCardState.setText(getString(R.string.card_present, uid));
            appendLog("卡片 " + uid);
        });
        // 5 秒内没有再次回调则认为卡片已离开
        ui.postDelayed(() -> {
            if (engine.getTag() == tag && !engine.hasTag()) {
                engine.setTag(null);
                tvCardState.setText(R.string.card_none);
            }
        }, 8500);
    }

    // ---------------------------------------------------------------- 渲染

    private void renderServiceState(boolean running, int clients, String hint) {
        if (hint != null && !hint.isEmpty()) {
            tvServiceState.setText(hint);
        } else if (running) {
            tvServiceState.setText(R.string.service_running);
        } else {
            tvServiceState.setText(R.string.service_stopped);
        }
        btnService.setText(running ? R.string.btn_stop : R.string.btn_start);
        tvClients.setText(String.valueOf(clients));
    }

    private void appendLog(String line) {
        String ts = String.format(Locale.US, "%02d:%02d:%02d ",
                new java.util.Date().getHours(),
                new java.util.Date().getMinutes(),
                new java.util.Date().getSeconds());
        CharSequence old = tvLog.getText();
        String base = old == null ? "" : old.toString();
        String[] lines = base.isEmpty() ? new String[0] : base.split("\n");
        StringBuilder sb = new StringBuilder();
        int start = Math.max(0, lines.length - 200);
        for (int i = start; i < lines.length; i++) sb.append(lines[i]).append('\n');
        sb.append(ts).append(line).append('\n');
        tvLog.setText(sb.toString());
    }

    private void refreshIp() {
        cachedIp = localIp();
        tvIp.setText(cachedIp);
    }

    private void updateBattery() {
        try {
            BatteryManager bm = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
            if (bm != null) {
                cachedBattery = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
                if (cachedBattery <= 0 || cachedBattery > 100) cachedBattery = 100;
            }
        } catch (Exception ignore) { }
    }

    private void acquireMulticastLock() {
        try {
            if (multicastLock == null) {
                WifiManager wm = (WifiManager) getApplicationContext()
                        .getSystemService(Context.WIFI_SERVICE);
                if (wm != null) {
                    multicastLock = wm.createMulticastLock("nfcm-reader");
                    multicastLock.setReferenceCounted(false);
                }
            }
            if (multicastLock != null && !multicastLock.isHeld()) multicastLock.acquire();
        } catch (Exception ignore) { }
    }

    private void releaseMulticastLock() {
        try {
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        } catch (Exception ignore) { }
    }

    // ---------------------------------------------------------------- 静态信息（供 NfcEngine/ReaderServer 使用）

    public static String deviceName() {
        String model = Build.MODEL == null ? "Android" : Build.MODEL;
        return model.replace(' ', '-');
    }

    public static String deviceSn() {
        if (cachedSn != null && !cachedSn.isEmpty()) return cachedSn;
        try {
            String id = Build.FINGERPRINT + Build.SERIAL + Build.MODEL;
            return String.format("%012X", Math.abs(id.hashCode() * 2654435761L) & 0xFFFFFFFFFFFFL);
        } catch (Exception e) {
            return "000000000000";
        }
    }

    private String deviceSerial() {
        try {
            String serial = Build.SERIAL;
            if (serial == null || serial.isEmpty() || Build.UNKNOWN.equals(serial)) {
                serial = UUID.randomUUID().toString();
            }
            String h = Protocol.hex(serial.getBytes("UTF-8"));
            return h.length() > 12 ? h.substring(0, 12) : String.format("%12s", h).replace(' ', '0');
        } catch (Exception e) {
            return "000000000000";
        }
    }

    public static int batteryLevel() {
        return cachedBattery <= 0 ? 100 : cachedBattery;
    }

    public static String localIp() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                String name = ni.getName();
                if (name != null && (name.startsWith("wlan") || name.startsWith("ap")
                        || name.startsWith("eth"))) {
                    for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                        if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                            return addr.getHostAddress();
                        }
                    }
                }
            }
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                        return addr.getHostAddress();
                    }
                }
            }
        } catch (Exception ignore) { }
        return "—";
    }
}
