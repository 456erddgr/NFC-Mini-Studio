package net.nfcreader.minireader;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * TCP 服务端 + UDP 信标。
 *
 * 与桌面端 app/transport_net.py 配套：
 *   1) 手机每 1.5 秒向 255.255.255.255:8766 广播 "NFCMINI/1 <名字> <ip> <端口>"
 *   2) 电脑连上 TCP 端口后，手机先发一行同样的握手文本（桌面会剥掉首个换行前的内容）
 *   3) 之后双向走私有帧协议
 *
 * 相对旧版的修复：
 *   - 端口可配置（旧版写死 8765）
 *   - 信标每轮重新读取本机 IP（旧版在循环外算一次，IP 变了就发旧地址）
 *   - 握手文本不再出现 "—"（旧版本机 IP 取不到时会把 "—" 当 host 发出去）
 *   - 客户端地址可查询，供 UI 显示「谁连着」
 */
public final class ReaderServer {

    public static final int DEFAULT_TCP_PORT = 8765;
    public static final int BEACON_PORT = 8766;
    public static final String BEACON_MAGIC = "NFCMINI/1";

    /** UI 回调（可能在任意线程触发，调用方自行切主线程）。 */
    public interface Listener {
        void onState(boolean running, int clients, String hint);

        void onLog(String line);
    }

    private final NfcEngine engine;
    private final List<Listener> listeners = new CopyOnWriteArrayList<>();
    private final List<Socket> clients = new CopyOnWriteArrayList<>();
    private final AtomicInteger activeClients = new AtomicInteger(0);

    private volatile boolean running = false;
    private volatile int port = DEFAULT_TCP_PORT;
    private ServerSocket serverSocket;
    private DatagramSocket beaconSocket;
    private Thread acceptThread;
    private Thread beaconThread;

    public ReaderServer(NfcEngine engine) {
        this.engine = engine;
    }

    public void addListener(Listener l) {
        if (l != null && !listeners.contains(l)) listeners.add(l);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public boolean isRunning() {
        return running;
    }

    public int getPort() {
        return port;
    }

    public void setPort(int p) {
        if (p >= 1024 && p <= 65535) port = p;
    }

    public int clientCount() {
        return activeClients.get();
    }

    public List<String> clientAddresses() {
        List<String> out = new ArrayList<>();
        for (Socket s : clients) {
            if (s == null || s.isClosed()) continue;
            InetAddress a = s.getInetAddress();
            if (a != null) out.add(a.getHostAddress());
        }
        return out;
    }

    /** 启动服务，成功返回 null，失败返回中文原因。 */
    public synchronized String start(int newPort) {
        if (running) return null;
        if (newPort >= 1024 && newPort <= 65535) port = newPort;
        try {
            ServerSocket ss = new ServerSocket();
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress(port), 4);
            serverSocket = ss;
        } catch (IOException e) {
            String hint = "端口 " + port + " 被占用或无法绑定：" + e.getMessage();
            log(hint);
            state(false, 0, hint);
            return hint;
        }
        running = true;
        acceptThread = new Thread(this::acceptLoop, "nfcm-accept");
        acceptThread.setDaemon(true);
        acceptThread.start();
        startBeacon();
        String ip = DeviceInfo.localIp();
        String hint = "已启动 · " + (ip == null ? "未连接局域网" : ip + ":" + port);
        log(hint);
        state(true, 0, hint);
        return null;
    }

    public synchronized void stop() {
        running = false;
        closeQuietly(serverSocket);
        serverSocket = null;
        stopBeacon();
        for (Socket s : clients) closeQuietly(s);
        clients.clear();
        activeClients.set(0);
        Thread at = acceptThread;
        if (at != null) {
            at.interrupt();
            try {
                at.join(600);
            } catch (InterruptedException ignore) {
            }
            acceptThread = null;
        }
        engine.closeCard();
        log("服务已停止");
        state(false, 0, "已停止");
    }

    // ---------------------------------------------------------------- 内部

    private void acceptLoop() {
        while (running && serverSocket != null && !serverSocket.isClosed()) {
            Socket s;
            try {
                s = serverSocket.accept();
            } catch (IOException e) {
                if (running) log("接受连接失败：" + e.getMessage());
                break;
            }
            try {
                s.setTcpNoDelay(true);
                s.setSoTimeout(0);
            } catch (Throwable ignore) {
            }
            clients.add(s);
            int n = activeClients.incrementAndGet();
            String addr = s.getInetAddress() == null ? "?" : s.getInetAddress().getHostAddress();
            log("电脑已连接：" + addr + "（当前 " + n + " 个）");
            state(true, n, addr + " 已连接");
            Thread t = new Thread(() -> serveClient(s), "nfcm-client");
            t.setDaemon(true);
            t.start();
        }
    }

