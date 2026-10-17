package net.nfcreader.core;

import android.nfc.Tag;
import android.nfc.tech.MifareClassic;
import android.nfc.tech.MifareUltralight;
import android.nfc.tech.NfcA;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * NFC 协议引擎：把桌面端发来的私有协议命令翻译成 Android NFC API 调用。
 *
 * 相对旧版的修复（都是真 bug）：
 *  1. 【致命】旧版 readBlock 忽略传入密钥、硬编码 FF×6，导致非出厂密钥的卡片永远读不出。
 *     现在改成「先查密钥缓存 → 用缓存密钥认证 → 再试给定额钥」，认证结果按
 *     (扇区, 密钥类型) 缓存，桌面端先发 0x06 认证、再发 0x07 读块时可复用。
 *  2. 【致命】旧版每个操作都 open/close MifareClassic，认证态随连接丢失，极慢且经常失败。
 *     现在连接与认证态都跨命令保持，只有卡片变化或显式 RESET 才关闭。
 *  3. 【致命】statusPos 缺省值写反：桌面端表里只有 0x06/0x07/0x08/0x09/0x0A/0x0B/0x20
 *     的状态码在 index 2，其余（包括 0x17/0x23/0x24/0x26/0x27/0x38）都在 index 1。
 *     旧版缺省返回 2，导致这些命令的错误码被桌面端读成「成功」。
 *  4. 【致命】旧版 hasTag 用 8 秒超时且 Activity 8.5 秒后强置 null，整卡导出中途会失败。
 *     现在改为「每次成功操作刷新 30 秒空闲期」，且不再由 Activity 主动清空。
 *  5. 0x0C 写卡号/0x27 写 UID 使用统一的块 0 构造函数（含 BCC 计算），不再用未初始化变量。
 *  6. 新增 Mifare Ultralight / NTAG（7 字节 UID 卡）按页读写支持。
 */
public final class NfcEngine {

    /** 日志回调（可能来自任意线程）。 */
    public interface Log {
        void onLog(String line);
    }

    /** 卡片信息（本机工具界面用）。 */
    public static final class CardInfo {
        public boolean present = false;
        public String uid = "";
        public int sak = -1;
        public String atqa = "";
        public int typeId = 0;
        public String typeName = "未知";
        public String className = "";
        public String label = "";
        public String note = "";
        public int sectorCount = 0;
        public int blockCount = 0;
        public int sizeBytes = 0;
        public String techs = "";
        public String mifareType = "";
    }

    /** 空闲多久之后认为卡片可能已离开（仅用于 UI 提示，不影响实际读写）。 */
    private static final long IDLE_HINT_MS = 30000L;

    private final Log logger;
    private final Object lock = new Object();
    private final Map<Integer, byte[]> authedKeys = new HashMap<>();

    private volatile Tag currentTag;
    private volatile long lastOkMs = 0L;
    private volatile byte[] defaultKey = Protocol.DEFAULT_KEY.clone();

    private MifareClassic mc;
    private MifareUltralight mu;
    private int mcTagId = 0;
    private int muTagId = 0;

    /** 最近一次写入的卡号（5 字节），供 0x24 卡号校验使用。 */
    private volatile byte[] lastCardNo;

    public NfcEngine(Log log) {
        this.logger = log;
    }

    /** 内部日志出口（任意线程安全）。 */
    private void log(String line) {
        try {
            if (logger != null) logger.onLog(line);
        } catch (Throwable ignore) {
        }
    }

    /**
     * 读 SAK。MifareClassic 本身没有 getSak()，SAK 只能从 NfcA 技术对象取。
     * 取不到返回 -1（表示不是 ISO14443-A 卡）。
     */
    private int sakOf(Tag t) {
        if (t == null) return -1;
        try {
            NfcA na = NfcA.get(t);
            if (na != null) return na.getSak() & 0xFF;
        } catch (Throwable ignore) {
        }
        return -1;
    }

