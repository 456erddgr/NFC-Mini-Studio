package net.nfcreader.core;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;

/**
 * 常见 MIFARE Classic 密钥库。
 *
 * 手机跑不了 mfkey32 那种随机数破解（需要读卡器抓 nonce 对），
 * 所以本机破解走的是「字典 + 常见密钥」路线：
 * 先试出厂默认，再试国内常见的门禁/电梯/水卡密钥，命中率能覆盖一大半场景。
 */
public final class KeyBank {

    private KeyBank() {
    }

    /** 出厂 / 极常见。 */
    private static final String[] COMMON = {
            "FFFFFFFFFFFF",
            "000000000000",
            "A0A1A2A3A4A5",
            "D3F7D3F7D3F7",
            "AABBCCDDEEFF",
            "B0B1B2B3B4B5",
            "4D3A99C351DD",
            "1A982C7E459A",
            "714C5C886E97",
            "587EE5F9350F",
            "A0478CC39091",
            "533CB6C723F6",
            "8FD0A4F256E9",
            "000000000001",
            "1234567890AB",
            "123456789012",
            "ABCDEF123456",
            "001122334455",
            "A1B2C3D4E5F6",
            "112233445566",
            "F1E2D3C4B5A6",
            "K" // 占位，下面过滤
    };

    /** 国内门禁 / 电梯 / 水卡常见。 */
    private static final String[] CN = {
            "9A1A5C0A1A1A",
            "4A4B4C4D4E4F",
            "A0A1A2A3A4A6",
            "6A6B6C6D6E6F",
            "D0D1D2D3D4D5",
            "E0E1E2E3E4E5",
            "F0F1F2F3F4F5",
            "C0C1C2C3C4C5",
            "B8B9BABBBCBD",
            "5A5B5C5D5E5F",
            "DAEDBEEFD00D",
            "CAFEBABE0000",
            "314159265358",
            "271828182845",
            "161803398874",
            "999999999999",
            "555555555555",
            "AAAAAAAAAAAA",
            "BBBBBBBBBBBB",
            "123456789ABC"
    };

    /** 返回去重后的默认密钥列表（顺序即尝试顺序）。 */
    public static List<byte[]> defaults() {
        LinkedHashSet<String> set = new LinkedHashSet<>();
        for (String k : COMMON) set.add(k);
        for (String k : CN) set.add(k);
        List<byte[]> out = new ArrayList<>();
        for (String hex : set) {
            try {
                out.add(Protocol.unhex(hex));
            } catch (Throwable ignore) {
            }
        }
        return out;
    }

    /** 把用户输入的多行文本解析成密钥列表（每行一个 12 位十六进制，允许空格冒号）。 */
    public static List<byte[]> parse(String text) {
        List<byte[]> out = new ArrayList<>();
        if (text == null) return out;
        for (String line : text.split("[\r\n]+")) {
            String hex = line.replaceAll("[^0-9a-fA-F]", "");
            if (hex.length() != 12) continue;
            try {
                out.add(Protocol.unhex(hex));
            } catch (Throwable ignore) {
            }
        }
        return out;
    }
}
