package net.nfcreader.core;

import java.util.ArrayList;
import java.util.List;

/**
 * 卡片级操作：读整卡、破解（字典）、整卡写回。
 *
 * 这些逻辑原本只存在于电脑端（Python 的 dump_card / crack），
 * Android 端之前只实现了「单块读写」这一层，所以手机上点「读取整卡」「破解」
 * 全都没反应。这里把上层业务补齐：密钥尝试、逐扇区推进、失败跳过、进度回调。
 */
public final class CardOps {

    /** 一张卡的完整快照。 */
    public static final class Dump {
        public String uid = "";
        public String atqa = "";
        public int sak = -1;
        public int typeId = 0;
        public String kind = "";
        public String mifareType = "";
        public int sectorCount = 0;
        public int blockCount = 0;
        public int sizeBytes = 0;
        public String[] blocks = new String[0];
        public boolean[] blockOk = new boolean[0];
        public String[] keyA = new String[0];
        public String[] keyB = new String[0];
        public int okCount = 0;
        public int failCount = 0;
        public String note = "";

        public String keyFor(int sector) {
            if (sector >= 0 && sector < keyA.length && keyA[sector] != null) return keyA[sector];
            if (sector >= 0 && sector < keyB.length && keyB[sector] != null) return keyB[sector];
            return null;
        }

        public int keyTypeFor(int sector) {
            if (sector >= 0 && sector < keyA.length && keyA[sector] != null) return 0;
            return 1;
        }

        public String blockText(int i) {
            if (i < 0 || i >= blocks.length) return "";
            return blocks[i] == null ? "" : blocks[i];
        }

        public String hexBlock(int i) {
            String s = blockText(i).replace(" ", "");
            return s.isEmpty() ? "00000000000000000000000000000000" : s;
        }
    }

    public interface Progress {
        void on(int done, int total, String line);
    }

    private CardOps() {
    }

    /** 默认密钥集合：出厂默认 + 内置字典 + 用户词表。 */
    public static List<byte[]> keyList(List<byte[]> extra) {
        List<byte[]> out = new ArrayList<>();
        out.add(Protocol.DEFAULT_KEY);
        for (byte[] k : KeyBank.defaults()) {
            if (k != null && k.length == 6) out.add(k);
        }
        if (extra != null) {
            for (byte[] k : extra) {
                if (k != null && k.length == 6) out.add(k);
            }
        }
        return out;
    }

