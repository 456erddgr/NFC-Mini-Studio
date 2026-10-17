package net.nfcreader.minireader;

import android.nfc.Tag;
import android.nfc.tech.MifareClassic;
import android.nfc.tech.NfcA;
import android.util.Log;

import java.io.IOException;
import java.util.HashMap;
import java.util.Map;

/**
 * 把手机 NFC 变成「NFC Mini 2.0 协议兼容读卡器」。
 *
 * 收到的是桌面端下发的 DATA 区（不含帧头），返回的也是 DATA 区应答。
 * 所有布局与桌面端 app/device.py 的解析严格对齐。
 */
public final class NfcEngine {

    private static final String TAG = "NfcEngine";

    /** 最近一次被读到的卡片（由 MainActivity 的 reader-mode 回调更新）。 */
    private volatile Tag currentTag;
    private volatile long lastSeenMs = 0L;

    /** 认证缓存：(sector<<1|keyType) -> 是否已认证。 */
    private final Map<Integer, Boolean> authed = new HashMap<>();
    private int authedTagHash = 0;

    /** 默认密钥（出厂 FF x6）。 */
    private byte[] defaultKey = new byte[]{-1, -1, -1, -1, -1, -1};

    /** 日志回调（给 UI 显示）。 */
    public interface Logger {
        void log(String line);
    }

    private final Logger logger;

    public NfcEngine(Logger logger) {
        this.logger = logger;
    }

    private void log(String s) {
        Log.d(TAG, s);
        if (logger != null) logger.log(s);
    }

    public void setTag(Tag tag) {
        this.currentTag = tag;
        this.lastSeenMs = System.currentTimeMillis();
        if (tag != null) {
            int h = tag.hashCode();
            if (h != authedTagHash) {
                authedTagHash = h;
                authed.clear();
            }
            log("检测到卡片 " + Protocol.hex(tag.getId()));
        } else {
            authed.clear();
            authedTagHash = 0;
            log("卡片离开");
        }
    }

    public Tag getTag() {
        return currentTag;
    }

    public boolean hasTag() {
        Tag t = currentTag;
        if (t == null) return false;
        // 卡片 8 秒内没再被回调认为已经拿走了
        return System.currentTimeMillis() - lastSeenMs < 8000L;
    }

    public void setDefaultKey(byte[] key) {
        if (key != null && key.length == 6) defaultKey = key.clone();
    }

    // ------------------------------------------------------------------ 分发

    /** 处理一条桌面端命令，返回 DATA 区应答；永不返回 null。 */
    public byte[] dispatch(byte[] req) {
        if (req == null || req.length == 0) return Protocol.of(0, 15);
        int cmd = req[0] & 0xFF;
        try {
            switch (cmd) {
                case Protocol.CMD_INFO:
                    return handleInfo();
                case Protocol.CMD_STATE:
                    return handleState();
                case Protocol.CMD_FIND_CARD:
                    return handleFindCard();
                case Protocol.CMD_KEY_A_CHECK:
                case Protocol.CMD_KEY_B_CHECK:
                    return handleKeyCheck(req);
                case Protocol.CMD_AUTH:
                    return handleAuth(req);
                case Protocol.CMD_READ_BLOCK:
                    return handleReadBlock(req);
                case Protocol.CMD_WRITE_BLOCK:
                    return handleWriteBlock08(req);
                case Protocol.CMD_WRITE_BLOCK_FAST:
                    return handleWriteBlock16(req);
                case Protocol.CMD_WRITE_BLOCK_NORMAL:
                    return handleWriteBlock18(req);
                case Protocol.CMD_SCAN_CARD:
                    return Protocol.of(Protocol.CMD_SCAN_CARD, 0);
                case Protocol.CMD_WRITE_CARD:
                case Protocol.CMD_WRITE_UID:
                    return handleWriteCardNo(req, cmd);
                case Protocol.CMD_CHECK_CARD_NO:
                    return handleCheckCardNo();
                case Protocol.CMD_RESET:
                    authed.clear();
                    return Protocol.of(Protocol.CMD_RESET, 0);
                default:
                    log("不支持的指令 0x" + String.format("%02X", cmd));
                    return Protocol.of(cmd, 15);
            }
        } catch (Throwable t) {
            log("指令 0x" + String.format("%02X", cmd) + " 失败: " + t);
            return Protocol.of(cmd, statusIndex(cmd), errFor(cmd));
        }
    }

