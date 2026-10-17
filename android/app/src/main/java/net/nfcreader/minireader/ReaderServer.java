package net.nfcreader.minireader;

import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 手机端网络读卡服务：
 *  - TCP 8765：接受桌面端连接，收发 NFC Mini 私有帧
 *  - UDP 8766：周期性广播发现信标 "NFCMINI/1 <名字> <ip> <port>"
 *
 * 与桌面端 app/transport_net.py 严格配套。
 */
public final class ReaderServer {

    private static final String TAG = "ReaderServer";

    public static final int TCP_PORT = 8765;
    public static final int BEACON_PORT = 8766;
    public static final String BEACON_MAGIC = "NFCMINI/1";

    public interface Listener {
        void onState(boolean running, int clients, String hint);

        void onLog(String line);
    }

    private final NfcEngine engine;
    private final Listener listener;
    private final AtomicBoolean running = new AtomicBoolean(false);

    private volatile ServerSocket serverSocket;
    private volatile DatagramSocket beaconSocket;
    private Thread acceptThread;
    private Thread beaconThread;
    private final List<Socket> clients = Collections.synchronizedList(new ArrayList<Socket>());

    public ReaderServer(NfcEngine engine, Listener listener) {
        this.engine = engine;
        this.listener = listener;
    }

    public boolean isRunning() {
        return running.get();
    }

    public int clientCount() {
        return clients.size();
    }

    // ------------------------------------------------------------------ 启停

    public synchronized void start() {
        if (running.get()) return;
        running.set(true);
        try {
            serverSocket = new ServerSocket();
            serverSocket.setReuseAddress(true);
            serverSocket.bind(new InetSocketAddress(TCP_PORT));
        } catch (IOException e) {
            log("TCP 端口 " + TCP_PORT + " 绑定失败: " + e.getMessage());
            running.set(false);
            state("端口 " + TCP_PORT + " 被占用，请关闭其它读卡服务后重试");
            return;
        }
        startBeacon();
        acceptThread = new Thread(this::acceptLoop, "nfcm-reader-accept");
        acceptThread.setDaemon(true);
        acceptThread.start();
        log("读卡服务已启动：TCP " + TCP_PORT + "，发现端口 UDP " + BEACON_PORT);
        state(null);
    }

    public synchronized void stop() {
        if (!running.getAndSet(false)) return;
        closeQuietly(serverSocket);
        serverSocket = null;
        if (beaconSocket != null) {
            try { beaconSocket.close(); } catch (Exception ignore) { }
            beaconSocket = null;
        }
        synchronized (clients) {
            for (Socket s : clients) closeQuietly(s);
            clients.clear();
        }
        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }
        if (beaconThread != null) {
            beaconThread.interrupt();
            beaconThread = null;
        }
        log("读卡服务已停止");
        state(null);
    }

    private void state(String hint) {
        if (listener != null) listener.onState(running.get(), clients.size(), hint);
    }

    private void log(String s) {
        Log.d(TAG, s);
        if (listener != null) listener.onLog(s);
    }

    // ------------------------------------------------------------------ TCP

    private void acceptLoop() {
        while (running.get()) {
            try {
                ServerSocket ss = serverSocket;
                if (ss == null) break;
                Socket s = ss.accept();
                if (!running.get()) {
                    closeQuietly(s);
                    break;
                }
                try {
                    s.setTcpNoDelay(true);
                } catch (Exception ignore) { }
                clients.add(s);
                log("桌面端已连接 " + s.getInetAddress().getHostAddress());
                // 握手文本：桌面端 NetTransport 会剥掉首个换行前的握手内容
                try {
                    OutputStream os = s.getOutputStream();
                    os.write((BEACON_MAGIC + " " + MainActivity.deviceName()
                            + " " + localIp() + " " + TCP_PORT + "\n").getBytes("UTF-8"));
                    os.flush();
                } catch (Exception ignore) { }
                state(null);
                Thread t = new Thread(() -> serveClient(s), "nfcm-reader-client");
                t.setDaemon(true);
                t.start();
            } catch (IOException e) {
                if (running.get()) {
                    log("accept 异常: " + e.getMessage());
                }
                break;
            }
        }
    }

    private void serveClient(Socket s) {
        Protocol.Parser parser = new Protocol.Parser();
        byte[] buf = new byte[1024];
        try {
            InputStream is = s.getInputStream();
            OutputStream os = s.getOutputStream();
            while (running.get() && !s.isClosed()) {
                int n = is.read(buf);
                if (n < 0) break;
                if (n == 0) continue;
                byte[] chunk = new byte[n];
                System.arraycopy(buf, 0, chunk, 0, n);
                for (Protocol.Frame f : parser.feed(chunk)) {
                    byte[] reply = engine.dispatch(f.data);
                    byte[] frame = Protocol.buildFrame(reply, f.msgid);
                    os.write(frame);
                    os.flush();
                }
            }
        } catch (IOException e) {
            Log.d(TAG, "客户端会话结束: " + e.getMessage());
        } finally {
            closeQuietly(s);
            clients.remove(s);
            log("桌面端已断开");
            state(null);
        }
    }

    // ------------------------------------------------------------------ UDP 信标

    private void startBeacon() {
        beaconThread = new Thread(() -> {
            try {
                DatagramSocket ds = new DatagramSocket();
                ds.setBroadcast(true);
                ds.setReuseAddress(true);
                beaconSocket = ds;
                String msg = BEACON_MAGIC + " " + MainActivity.deviceName()
                        + " " + localIp() + " " + TCP_PORT;
                byte[] payload = msg.getBytes("UTF-8");
                while (running.get()) {
                    try {
                        DatagramPacket p = new DatagramPacket(payload, payload.length,
                                InetAddress.getByName("255.255.255.255"), BEACON_PORT);
                        ds.send(p);
                    } catch (Exception e) {
                        Log.d(TAG, "信标发送失败: " + e.getMessage());
                    }
                    try {
                        Thread.sleep(1500);
                    } catch (InterruptedException ie) {
                        break;
                    }
                }
            } catch (Exception e) {
                log("信标广播不可用: " + e.getMessage());
            }
        }, "nfcm-reader-beacon");
        beaconThread.setDaemon(true);
        beaconThread.start();
    }

    // ------------------------------------------------------------------ 工具

    public static String localIp() {
        return MainActivity.localIp();
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c != null) {
            try { c.close(); } catch (Exception ignore) { }
        }
    }

    @SuppressWarnings("unused")
    private static List<String> interfaces() {
        List<String> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (ni.isUp() && !ni.isLoopback()) out.add(ni.getName());
            }
        } catch (Exception ignore) { }
        return out;
    }
}