    /**
     * 读整卡：逐扇区先试密钥认证，成功后读该扇区的全部块。
     * 认证不过的扇区记为失败并跳过（不会让整次读卡中断）。
     */
    public static Dump readAll(NfcEngine e, List<byte[]> extraKeys, Progress p) {
        Dump d = new Dump();
        if (e == null) {
            d.note = "读卡引擎未初始化";
            return d;
        }
        NfcEngine.CardInfo ci = e.readCardInfo();
        d.uid = ci.uid == null ? "" : ci.uid;
        d.atqa = ci.atqa == null ? "" : ci.atqa;
        d.sak = ci.sak;
        d.typeId = ci.typeId;
        d.kind = ci.className == null ? "" : ci.className;
        d.mifareType = ci.mifareType == null ? "" : ci.mifareType;
        d.sectorCount = Math.max(0, ci.sectorCount);
        d.blockCount = Math.max(0, ci.blockCount);
        d.sizeBytes = Math.max(0, ci.sizeBytes);
        if (!ci.present) {
            d.note = ci.note == null ? "没有检测到卡片" : ci.note;
            return d;
        }
        if (d.blockCount <= 0) {
            d.note = "无法获取卡片容量（不是 Mifare Classic / Ultralight？）";
            return d;
        }
        int sectors = d.sectorCount > 0 ? d.sectorCount : (d.blockCount + 3) / 4;
        d.sectorCount = sectors;
        d.blocks = new String[d.blockCount];
        d.blockOk = new boolean[d.blockCount];
        d.keyA = new String[sectors];
        d.keyB = new String[sectors];
        for (int i = 0; i < d.blockCount; i++) d.blocks[i] = "";

        List<byte[]> keys = mergeCloudKeys(d.uid, keyList(extraKeys), p);
        for (int sec = 0; sec < sectors; sec++) {
            int found = findSectorKey(e, sec, keys, d);
            if (found < 0) {
                int first = Protocol.firstBlockOfSector(sec);
                int cnt = Protocol.blocksOfSector(sec);
                for (int i = 0; i < cnt; i++) {
                    int b = first + i;
                    if (b < d.blockCount) {
                        d.blocks[b] = "";
                        d.blockOk[b] = false;
                        d.failCount++;
                    }
                }
                if (p != null) p.on(sec + 1, sectors, "扇区 " + sec + " 未找到密钥，跳过");
                continue;
            }
            int kt = found;
            byte[] key = kt == 0 ? Protocol.unhex(d.keyA[sec]) : Protocol.unhex(d.keyB[sec]);
            int first = Protocol.firstBlockOfSector(sec);
            int cnt = Protocol.blocksOfSector(sec);
            for (int i = 0; i < cnt; i++) {
                int b = first + i;
                if (b >= d.blockCount) break;
                byte[] data = e.readBlockData(b, kt, key);
                if (data != null) {
                    d.blocks[b] = Protocol.hexSpaced(data);
                    d.blockOk[b] = true;
                    d.okCount++;
                } else {
                    d.blocks[b] = "";
                    d.blockOk[b] = false;
                    d.failCount++;
                }
            }
            if (p != null) {
                p.on(sec + 1, sectors, "扇区 " + sec + " 完成（"
                        + (kt == 0 ? "KEY A" : "KEY B") + " " + d.keyFor(sec) + "）");
            }
        }
        if (p != null) p.on(sectors, sectors, "读卡结束：成功 " + d.okCount + " 块，失败 " + d.failCount + " 块");
        return d;
    }

    /**
     * 只破解密钥：逐扇区试 KEY A / KEY B，命中即记录。
     * 手机端没有随机数采集能力，所以这是「字典 + 常见密钥」路线。
     */
    public static Dump crack(NfcEngine e, List<byte[]> extraKeys, Progress p) {
        Dump d = new Dump();
        if (e == null) {
            d.note = "读卡引擎未初始化";
            return d;
        }
        NfcEngine.CardInfo ci = e.readCardInfo();
        d.uid = ci.uid == null ? "" : ci.uid;
        d.sak = ci.sak;
        d.typeId = ci.typeId;
        d.kind = ci.className == null ? "" : ci.className;
        d.mifareType = ci.mifareType == null ? "" : ci.mifareType;
        d.sectorCount = Math.max(0, ci.sectorCount);
        d.blockCount = Math.max(0, ci.blockCount);
        d.sizeBytes = Math.max(0, ci.sizeBytes);
        if (!ci.present) {
            d.note = ci.note == null ? "没有检测到卡片" : ci.note;
            return d;
        }
        int sectors = d.sectorCount > 0 ? d.sectorCount : (d.blockCount + 3) / 4;
        d.sectorCount = sectors;
        d.keyA = new String[sectors];
        d.keyB = new String[sectors];
        List<byte[]> keys = mergeCloudKeys(d.uid, keyList(extraKeys), p);
        for (int sec = 0; sec < sectors; sec++) {
            int kt = findSectorKey(e, sec, keys, d);
            if (kt >= 0) {
                d.okCount++;
                if (p != null) p.on(sec + 1, sectors, "扇区 " + sec + " 命中：" + d.keyFor(sec));
            } else {
                d.failCount++;
                if (p != null) p.on(sec + 1, sectors, "扇区 " + sec + " 全部密钥都不对");
            }
        }
        if (p != null) {
            p.on(sectors, sectors, "破解结束：命中 " + d.okCount + " / " + sectors + " 个扇区");
        }
        return d;
    }