    // ------------------------------------------------------------ 卡片生命周期

    public void setTag(Tag t) {
        synchronized (lock) {
            if (t == null) {
                closeCardLocked();
                return;
            }
            Tag old = currentTag;
            if (old != null && sameTag(old, t)) {
                lastOkMs = System.currentTimeMillis();
                return;
            }
            closeCardLocked();
            currentTag = t;
            lastOkMs = System.currentTimeMillis();
            authedKeys.clear();
            log("检测到卡片：" + Protocol.hex(t.getId()));
        }
    }

    public Tag getTag() {
        return currentTag;
    }

    /** 卡片是否可能还在（30 秒内有成功操作）。 */
    public boolean hasTag() {
        Tag t = currentTag;
        if (t == null) return false;
        return System.currentTimeMillis() - lastOkMs < IDLE_HINT_MS;
    }

    public void closeCard() {
        synchronized (lock) {
            closeCardLocked();
        }
    }

    public void setDefaultKey(byte[] key) {
        if (key != null && key.length == 6) defaultKey = key.clone();
    }

    public byte[] getDefaultKey() {
        return defaultKey.clone();
    }

    private void closeCardLocked() {
        closeClassic();
        closeUltralight();
        currentTag = null;
        lastOkMs = 0L;
        authedKeys.clear();
    }

    private void closeClassic() {
        MifareClassic m = mc;
        mc = null;
        mcTagId = 0;
        if (m != null) {
            try {
                m.close();
            } catch (Throwable ignore) {
            }
        }
    }

    private void closeUltralight() {
        MifareUltralight u = mu;
        mu = null;
        muTagId = 0;
        if (u != null) {
            try {
                u.close();
            } catch (Throwable ignore) {
            }
        }
    }

    private static boolean sameTag(Tag a, Tag b) {
        if (a == b) return true;
        if (a == null || b == null) return false;
        byte[] ua = a.getId();
        byte[] ub = b.getId();
        return ua != null && ub != null && Arrays.equals(ua, ub);
    }

    // ------------------------------------------------------------ 连接管理

    /** 取得（必要时建立）Mifare Classic 连接；卡片不是 Classic 时返回 null。 */
    private MifareClassic classic() {
        Tag t = currentTag;
        if (t == null) return null;
        int id = System.identityHashCode(t);
        MifareClassic cur = mc;
        if (cur != null && mcTagId == id) {
            try {
                if (cur.isConnected()) return cur;
            } catch (Throwable ignore) {
            }
            closeClassic();
        } else if (cur != null) {
            closeClassic();
        }
        try {
            MifareClassic m = MifareClassic.get(t);
            if (m == null) return null;
            m.connect();
            mc = m;
            mcTagId = id;
            return m;
        } catch (Throwable e) {
            log("连接卡片失败：" + e.getMessage());
            return null;
        }
    }

    /** 取得（必要时建立）Mifare Ultralight 连接。 */
    private MifareUltralight ultralight() {
        Tag t = currentTag;
        if (t == null) return null;
        int id = System.identityHashCode(t);
        MifareUltralight cur = mu;
        if (cur != null && muTagId == id) {
            try {
                if (cur.isConnected()) return cur;
            } catch (Throwable ignore) {
            }
            closeUltralight();
        } else if (cur != null) {
            closeUltralight();
        }
        try {
            MifareUltralight u = MifareUltralight.get(t);
            if (u == null) return null;
            u.connect();
            mu = u;
            muTagId = id;
            return u;
        } catch (Throwable e) {
            log("连接卡片失败：" + e.getMessage());
            return null;
        }
    }

    // ------------------------------------------------------------ 认证

    private static int keyIndex(int sector, int keyType) {
        return (sector << 1) | (keyType & 1);
    }

