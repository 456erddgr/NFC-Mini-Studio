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
    public static final int CMD_UFUID_LOCK = 0x17;
    public static final int CMD_WRITE_BLOCK_NORMAL = 0x18;
    public static final int CMD_HARDNESTED = 0x20;
    public static final int CMD_FULL_DECODE = 0x22;
    public static final int CMD_LOCK_CARD = 0x23;
    public static final int CMD_CHECK_CARD_NO = 0x24;
    public static final int CMD_IS_BUG = 0x25;
    public static final int CMD_READ_ID_CARD = 0x26;
    public static final int CMD_WRITE_UID = 0x27;
    public static final int CMD_HALF_DECODE = 0x30;
    public static final int CMD_STATICNEST = 0x32;
    public static final int CMD_FM11RF08S = 0x37;
    public static final int CMD_AUTO_IDENTIFY = 0x38;
    public static final int CMD_KEY_CACHE = 0x40;
    public static final int CMD_BATCH_FIND = 0x41;
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

    public static final int E_OK = 0;
    public static final int E_NO_CARD = 17;
    public static final int E_AUTH = 8;
    public static final int E_READ = 9;
    public static final int E_WRITE = 10;
    public static final int E_RANGE = 15;
    public static final int E_UID = 11;

    /** 出厂通用密钥（Mifare 出厂默认）。 */
    public static final byte[] DEFAULT_KEY = {
            (byte) 0xFF, (byte) 0xFF, (byte) 0xFF, (byte) 0xFF, (byte) 0xFF, (byte) 0xFF
    };

    /** 卡类型 ID → 名称（与桌面端 protocol.CARD_TYPE 一致）。 */
    public static final String[] CARD_TYPE = {
            "未知", "IC 卡 (Mifare Classic)", "UID 卡", "ID 卡", "7 字节 UID 卡"
    };

    /** 低频 ID 卡频率档位。 */
    public static final String[] ID_FREQ = {
            "未知", "125 kHz", "250 kHz", "375 kHz", "500 kHz"
    };

    private Protocol() {
    }

    public static String errText(int code) {
        if (code >= 0 && code < ERR.length) return ERR[code];
        return "未知错误(" + code + ")";
    }

    /**
     * 状态码在应答 DATA 中的下标（与桌面端 protocol.STATUS_INDEX 完全一致）。
     *
     * 桌面端表：0x05/0x11/0x21/0x25/0x30/0x22/0x32/0x34/0x35/0x36/0x37 → 1，
     * 0x06/0x07/0x08/0x09/0x0A/0x0B/0x20 → 2，缺省 1。
     *
     * 因此这里只能把「为 2」的 7 个命令特判，其余全部返回 1。
     * 旧版本把缺省写成 2，导致桌面端读 0x27/0x17/0x23/0x24/0x26/0x38 的状态码时
     * 取错下标，永远看到「成功」——这是必须修的 bug。
     */
    public static int statusPos(int cmd) {
        switch (cmd & 0xFF) {
            case CMD_KEY_A_CHECK:
            case CMD_READ_BLOCK:
            case CMD_WRITE_BLOCK:
            case CMD_KEY_B_CHECK:
            case CMD_AUTH:
            case CMD_SCAN_CARD:
            case CMD_HARDNESTED:
                return 2;
            default:
                return 1;
        }
    }

    /** 块号 → 扇区号（Mifare Classic：0..127 每扇区 4 块，128..255 每扇区 16 块）。 */
    public static int sectorOfBlock(int block) {
        if (block < 0) return 0;
        if (block < 128) return block / 4;
        return 32 + (block - 128) / 16;
    }

    /** 扇区首块号。 */
    public static int firstBlockOfSector(int sector) {
        if (sector < 32) return sector * 4;
        return 128 + (sector - 32) * 16;
    }

    /** 扇区块数。 */
    public static int blocksOfSector(int sector) {
        return sector < 32 ? 4 : 16;
    }

    /** 扇区内最后一块是尾块（存放密钥与控制位）。 */
    public static boolean isTrailerBlock(int block) {
        int sector = sectorOfBlock(block);
        return block == firstBlockOfSector(sector) + blocksOfSector(sector) - 1;
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
                // 剥离帧头前的噪声字节
                int drop = 0;
                while (drop < b.length && b[drop] != (byte) 0xFF && b[drop] != (byte) 0xFD) drop++;
                if (drop > 0) {
                    noise += drop;
                    b = Arrays.copyOfRange(b, drop, b.length);
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
                    if (b.length < 6) {
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

    /** 补齐/截断到固定长度。 */
    public static byte[] fixed(byte[] src, int len) {
        byte[] r = new byte[len];
        if (src != null) System.arraycopy(src, 0, r, 0, Math.min(src.length, len));
        return r;
    }

    public static String hex(byte[] d) {
        if (d == null) return "";
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) sb.append(String.format("%02X", b & 0xFF));
        return sb.toString();
    }

    public static String hexSpaced(byte[] d) {
        if (d == null) return "";
        StringBuilder sb = new StringBuilder(d.length * 3);
        for (int i = 0; i < d.length; i++) {
            if (i > 0) sb.append(' ');
            sb.append(String.format("%02X", d[i] & 0xFF));
        }
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

    /** 按卡类型 ID + UID + SAK 做一次人类可读分类。 */
    public static String[] classify(int typeId, int sak) {
        // 返回 {class, label, note}
        if (typeId == 3) {
            return new String[]{"ID", "ID 卡 · EM4100/EM4200 只读卡", "125 kHz 低频卡，只能读不能写"};
        }
        if (typeId == 4) {
            return new String[]{"UID7", "7 字节 UID 卡 · Ultralight/NTAG 页式卡", "页式存储，按 4 字节一页读写"};
        }
        if (typeId == 2) {
            return new String[]{"UID", "UID 卡 · 可改写 UID 的兼容卡", "通常是可擦写的 Mifare 兼容卡"};
        }
        boolean classicSak = sak == 0x08 || sak == 0x18 || sak == 0x09
                || sak == 0x28 || sak == 0x88 || sak == 0x98;
        if (typeId == 1 || classicSak) {
            return new String[]{"IC", "IC 卡 · Mifare Classic 扇区卡", "16 扇区 / 64 块，可用密钥读写"};
        }
        return new String[]{"UNKNOWN", "未知卡型", "无法识别的卡片"};
    }

    /** 计算块 0 的 BCC（4 字节 UID 逐字节异或）。 */
    public static byte bcc(byte[] uid4) {
        return (byte) ((uid4[0] ^ uid4[1] ^ uid4[2] ^ uid4[3]) & 0xFF);
    }

    /** 构造块 0 = UID4 + BCC + SAK(0x08) + ATQA(04 00) + 厂商字节。 */
    public static byte[] buildBlockZero(byte[] uid4, int sak) {
        byte[] b = new byte[16];
        System.arraycopy(uid4, 0, b, 0, 4);
        b[4] = bcc(uid4);
        b[5] = (byte) (sak & 0xFF);
        b[6] = 0x04;
        b[7] = 0x00;
        return b;
    }
}