    /**
     * 把官方云端密钥库的候选密钥排在最前面（命中率最高），再接内置字典。
     * 网络不可用 / 未收录时安静地退化成纯字典，不打断读卡。
     */
    private static List<byte[]> mergeCloudKeys(String uid, List<byte[]> base, Progress p) {
        List<byte[]> out = new ArrayList<>();
        try {
            for (byte[] k : CloudKeys.lookup(uid, p)) {
                if (k != null && k.length == 6) out.add(k);
            }
        } catch (Throwable ignore) {
        }
        if (p != null && !out.isEmpty()) p.on(0, 1, "云端候选密钥 " + out.size() + " 把，先逐个认证");
        for (byte[] k : base) {
            boolean dup = false;
            for (byte[] old : out) {
                if (java.util.Arrays.equals(old, k)) {
                    dup = true;
                    break;
                }
            }
            if (!dup) out.add(k);
        }
        return out;
    }

    /** 找到返回 0=KEY A / 1=KEY B，失败返回 -1。 */
    private static int findSectorKey(NfcEngine e, int sec, List<byte[]> keys, Dump d) {
        for (byte[] k : keys) {
            if (e.authSector(sec, 0, k)) {
                if (d != null && d.keyA != null && sec < d.keyA.length) {
                    d.keyA[sec] = Protocol.hex(k);
                }
                return 0;
            }
        }
        for (byte[] k : keys) {
            if (e.authSector(sec, 1, k)) {
                if (d != null && d.keyB != null && sec < d.keyB.length) {
                    d.keyB[sec] = Protocol.hex(k);
                }
                return 1;
            }
        }
        return -1;
    }

    /** 写一块（16 字节 hex）。 */
    public static boolean writeBlockHex(NfcEngine e, int block, String hex16, String keyHex, int keyType) {
        if (e == null || hex16 == null) return false;
        byte[] data;
        byte[] key;
        try {
            data = Protocol.fixed(Protocol.unhex(hex16), 16);
            key = keyHex == null || keyHex.trim().isEmpty()
                    ? Protocol.DEFAULT_KEY : Protocol.unhex(keyHex);
        } catch (Throwable t) {
            return false;
        }
        if (keyType == 1) {
            if (!e.authSector(Protocol.sectorOfBlock(block), 1, key)) return false;
        }
        return e.writeBlockData(block, data, key);
    }

    /**
     * 整卡写回：只写读出成功的块，默认跳过尾块（避免写坏访问位导致锁卡）。
     * 返回成功写入的块数。
     */
    public static int writeAll(NfcEngine e, Dump d, String keyHex, boolean skipTrailer, Progress p) {
        if (e == null || d == null || d.blocks == null) return 0;
        byte[] key;
        try {
            key = keyHex == null || keyHex.trim().isEmpty()
                    ? Protocol.DEFAULT_KEY : Protocol.unhex(keyHex);
        } catch (Throwable t) {
            return 0;
        }
        int n = 0;
        int total = d.blocks.length;
        for (int b = 0; b < total; b++) {
            if (!d.blockOk[b] || d.blocks[b].isEmpty()) continue;
            if (skipTrailer && Protocol.isTrailerBlock(b)) continue;
            byte[] data;
            try {
                data = Protocol.fixed(Protocol.unhex(d.blocks[b]), 16);
            } catch (Throwable t) {
                continue;
            }
            if (e.writeBlockData(b, data, key)) {
                n++;
                if (p != null) p.on(b + 1, total, "写入块 " + b + " 成功");
            } else {
                if (p != null) p.on(b + 1, total, "写入块 " + b + " 失败");
            }
        }
        return n;
    }

    /** 十六进制文本转字节（容错）。 */
    public static byte[] hex(String s) {
        try {
            return Protocol.unhex(s);
        } catch (Throwable t) {
            return null;
        }
    }
}