    private boolean tryAuth(MifareClassic m, int sector, int keyType, byte[] key) {
        if (m == null || key == null || key.length != 6) return false;
        try {
            boolean ok = (keyType & 1) == 1
                    ? m.authenticateSectorWithKeyB(sector, key)
                    : m.authenticateSectorWithKeyA(sector, key);
            int idx = keyIndex(sector, keyType);
            if (ok) authedKeys.put(idx, key.clone());
            else authedKeys.remove(idx);
            return ok;
        } catch (Throwable t) {
            // 认证失败本身也会抛 IOException，所以这里不能当成「卡片离开」
            return false;
        }
    }

    /**
     * 确保某个扇区已认证。顺序：缓存命中 → 给定密钥 + 指定类型 → 给定密钥 + 另一类型
     * → 缓存密钥 → 出厂默认密钥。这样桌面端「先 0x06 认证、再 0x07 读块」的流程才能复用到密钥。
     */
    private boolean ensureAuthed(int sector, int keyType, byte[] key) {
        MifareClassic m = mc;
        if (m == null) return false;
        int k = keyType & 1;
        byte[] useKey = (key != null && key.length == 6) ? key : null;

        byte[] cachedSame = authedKeys.get(keyIndex(sector, k));
        // 没带密钥的读块请求（桌面端认证一次之后连续读该扇区）：只要这个扇区认证过就
        // 直接认为还有效。MifareClassic 的连接状态是按扇区保持的，不会因为读块失效。
        if (useKey == null &&
                (cachedSame != null || authedKeys.get(keyIndex(sector, 1 - k)) != null)) {
            markOk();
            return true;
        }
        if (useKey != null && cachedSame != null && Arrays.equals(cachedSame, useKey)) {
            markOk();
            return true;
        }
        if (useKey != null && tryAuth(m, sector, k, useKey)) {
            markOk();
            return true;
        }
        if (useKey != null && tryAuth(m, sector, 1 - k, useKey)) {
            markOk();
            return true;
        }
        byte[] cachedOther = authedKeys.get(keyIndex(sector, 1 - k));
        byte[] cachedPick = cachedSame != null ? cachedSame : cachedOther;
        if (cachedPick != null && tryAuth(m, sector, k, cachedPick)) {
            markOk();
            return true;
        }
        if (cachedOther != null && tryAuth(m, sector, 1 - k, cachedOther)) {
            markOk();
            return true;
        }
        if (tryAuth(m, sector, k, defaultKey)) {
            markOk();
            return true;
        }
        if (tryAuth(m, sector, 1 - k, defaultKey)) {
            markOk();
            return true;
        }
        return false;
    }

    private void markOk() {
        lastOkMs = System.currentTimeMillis();
    }

    // ------------------------------------------------------------ 协议分发

    /** 单一入口：接收命令 DATA，返回应答 DATA。线程安全。 */
    public byte[] dispatch(byte[] req) {
        if (req == null || req.length == 0) return Protocol.of(0x00, Protocol.E_RANGE);
        int cmd = req[0] & 0xFF;
        synchronized (lock) {
            try {
                switch (cmd) {
                    case Protocol.CMD_INFO:
                        return handleInfo();
                    case Protocol.CMD_STATE:
                        return handleState();
                    case Protocol.CMD_FIND_CARD:
                        return handleFindCard();
                    case Protocol.CMD_KEY_A_CHECK:
                        return handleKeyCheck(req, 0);
                    case Protocol.CMD_KEY_B_CHECK:
                        return handleKeyCheck(req, 1);
                    case Protocol.CMD_AUTH:
                        // 0x0A 在固件里是空命令，恒回 AAA 0 0
                        return Protocol.of(Protocol.CMD_AUTH, 0, 0);
                    case Protocol.CMD_READ_BLOCK:
                        return handleReadBlock(req);
                    case Protocol.CMD_WRITE_BLOCK:
                    case Protocol.CMD_WRITE_BLOCK_FAST:
                    case Protocol.CMD_WRITE_BLOCK_NORMAL:
                        return handleWriteBlock(cmd, req);
                    case Protocol.CMD_SCAN_CARD:
                        return handleScanCard();
                    case Protocol.CMD_WRITE_CARD:
                        return handleWriteCardNo(req);
                    case Protocol.CMD_WRITE_UID:
                        return handleWriteUid(req);
                    case Protocol.CMD_CHECK_CARD_NO:
                        return handleCheckCardNo();
                    case Protocol.CMD_AUTO_IDENTIFY:
                        // 0x38 单字节 = 自动识别卡片（与 staticnest2 同码，靠 DATA 长度区分）
                        if (req.length <= 1) return handleFindCard();
                        return fail(cmd, Protocol.E_RANGE);
                    case Protocol.CMD_RESET:
                        closeClassic();
                        closeUltralight();
                        authedKeys.clear();
                        log("已重置卡片连接");
                        return new byte[]{(byte) Protocol.CMD_RESET, 0};
                    default:
                        // 手机读卡器不具备的固件专属命令（破解/低频 ID/封卡等），
                        // 桌面端在 net 模式下本就有能力守卫，这里再兜一层。
                        return fail(cmd, Protocol.E_RANGE);
                }
            } catch (Throwable t) {
                log("命令 0x" + String.format("%02X", cmd) + " 执行失败：" + t);
                return fail(cmd, Protocol.E_NO_CARD);
            }
        }
    }