    private void serveClient(Socket s) {
        Protocol.Parser parser = new Protocol.Parser();
        InputStream in = null;
        OutputStream out = null;
        try {
            in = s.getInputStream();
            out = s.getOutputStream();
            // 握手行：桌面端会剥掉第一个换行之前的内容
            String ip = DeviceInfo.localIp();
            if (ip == null && s.getLocalAddress() != null) {
                ip = s.getLocalAddress().getHostAddress();
            }
            if (ip == null) ip = "127.0.0.1";
            String hello = BEACON_MAGIC + " " + DeviceInfo.deviceName() + " " + ip + " " + port + "\n";
            out.write(hello.getBytes(StandardCharsets.UTF_8));
            out.flush();

            byte[] buf = new byte[2048];
            while (running && !s.isClosed()) {
                int n;
                try {
                    n = in.read(buf);
                } catch (IOException e) {
                    break;
                }
                if (n < 0) break;
                if (n == 0) continue;
                byte[] chunk = new byte[n];
                System.arraycopy(buf, 0, chunk, 0, n);
                for (Protocol.Frame f : parser.feed(chunk)) {
                    byte[] reply;
                    try {
                        reply = engine.dispatch(f.data);
                    } catch (Throwable t) {
                        reply = Protocol.of(0x00, Protocol.E_NO_CARD);
                        log("处理命令异常：" + t);
                    }
                    if (reply == null) reply = new byte[0];
                    out.write(Protocol.buildFrame(reply, f.msgid));
                    out.flush();
                }
            }
        } catch (Throwable t) {
            log("会话结束：" + t.getMessage());
        } finally {
            clients.remove(s);
            int n = Math.max(0, activeClients.decrementAndGet());
            closeQuietly(s);
            log("电脑已断开（当前 " + n + " 个）");
            state(running, n, running ? "等待连接" : "已停止");
        }
    }

    private void startBeacon() {
        stopBeacon();
        beaconThread = new Thread(() -> {
            DatagramSocket ds = null;
            try {
                ds = new DatagramSocket();
                beaconSocket = ds;
                ds.setBroadcast(true);
                while (running) {
                    // 每轮重新取 IP —— 修掉「IP 变了还发旧地址」的 bug
                    String ip = DeviceInfo.localIp();
                    if (ip != null) {
                        String msg = BEACON_MAGIC + " " + DeviceInfo.deviceName() + " " + ip + " " + port;
                        byte[] payload = msg.getBytes(StandardCharsets.UTF_8);
                        try {
                            DatagramPacket pkt = new DatagramPacket(
                                    payload, payload.length,
                                    InetAddress.getByName("255.255.255.255"), BEACON_PORT);
                            ds.send(pkt);
                        } catch (Throwable ignore) {
                        }
                    }
                    try {
                        Thread.sleep(1500);
                    } catch (InterruptedException e) {
                        break;
                    }
                }
            } catch (Throwable t) {
                log("广播信标失败：" + t.getMessage());
            } finally {
                if (ds != null) ds.close();
                if (beaconSocket == ds) beaconSocket = null;
            }
        }, "nfcm-beacon");
        beaconThread.setDaemon(true);
        beaconThread.start();
    }

    private void stopBeacon() {
        Thread bt = beaconThread;
        beaconThread = null;
        DatagramSocket ds = beaconSocket;
        beaconSocket = null;
        if (ds != null) ds.close();
        if (bt != null) {
            bt.interrupt();
            try {
                bt.join(400);
            } catch (InterruptedException ignore) {
            }
        }
    }

    private void state(boolean run, int n, String hint) {
        for (Listener l : listeners) {
            try {
                l.onState(run, n, hint);
            } catch (Throwable ignore) {
            }
        }
    }

    private void log(String line) {
        for (Listener l : listeners) {
            try {
                l.onLog(line);
            } catch (Throwable ignore) {
            }
        }
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c == null) return;
        try {
            c.close();
        } catch (Throwable ignore) {
        }
    }
}
