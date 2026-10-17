package net.nfcreader.minireader;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * NFC Mini 2.0 私有帧协议（与桌面端 app/protocol.py 逐字节对齐）。
 *
 * 短帧: FF | LEN | MSGID | DATA[LEN] | SUM | FE
 * 长帧: FD | LEN_H | LEN_L | MSGID | DATA[LEN] | SUM | FE
 * SUM = sum(DATA) & 0xFF, MSGID 取 1..253
 */
public final class Protocol {

    public static final int CMD_INFO = 0x01;
    public static final int CMD_STATE = 0x02;
    public static final int CMD_KEY_A_CHECK = 0x06;
    public static final int CMD_READ_BLOCK = 0x07;
    public static final int CMD_WRITE_BLOCK = 0x08;
    public static final int CMD_KEY_B_CHECK = 0x09;
    public static final int CMD_AUTH = 0x0A;
    public static final int CMD_SCAN_CARD = 0x0B;
    public static final int CMD_WRITE_CARD = 0x0C;
    public static final int CMD_FIND_CARD = 0x11;
    public static final int CMD_WRITE_BLOCK_FAST = 0x16;
    public static final int CMD_WRITE_BLOCK_NORMAL = 0x18;
    public static final int CMD_CHECK_CARD_NO = 0x24;
    public static final int CMD_WRITE_UID = 0x27;
    public static final int CMD_RESET = 0x7F;

    /** ERR 表（与桌面端 protocol.ERR 一致）。 */
    public static final String[] ERR = {
            "成功", "错误 Code:1", "指令长度错误", "指令 CRC 校验错误",
            "固件升级地址错误", "固件升级扇区擦除失败", "固件升级写入失败",
            "IC 寻卡失败", "密钥认证失败", "IC 读卡失败", "IC 写卡失败",
            "写 UID 卡失败", "写 GTU 卡失败", "写 IC 卡控制段错误",
            "写 IC 卡厂商数据段错误", "参数超出范围", "采集随机数失败",
            "检测不到卡片，请重新摆放后再试"
    };

    public static final int E_NO_CARD = 17;
    public static final int E_AUTH = 8;
    public static final int E_READ = 9;
    public static final int E_WRITE = 10;
    public static final int E_RANGE = 15;
    public static final int E_UID = 11;

    private Protocol() {
    }

    public static String errText(int code) {
        if (code >= 0 && code < ERR.length) return ERR[code];
        return "未知错误(" + code + ")";
    }

    /** 组帧。 */
    public static byte[] buildFrame(byte[] data, int msgid) {
        if (data == null) data = new byte[0];
        int mid = msgid & 0xFF;
        if (mid == 0) mid = 1;
        int sum = 0;
        for (byte b : data) sum = (sum + (b & 0xFF)) & 0xFF;
        ByteArrayOutputStream out = new ByteArrayOutputStream(data.length + 8);
        if (data.length <= 255) {
            out.write(0xFF);
            out.write(data.length);
            out.write(mid);
            out.write(data, 0, data.length);
            out.write(sum);
            out.write(0xFE);
        } else {
            out.write(0xFD);
            out.write((data.length >> 8) & 0xFF);
            out.write(data.length & 0xFF);
            out.write(mid);
            out.write(data, 0, data.length);
            out.write(sum);
            out.write(0xFE);
        }
        return out.toByteArray();
    }

    /** 单个解析结果。 */
    public static final class Frame {
        public final int msgid;
        public final byte[] data;

        Frame(int msgid, byte[] data) {
            this.msgid = msgid;
            this.data = data;
        }

        @Override
        public String toString() {
            return "Frame{mid=" + msgid + ", len=" + data.length + "}";
        }
    }

    /** 流式帧解析器（与桌面端 FrameParser 等价）。 */
    public static final class Parser {
        private final ByteArrayOutputStream buf = new ByteArrayOutputStream();
        private int noise = 0;

        public int noiseCount() {
            return noise;
        }

        public void reset() {
            buf.reset();
        }

        public List<Frame> feed(byte[] chunk) {
            List<Frame> out = new ArrayList<>();
            if (chunk != null && chunk.length > 0) buf.write(chunk, 0, chunk.length);
            while (true) {
                byte[] b = buf.toByteArray();
                if (b.length < 2) break;
                int start = -1;
                while (b.length > 0 && b[0] != (byte) 0xFF && b[0] != (byte) 0xFD) {
                    b = Arrays.copyOfRange(b, 1, b.length);
                    noise++;
                }
                if (b.length < 2) {
                    buf.reset();
                    if (b.length == 1) buf.write(b[0]);
                    break;
                }
                boolean longFrame = b[0] == (byte) 0xFD;
                int need;
                int len;
                if (longFrame) {
                    if (b.length < 4) {
                        buf.reset();
                        buf.write(b, 0, b.length);
                        break;
                    }
                    len = ((b[1] & 0xFF) << 8) | (b[2] & 0xFF);
                    need = len + 6;
                } else {
                    len = b[1] & 0xFF;
                    need = len + 5;
                }
                if (b.length < need) {
                    // 首字节可能是噪声，丢弃后重试
                    buf.reset();
                    buf.write(b, 0, b.length);
                    break;
                }
                int tail = longFrame ? b[4 + len] : b[3 + len];
                int msgid = longFrame ? (b[3] & 0xFF) : (b[2] & 0xFF);
                byte[] payload = longFrame
                        ? Arrays.copyOfRange(b, 4, 4 + len)
                        : Arrays.copyOfRange(b, 3, 3 + len);
                int sum = 0;
                for (byte p : payload) sum = (sum + (p & 0xFF)) & 0xFF;
                boolean tailOk = b[need - 1] == (byte) 0xFE;
                if (tail == sum && tailOk && msgid > 0 && msgid < 254) {
                    out.add(new Frame(msgid, payload));
                    b = Arrays.copyOfRange(b, need, b.length);
                } else {
                    noise++;
                    b = Arrays.copyOfRange(b, 1, b.length);
                }
                buf.reset();
                if (b.length > 0) buf.write(b, 0, b.length);
            }
            // 防御性上限，避免异常数据把缓冲撑爆
            if (buf.size() > 8192) buf.reset();
            return out;
        }
    }

    public static byte[] concat(byte[] a, byte[] b) {
        byte[] r = new byte[a.length + b.length];
        System.arraycopy(a, 0, r, 0, a.length);
        System.arraycopy(b, 0, r, a.length, b.length);
        return r;
    }

    public static byte[] of(int... vals) {
        byte[] r = new byte[vals.length];
        for (int i = 0; i < vals.length; i++) r[i] = (byte) (vals[i] & 0xFF);
        return r;
    }

    public static String hex(byte[] d) {
        if (d == null) return "";
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) sb.append(String.format("%02X", b & 0xFF));
        return sb.toString();
    }

    public static byte[] unhex(String s) {
        if (s == null) return new byte[0];
        String t = s.replaceAll("[^0-9a-fA-F]", "");
        if (t.length() % 2 == 1) t = "0" + t;
        byte[] r = new byte[t.length() / 2];
        for (int i = 0; i < r.length; i++)
            r[i] = (byte) Integer.parseInt(t.substring(i * 2, i * 2 + 2), 16);
        return r;
    }
}