    /** 供 TCP 层在 dispatch 抛异常时合成一条形状正确的失败应答。 */
    public byte[] failReply(byte[] req) {
        int cmd = (req != null && req.length > 0) ? (req[0] & 0xFF) : 0;
        try {
            return fail(cmd, Protocol.E_NO_CARD);
        } catch (Throwable t) {
            return Protocol.of(cmd, Protocol.E_NO_CARD);
        }
    }

    private byte[] fail(int cmd, int code) {
        int pos = Protocol.statusPos(cmd);
        byte[] r = new byte[Math.max(pos + 1, 2)];
        r[0] = (byte) cmd;
        r[Math.min(pos, r.length - 1)] = (byte) code;
        return r;
    }

    // ------------------------------------------------------------ 各命令实现

    private byte[] handleInfo() {
        byte[] r = new byte[16];
        r[0] = (byte) Protocol.CMD_INFO;
        // 桌面端把 d[1..3] 显示成「主.次.修订」，这里回报真实的 App 版本 2.0.0
        r[1] = 0x02;
        r[2] = 0x00;
        r[3] = 0x00;
        byte[] sn = Protocol.unhex(DeviceInfo.snCacheForProtocol());
        byte[] sn12 = Protocol.fixed(sn, 12);
        System.arraycopy(sn12, 0, r, 4, 12);
        return r;
    }

    private byte[] handleState() {
        // 每次现读：只在启动时缓存一次会让桌面端一直显示「—」
        int bat = DeviceInfo.refreshBattery();
        if (bat < 0 || bat > 100) bat = 0;
        return Protocol.of(Protocol.CMD_STATE, bat);
    }