    private static int statusIndex(int cmd) {
        switch (cmd) {
            case 0x05: case 0x11: case 0x21: case 0x25: case 0x30: case 0x22:
            case 0x32: case 0x34: case 0x35: case 0x36: case 0x37:
                return 1;
            default:
                return 2;
        }
    }

    private static int errFor(int cmd) {
        switch (cmd) {
            case 0x06: case 0x09: case 0x0A:
                return Protocol.E_AUTH;
            case 0x07:
                return Protocol.E_READ;
            case 0x08: case 0x16: case 0x18:
                return Protocol.E_WRITE;
            case 0x11:
                return Protocol.E_NO_CARD;
            default:
                return Protocol.E_RANGE;
        }
    }

    /** 生成带状态码的失败应答：把状态码放到正确下标。 */
    private static byte[] fail(int cmd, int code) {
        int idx = statusIndex(cmd);
        byte[] r = new byte[idx + 1];
        r[0] = (byte) cmd;
        r[idx] = (byte) (code & 0xFF);
        return r;
    }

    // ------------------------------------------------------------------ 基础指令

    private byte[] handleInfo() {
        // [0]=cmd, [1..3]=version(1.0.0), [4..15]=SN(12 字节)
        byte[] sn = new byte[12];
        String id = MainActivity.deviceSn();
        byte[] raw = Protocol.unhex(id);
        for (int i = 0; i < 12; i++) sn[i] = i < raw.length ? raw[i] : (byte) 0xAA;
        byte[] r = new byte[16];
        r[0] = (byte) Protocol.CMD_INFO;
        r[1] = 1;
        r[2] = 0;
        r[3] = 0;
        System.arraycopy(sn, 0, r, 4, 12);
        return r;
    }

    private byte[] handleState() {
        int level = MainActivity.batteryLevel();
        return Protocol.of(Protocol.CMD_STATE, level);
    }