    private byte[] handleFindCard() {
        Tag t = currentTag;
        if (t == null) return Protocol.of(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
        byte[] uid = t.getId();
        if (uid == null || uid.length == 0) {
            return Protocol.of(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
        }
        int sak = -1;
        MifareClassic m = classic();
        if (m != null) {
            try {
                sak = sakOf(t);
            } catch (Throwable ignore) {
            }
        }
        if (sak < 0) {
            MifareUltralight u = ultralight();
            if (u == null) {
                // 连不上卡片 → 认为已经离开
                closeCardLocked();
                return Protocol.of(Protocol.CMD_FIND_CARD, Protocol.E_NO_CARD);
            }
            sak = 0x00;
        }
        boolean isUidCard = false;
        if (uid.length >= 7) {
            // 7 字节 UID 卡：桌面端按原序取 d[3..10]
            byte[] r = new byte[12];
            r[0] = (byte) Protocol.CMD_FIND_CARD;
            r[1] = 0x00;
            r[2] = 0x04;
            System.arraycopy(uid, 0, r, 3, 7);
            r[10] = (byte) (sak & 0xFF);
            r[11] = (byte) (isUidCard ? 1 : 0);
            markOk();
            return r;
        }
        // 4 字节 UID：桌面端会再反序一次，所以这里要按反序发
        byte[] r = new byte[9];
        r[0] = (byte) Protocol.CMD_FIND_CARD;
        r[1] = 0x00;
        r[2] = 0x01; // IC 卡
        r[3] = uid[3];
        r[4] = uid[2];
        r[5] = uid[1];
        r[6] = uid[0];
        r[7] = (byte) (sak & 0xFF);
        r[8] = (byte) (isUidCard ? 1 : 0);
        markOk();
        return r;
    }

    private byte[] handleKeyCheck(byte[] req, int keyType) {
        int cmd = keyType == 1 ? Protocol.CMD_KEY_B_CHECK : Protocol.CMD_KEY_A_CHECK;
        if (req.length < 8) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        byte[] key = Arrays.copyOfRange(req, 2, 8);
        MifareClassic m = classic();
        boolean ok = m != null && tryAuth(m, Protocol.sectorOfBlock(block), keyType, key);
        if (ok) markOk();
        return Protocol.of(cmd, block, ok ? 0 : Protocol.E_AUTH);
    }

    private byte[] handleReadBlock(byte[] req) {
        if (req.length < 2) return fail(Protocol.CMD_READ_BLOCK, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        int keyType = req.length >= 3 ? (req[2] & 1) : 0;
        byte[] key = req.length >= 9 ? Arrays.copyOfRange(req, 3, 9) : null;
        byte[] data = readBlockData(block, keyType, key);
        if (data == null) return fail(Protocol.CMD_READ_BLOCK, Protocol.E_READ);
        byte[] r = new byte[18];
        r[0] = (byte) Protocol.CMD_READ_BLOCK;
        r[1] = 0x00;
        System.arraycopy(Protocol.fixed(data, 16), 0, r, 2, 16);
        return r;
    }

    /**
     * 写块：只接受协议里明确的三种布局，**绝不再按偏移"猜"**。
     *
     * 旧实现按长度依次试 D-K16 / K16-D / keyed / data-only 四种切法，
     * 只要某一种切法恰好认证成功就把**错位的数据**写进卡里 ——
     * 实测正是「写卡提示成功、卡片却刷不了」的根因（默认密钥卡尤其容易中）。
     *
     *   0x16 极速档：  DATA = cmd, 块号, 数据16
     *   0x18 普通档：  DATA = cmd, 块号, 0x00, 数据16
     *   0x08 带密钥类型：DATA = cmd, 块号, 密钥类型, 数据16 [, 密钥6]
     */
    private byte[] handleWriteBlock(int cmd, byte[] req) {
        if (req == null || req.length < 2) return fail(cmd, Protocol.E_RANGE);
        int block = req[1] & 0xFF;
        byte[] data;
        byte[] key = null;
        int keyType = 0;

        if (cmd == Protocol.CMD_WRITE_BLOCK_FAST) {
            if (req.length != 18) return fail(cmd, Protocol.E_RANGE);
            data = Arrays.copyOfRange(req, 2, 18);
        } else if (cmd == Protocol.CMD_WRITE_BLOCK_NORMAL) {
            if (req.length != 19) return fail(cmd, Protocol.E_RANGE);
            data = Arrays.copyOfRange(req, 3, 19);
        } else {
            if (req.length == 19) {
                keyType = req[2] & 1;
                data = Arrays.copyOfRange(req, 3, 19);
            } else if (req.length == 25) {
                keyType = req[2] & 1;
                data = Arrays.copyOfRange(req, 3, 19);
                key = Arrays.copyOfRange(req, 19, 25);
            } else {
                return fail(cmd, Protocol.E_RANGE);
            }
        }
        boolean ok = writeBlockData(block, data, key, keyType);
        return writeResult(cmd, block, ok ? 0 : Protocol.E_WRITE);
    }

    private byte[] handleScanCard() {
        // 全卡扫描由桌面端逐块读完成，这里只回一个空成功
        return Protocol.of(Protocol.CMD_SCAN_CARD, 0x00);
    }

    /** 0x0C 写卡号：取 5 字节卡号的前 4 字节做 UID，重算 BCC 写块 0。 */
    private byte[] handleWriteCardNo(byte[] req) {
        if (req.length < 6) return fail(Protocol.CMD_WRITE_CARD, Protocol.E_RANGE);
        byte[] cardNo = Arrays.copyOfRange(req, 1, 6);
        return writeCardNoInternal(cardNo);
    }

    /** 0x27 写 UID：DATA = [0x27, 0x00, uid6]。 */
    private byte[] handleWriteUid(byte[] req) {
        if (req.length < 8) return fail(Protocol.CMD_WRITE_UID, Protocol.E_RANGE);
        byte[] uid6 = Arrays.copyOfRange(req, 2, 8);
        byte[] uid4 = Arrays.copyOfRange(uid6, 0, 4);
        byte[] b0 = Protocol.buildBlockZero(uid4, 0x08);
        byte[] payload = new byte[6];
        payload[0] = (byte) Protocol.CMD_WRITE_UID;
        boolean ok = writeBlockData(0, b0, authedKeyOrNull(0));
        payload[1] = (byte) (ok ? 0 : Protocol.E_UID);
        lastCardNo = cardNoFromUid(uid4);
        return payload;
    }

    private byte[] writeCardNoInternal(byte[] cardNo5) {
        byte[] uid4 = Arrays.copyOfRange(cardNo5, 0, 4);
        byte[] b0 = Protocol.buildBlockZero(uid4, 0x08);
        boolean ok = writeBlockData(0, b0, authedKeyOrNull(0));
        byte[] r = new byte[2];
        r[0] = (byte) Protocol.CMD_WRITE_CARD;
        r[1] = (byte) (ok ? 0 : Protocol.E_WRITE);
        if (ok) lastCardNo = cardNoFromUid(uid4);
        return r;
    }

    private static byte[] cardNoFromUid(byte[] uid4) {
        byte[] c = new byte[5];
        System.arraycopy(uid4, 0, c, 0, Math.min(4, uid4.length));
        c[4] = (byte) Protocol.bcc(uid4);
        return c;
    }

    private byte[] authedKeyOrNull(int block) {
        int sector = Protocol.sectorOfBlock(block);
        byte[] a = authedKeys.get(keyIndex(sector, 0));
        if (a == null) a = authedKeys.get(keyIndex(sector, 1));
        return a == null ? null : a.clone();
    }

    private byte[] writeResult(int cmd, int block, int status) {
        return Protocol.of(cmd, block, status);
    }

    /**
     * 0x24 卡号校验：与最近一次写入的卡号比对。
     * 桌面端解析 status==1 通过 / 2 未知 / 7 卡片不符。
     */
    private byte[] handleCheckCardNo() {
        Tag t = currentTag;
        if (t == null) return Protocol.of(Protocol.CMD_CHECK_CARD_NO, 7);
        byte[] expect = lastCardNo;
        if (expect == null) return Protocol.of(Protocol.CMD_CHECK_CARD_NO, 2);
        byte[] uid = t.getId();
        if (uid == null || uid.length < 4) return Protocol.of(Protocol.CMD_CHECK_CARD_NO, 2);
        boolean same = uid[0] == expect[0] && uid[1] == expect[1]
                && uid[2] == expect[2] && uid[3] == expect[3];
        return Protocol.of(Protocol.CMD_CHECK_CARD_NO, same ? 1 : 7);
    }

    // ------------------------------------------------------------ 底层读写

    /** 读一块，返回 16 字节；失败返回 null。 */
    public byte[] readBlockData(int block, int keyType, byte[] key) {
        if (block < 0) return null;
        MifareClassic m = classic();
        if (m != null) {
            if (!ensureAuthed(Protocol.sectorOfBlock(block), keyType, key)) return null;
            try {
                byte[] d = m.readBlock(block);
                markOk();
                return Protocol.fixed(d, 16);
            } catch (Throwable e) {
                log("读块 " + block + " 失败：" + e.getMessage());
                return null;
            }
        }
        MifareUltralight u = ultralight();
        if (u != null) {
            try {
                byte[] d = u.readPages(block * 4);
                markOk();
                return Protocol.fixed(d, 16);
            } catch (Throwable e) {
                log("读页 " + (block * 4) + " 失败：" + e.getMessage());
                return null;
            }
        }
        return null;
    }

    /** 单参数版本（本机工具用），默认 KEY A。 */
    public byte[] readBlockData(int block, byte[] key) {
        return readBlockData(block, 0, key);
    }

    /** 写一块（16 字节）；失败返回 false。 */
    public boolean writeBlockData(int block, byte[] data, byte[] key) {
        if (block < 0) return false;
        byte[] d16 = Protocol.fixed(data, 16);
        MifareClassic m = classic();
        if (m != null) {
            if (!ensureAuthed(Protocol.sectorOfBlock(block), 0, key)) {
                log("写块 " + block + " 失败：认证未通过");
                return false;
            }
            try {
                m.writeBlock(block, d16);
                markOk();
                return true;
            } catch (Throwable e) {
                log("写块 " + block + " 失败：" + e.getMessage());
                return false;
            }
        }
        MifareUltralight u = ultralight();
        if (u != null) {
            try {
                int page = block * 4;
                for (int i = 0; i < 4; i++) {
                    u.writePage(page + i, new byte[]{
                            d16[i * 4], d16[i * 4 + 1], d16[i * 4 + 2], d16[i * 4 + 3]
                    });
                }
                markOk();
                return true;
            } catch (Throwable e) {
                log("写页失败：" + e.getMessage());
                return false;
            }
        }
        return false;
    }

    /** 写一块，可指定 KEY A / KEY B。 */
    public boolean writeBlockData(int block, byte[] data, byte[] key, int keyType) {
        if (block < 0) return false;
        byte[] d16 = Protocol.fixed(data, 16);
        MifareClassic m = classic();
        if (m != null) {
            if (!ensureAuthed(Protocol.sectorOfBlock(block), keyType & 1, key)) {
                log("写块 " + block + " 失败：密钥认证未通过");
                return false;
            }
            try {
                m.writeBlock(block, d16);
                markOk();
                return true;
            } catch (Throwable e) {
                log("写块 " + block + " 失败：" + e.getMessage());
                return false;
            }
        }
        return writeBlockData(block, data, key);
    }

    /** 显式认证某扇区（本机工具用）。 */
    public boolean authSector(int sector, int keyType, byte[] key) {
        MifareClassic m = classic();
        if (m == null) return false;
        boolean ok = tryAuth(m, sector, keyType, key);
        if (ok) markOk();
        return ok;
    }

    /**
     * 逐块读取某个扇区（本机工具用）。
     * 返回每个块一行格式化文本；读失败返回 null。
     */
    public List<String> readSectorLines(int sector, byte[] key) {
        List<String> out = new ArrayList<>();
        MifareClassic m = classic();
        if (m == null) return null;
        if (!ensureAuthed(sector, 0, key)) return null;
        int first = Protocol.firstBlockOfSector(sector);
        int count = Protocol.blocksOfSector(sector);
        for (int i = 0; i < count; i++) {
            int block = first + i;
            try {
                byte[] d = m.readBlock(block);
                String tag = Protocol.isTrailerBlock(block) ? "（尾块·密钥/控制位）" : "";
                out.add(String.format("块 %-3d  %s %s", block, Protocol.hexSpaced(d), tag));
                markOk();
            } catch (Throwable e) {
                out.add("块 " + block + "  读取失败：" + e.getMessage());
                break;
            }
        }
        return out;
    }

    /** 读卡信息（本机工具用）。 */
    public CardInfo readCardInfo() {
        CardInfo ci = new CardInfo();
        Tag t = currentTag;
        if (t == null) {
            ci.note = "没有检测到卡片，请把卡贴到手机背面 NFC 区域";
            return ci;
        }
        ci.present = true;
        byte[] uid = t.getId();
        ci.uid = Protocol.hex(uid);
        ci.techs = joinTechs(t.getTechList());

        if (mc == null) {
            byte[] atqa = readAtqa(t);
            if (atqa != null) ci.atqa = Protocol.hex(atqa);
        }

        MifareClassic m = classic();
        if (m != null) {
            try {
                ci.sak = sakOf(t);
                ci.sectorCount = m.getSectorCount();
                ci.blockCount = m.getBlockCount();
                ci.sizeBytes = m.getSize();
                int mt = m.getType();
                if (mt == MifareClassic.TYPE_CLASSIC) ci.mifareType = "Mifare Classic";
                else if (mt == MifareClassic.TYPE_PLUS) ci.mifareType = "Mifare Plus";
                else if (mt == MifareClassic.TYPE_PRO) ci.mifareType = "Mifare Pro";
                else ci.mifareType = "未知";
            } catch (Throwable e) {
                ci.note = "读取卡片参数失败：" + e.getMessage();
            }
            ci.typeId = uid != null && uid.length >= 7 ? 4 : 1;
        } else {
            MifareUltralight u = ultralight();
            if (u != null) {
                ci.typeId = uid != null && uid.length >= 7 ? 4 : 1;
                ci.mifareType = "Mifare Ultralight / NTAG";
                ci.blockCount = 16;
                ci.sectorCount = 4;
                ci.sizeBytes = 64;
            } else {
                ci.typeId = uid != null && uid.length >= 7 ? 4 : 2;
                ci.mifareType = "非 Mifare 卡";
            }
        }
        String[] cls = Protocol.classify(ci.typeId, ci.sak);
        ci.className = cls[0];
        ci.label = cls[1];
        ci.note = ci.note.isEmpty() ? cls[2] : ci.note;
        if (ci.typeName.equals("未知")) {
            ci.typeName = Protocol.CARD_TYPE[Math.min(ci.typeId, Protocol.CARD_TYPE.length - 1)];
        }
        markOk();
        return ci;
    }

    private byte[] readAtqa(Tag t) {
        try {
            NfcA na = NfcA.get(t);
            if (na == null) return null;
            na.connect();
            byte[] a = na.getAtqa();
            try {
                na.close();
            } catch (Throwable ignore) {
            }
            return a;
        } catch (Throwable e) {
            return null;
        }
    }

    private static String joinTechs(String[] techs) {
        if (techs == null || techs.length == 0) return "";
        StringBuilder sb = new StringBuilder();
        for (String s : techs) {
            if (s == null) continue;
            int i = s.lastIndexOf('.');
            String n = i >= 0 ? s.substring(i + 1) : s;
            if (sb.length() > 0) sb.append(" · ");
            sb.append(n);
        }
        return sb.toString();
    }

    /** 卡片是否还物理在位（会真的发一次寻卡指令）。 */
    public boolean probePresence() {
        Tag t = currentTag;
        if (t == null) return false;
        try {
            MifareClassic m = classic();
            if (m != null) {
                // 读块 0，成功就说明卡还在
                if (ensureAuthed(0, 0, null)) {
                    m.readBlock(0);
                    markOk();
                    return true;
                }
                return m.isConnected();
            }
            MifareUltralight u = ultralight();
            if (u != null) {
                u.readPages(0);
                markOk();
                return true;
            }
        } catch (Throwable ignore) {
        }
        return false;
    }
}