    private byte[] handleFindCard() {
        Tag t = currentTag;
        if (t == null || !hasTag()) return fail(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
        try {
            byte[] uid = t.getId();
            NfcA na = NfcA.get(t);
            int sak = 0;
            if (na != null) {
                try {
                    na.connect();
                    sak = na.getSak();
                } finally {
                    try { na.close(); } catch (Exception ignore) { }
                }
            }
            boolean isClassic = MifareClassic.get(t) != null;
            if (uid != null && uid.length >= 7) {
                // 7 字节 UID：type_id=4，桌面端保持原序
                // 布局: 11 00 04 uid7[7] sak is_uid => 12 字节
                byte[] r = new byte[12];
                r[0] = (byte) Protocol.CMD_FIND_CARD;
                r[1] = 0;
                r[2] = 4;
                System.arraycopy(uid, 0, r, 3, 7);
                r[10] = (byte) (sak & 0xFF);
                r[11] = 0; // is_uid_card
                return r;
            }
            if (uid == null || uid.length < 4) {
                return fail(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
            }
            // 4 字节 UID：type_id=1(IC) 或 2(UID)；桌面端会对 4 字节做 byte 反序
            int typeId = isClassic ? 1 : 2;
            byte[] r = new byte[9];
            r[0] = (byte) Protocol.CMD_FIND_CARD;
            r[1] = 0;
            r[2] = (byte) typeId;
            // 设备固件回的是反序 UID，桌面端 uid_reverse() 会翻回来，所以这里发反序
            r[3] = uid[3];
            r[4] = uid[2];
            r[5] = uid[1];
            r[6] = uid[0];
            r[7] = (byte) (sak & 0xFF);
            r[8] = 0; // is_uid_card
            return r;
        } catch (Exception e) {
            log("寻卡失败: " + e);
            return fail(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
        }
    }

    private byte[] handleKeyCheck(byte[] req) {
        int cmd = req[0] & 0xFF;
        if (req.length < 8) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        byte[] key = new byte[6];
        System.arraycopy(req, 2, key, 0, 6);
        int keyType = cmd == Protocol.CMD_KEY_B_CHECK ? 1 : 0;
        boolean ok = authenticate(block, keyType, key);
        int status = ok ? 0 : Protocol.E_AUTH;
        byte[] r = new byte[3];
        r[0] = (byte) cmd;
        r[1] = (byte) (block & 0xFF);
        r[2] = (byte) status;
        return r;
    }

    private byte[] handleAuth(byte[] req) {
        // 0x0A 在原厂固件里是空命令，恒回 0A 00 00
        return Protocol.of(Protocol.CMD_AUTH, 0, 0);
    }

    private byte[] handleReadBlock(byte[] req) {
        int cmd = Protocol.CMD_READ_BLOCK;
        if (req.length < 2) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        int keyType = req.length >= 3 ? (req[2] & 1) : 0;
        byte[] data = readBlock(block, keyType);
        if (data == null) return fail(cmd, Protocol.E_READ);
        byte[] r = new byte[18];
        r[0] = (byte) cmd;
        r[1] = 0;
        System.arraycopy(data, 0, r, 2, 16);
        return r;
    }

    // ---- 写块：三种布局都要兼容 ----

    private byte[] handleWriteBlock08(byte[] req) {
        int cmd = Protocol.CMD_WRITE_BLOCK;
        if (req.length < 4) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        // int keyTypeHint = req[2] & 1;
        byte[] rest = new byte[req.length - 3];
        System.arraycopy(req, 3, rest, 0, rest.length);

        byte[] data;
        byte[] key;
        int keyType = 0;
        if (rest.length >= 22) {
            // 先假设 data_then_key，用后 6 字节当密钥尝试认证
            byte[] k1 = slice(rest, 16, 6);
            byte[] d1 = slice(rest, 0, 16);
            if (authenticate(block, 0, k1)) {
                data = d1;
                key = k1;
            } else if (authenticate(block, 0, slice(rest, 0, 6))) {
                // key_then_data
                key = slice(rest, 0, 6);
                data = slice(rest, 6, 16);
            } else if (authenticate(block, 1, k1)) {
                data = d1;
                key = k1;
                keyType = 1;
            } else if (authenticate(block, 1, slice(rest, 0, 6))) {
                key = slice(rest, 0, 6);
                data = slice(rest, 6, 16);
                keyType = 1;
            } else {
                // 认证全失败：按首选布局试写一次（依赖桌面端已预认证）
                data = d1;
                key = k1;
            }
        } else if (rest.length >= 16) {
            data = slice(rest, 0, 16);
            key = defaultKey;
        } else {
            return fail(cmd, Protocol.E_RANGE);
        }

        boolean ok = writeBlock(block, data, keyType, key);
        return writeResult(cmd, block, ok);
    }

    private byte[] handleWriteBlock16(byte[] req) {
        // 布局: 16 blk data16
        int cmd = Protocol.CMD_WRITE_BLOCK_FAST;
        if (req.length < 18) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        byte[] data = slice(req, 2, 16);
        boolean ok = writeBlock(block, data, 0, defaultKey);
        return writeResult(cmd, block, ok);
    }

    private byte[] handleWriteBlock18(byte[] req) {
        // 布局: 18 blk 00 data16
        int cmd = Protocol.CMD_WRITE_BLOCK_NORMAL;
        if (req.length < 19) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        byte[] data = slice(req, 3, 16);
        boolean ok = writeBlock(block, data, 0, defaultKey);
        return writeResult(cmd, block, ok);
    }

    private static byte[] writeResult(int cmd, int block, boolean ok) {
        byte[] r = new byte[3];
        r[0] = (byte) cmd;
        r[1] = (byte) (block & 0xFF);
        r[2] = (byte) (ok ? 0 : Protocol.E_WRITE);
        return r;
    }

    private byte[] handleWriteCardNo(byte[] req, int cmd) {
        // 0x0C: [cmd] + 5 字节卡号；0x27: [cmd, 00] + 6 字节 UID
        byte[] uid;
        if (cmd == Protocol.CMD_WRITE_UID) {
            if (req.length < 8) return fail(cmd, Protocol.E_RANGE);
            uid = slice(req, 2, 6);
        } else {
            if (req.length < 6) return fail(cmd, Protocol.E_RANGE);
            byte[] no = slice(req, 1, 5);
            uid = new byte[6];
            uid[0] = no[0];
            uid[1] = no[1];
            uid[2] = no[2];
            uid[3] = no[3];
            uid[4] = (byte) bcc(no[0], no[1], no[2], no[3]);
            uid[5] = 0x08;
        }
        // 写块 0：UID[4] + BCC + SAK + ATQA(2) + 厂商(8)
        byte[] block0 = new byte[16];
        block0[0] = uid[0];
        block0[1] = uid[1];
        block0[2] = uid[2];
        block0[3] = uid[3];
        block0[4] = (byte) bcc(uid[0], uid[1], uid[2], uid[3]);
        block0[5] = 0x08;   // SAK
        block0[6] = 0x04;   // ATQA lo
        block0[7] = 0x00;   // ATQA hi
        // 后 8 字节厂商数据保持 0（部分魔术卡要求）
        boolean ok = writeBlock(0, block0, 0, defaultKey);
        if (!ok) {
            log("写卡号/UID 失败：块 0 被写保护（该卡不是 UID 可写卡或需要专用指令）");
        }
        return writeResult(cmd, 0, ok);
    }

    private byte[] handleCheckCardNo() {
        if (!hasTag()) return fail(Protocol.CMD_CHECK_CARD_NO, Protocol.E_NO_CARD);
        // 手机端无法得知期望卡号，返回 status=1（通过）交给桌面端按当前卡号显示
        return Protocol.of(Protocol.CMD_CHECK_CARD_NO, 1);
    }

    // ------------------------------------------------------------------ 卡片操作

    private static int bcc(byte... b) {
        int x = 0;
        for (byte v : b) x ^= (v & 0xFF);
        return x & 0xFF;
    }

    private static byte[] slice(byte[] src, int off, int len) {
        byte[] r = new byte[len];
        System.arraycopy(src, off, r, 0, Math.min(len, src.length - off));
        return r;
    }

    private static int sectorOfBlock(int block) {
        if (block < 128) return block / 4;
        return 32 + (block - 128) / 16;
    }

    private MifareClassic openClassic() {
        Tag t = currentTag;
        if (t == null) return null;
        MifareClassic mc = MifareClassic.get(t);
        if (mc == null) return null;
        try {
            mc.connect();
            return mc;
        } catch (IOException e) {
            log("连接卡片失败: " + e);
            return null;
        }
    }

    private static void closeQuietly(MifareClassic mc) {
        if (mc != null) {
            try { mc.close(); } catch (Exception ignore) { }
        }
    }

    private boolean authenticate(int block, int keyType, byte[] key) {
        if (key == null || key.length != 6) return false;
        int sector = sectorOfBlock(block);
        int cacheKey = (sector << 1) | (keyType & 1);
        if (Boolean.TRUE.equals(authed.get(cacheKey))) return true;
        MifareClassic mc = openClassic();
        if (mc == null) return false;
        try {
            boolean ok = keyType == 1
                    ? mc.authenticateSectorWithKeyB(sector, key)
                    : mc.authenticateSectorWithKeyA(sector, key);
            if (ok) authed.put(cacheKey, true);
            return ok;
        } catch (Exception e) {
            Log.d(TAG, "auth fail block=" + block + " keyType=" + keyType + ": " + e);
            return false;
        } finally {
            closeQuietly(mc);
        }
    }

    private byte[] readBlock(int block, int keyType) {
        MifareClassic mc = openClassic();
        if (mc == null) return null;
        try {
            int sector = sectorOfBlock(block);
            boolean ok = keyType == 1
                    ? mc.authenticateSectorWithKeyB(sector, defaultKey)
                    : mc.authenticateSectorWithKeyA(sector, defaultKey);
            if (!ok) {
                // 试另一个密钥类型
                ok = keyType == 1
                        ? mc.authenticateSectorWithKeyA(sector, defaultKey)
                        : mc.authenticateSectorWithKeyB(sector, defaultKey);
            }
            if (!ok) return null;
            byte[] d = mc.readBlock(block);
            if (d == null || d.length < 16) return null;
            return d;
        } catch (Exception e) {
            log("读块 " + block + " 失败: " + e);
            return null;
        } finally {
            closeQuietly(mc);
        }
    }

    private boolean writeBlock(int block, byte[] data, int keyType, byte[] key) {
        if (data == null || data.length < 16) return false;
        MifareClassic mc = openClassic();
        if (mc == null) return false;
        try {
            int sector = sectorOfBlock(block);
            byte[] useKey = (key != null && key.length == 6) ? key : defaultKey;
            boolean ok = keyType == 1
                    ? mc.authenticateSectorWithKeyB(sector, useKey)
                    : mc.authenticateSectorWithKeyA(sector, useKey);
            if (!ok) {
                ok = keyType == 1
                        ? mc.authenticateSectorWithKeyA(sector, useKey)
                        : mc.authenticateSectorWithKeyB(sector, useKey);
            }
            if (!ok) {
                log("写块 " + block + " 认证失败");
                return false;
            }
            mc.writeBlock(block, slice(data, 0, 16));
            return true;
        } catch (Exception e) {
            log("写块 " + block + " 失败: " + e);
            return false;
        } finally {
            closeQuietly(mc);
        }
    }
}
