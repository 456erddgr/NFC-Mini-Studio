# -*- coding: utf-8 -*-
"""
NFC Mini 2.0 私有 BLE 协议层
=============================
帧格式（逆向自微信小程序 wxc3dd33d9d4ae0fd6）：

    FF | LEN | MSGID | DATA(LEN 字节) | SUM | FE

    * LEN    : DATA 区字节数（1..255），帧总长 = LEN + 5
    * MSGID  : 主机 1..253 自增分配，设备响应原样回显
    * SUM    : DATA 逐字节累加 & 0xFF
    * DATA[0]: 命令码

传输通道：
    Service 0xFFE0 / 主机写 0xFFE2 / 设备通知 0xFFE1
"""
from __future__ import annotations

from typing import List, Optional, Tuple

FRAME_HEAD = 0xFF
FRAME_TAIL = 0xFE
MSGID_MIN = 1
MSGID_MAX = 253


# ------------------------------------------------------------------ 异常
class NfcError(Exception):
    """设备返回非 0 状态码，或协议层异常。"""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ 命令码
class CMD:
    INFO = 0x01          # 设备信息（版本 + SN）
    STATE = 0x02         # 设备状态
    KEY_A_CHECK = 0x06   # 校验/写入 KEY A（DATA = cmd, 块号, 密钥6）
    READ_BLOCK = 0x07    # 读块
    WRITE_BLOCK = 0x08   # 写块（DATA = cmd, 块号, 密钥类型, 数据16）
    WRITE_BLOCK_KEY = 0x08   # 别名：写数据块（带密钥类型），小程序 card-write 页用
    KEY_B_CHECK = 0x09   # 校验/写入 KEY B
    AUTH = 0x0A          # 密钥认证
    SCAN_CARD = 0x0B     # 全卡扫描
    WRITE_CARD = 0x0C    # 写卡号（DATA = cmd + 5 字节卡号）
    FIND_CARD = 0x11     # 寻卡
    WRITE_BLOCK_16 = 0x16   # 写块 · 极速档（DATA = cmd, 块号, 数据16）
    WRITE_BLOCK_17 = 0x17   # 单字节：UFUID / 类魔术卡封卡（不可逆，切勿当写块用）
    UFUID_LOCK = 0x17       # 别名：封卡（与 WRITE_BLOCK_17 同码）
    WRITE_BLOCK_18 = 0x18   # 写块 · 普通档（DATA = cmd, 块号, 0x00, 数据16）
    HARDNESTED = 0x20    # hardnested 破解
    FULL_DECODE = 0x22   # 全卡解码
    LOCK_CARD = 0x23     # 单字节：锁卡（不可逆）
    CHECK_CARD_NO = 0x24    # 单字节：卡号校验，返回 1=校验通过 / 2=未知 / 7=卡片不符
    WRITE_BLOCK_24 = 0x24   # 旧名保留：历史上被误标为「写块·延迟档」，实为卡号校验
    IS_BUG = 0x25        # 检测魔术卡/漏洞卡
    READ_ID_CARD = 0x26  # 读 ID 卡 / 低频卡（返回卡号 + 频率档位）
    WRITE_UID = 0x27     # 写 UID / 生成手机手环 NFC 转接卡（DATA = cmd, 0x00, UID6）
    HALF_DECODE = 0x30   # half_decode 破解
    STATICNEST = 0x32    # staticnested 破解
    FM11RF08S = 0x37     # FM11RF08S 后门
    STATICNEST2 = 0x38   # staticnested 破解（变体，DATA = cmd, 块, ?, KEY A idx, KEY B idx）
    AUTO_IDENTIFY = 0x38    # 单字节：自动识别卡片（与 STATICNEST2 同码，靠 DATA 长度区分）
    RESET = 0x7F         # 复位


CMD_NAME = {
    CMD.INFO: "读设备信息",
    CMD.STATE: "读设备状态",
    CMD.KEY_A_CHECK: "校验 KEY A",
    CMD.READ_BLOCK: "读块",
    CMD.WRITE_BLOCK: "写块",
    CMD.KEY_B_CHECK: "校验 KEY B",
    CMD.AUTH: "密钥认证",
    CMD.SCAN_CARD: "全卡扫描",
    CMD.WRITE_CARD: "写卡",
    CMD.FIND_CARD: "寻卡",
    CMD.WRITE_BLOCK_16: "写块·极速档",
    CMD.WRITE_BLOCK_17: "封卡(UFUID/类魔术卡)",
    CMD.WRITE_BLOCK_18: "写块·普通档",
    CMD.LOCK_CARD: "锁卡",
    CMD.CHECK_CARD_NO: "卡号校验",
    CMD.HARDNESTED: "hardnested 破解",
    CMD.FULL_DECODE: "全卡解码",
    CMD.READ_ID_CARD: "读 ID 卡",
    CMD.IS_BUG: "漏洞卡检测",
    CMD.WRITE_UID: "写 UID / 生成转接卡",
    CMD.HALF_DECODE: "half_decode 破解",
    CMD.STATICNEST: "staticnested 破解",
    CMD.FM11RF08S: "FM11RF08S 后门",
    CMD.STATICNEST2: "自动识别卡片 / staticnested 变体",
    CMD.RESET: "复位",
}


# 响应状态字节位置表（逆向自小程序 dispatch 表：cmd -> 状态码在 DATA 中的下标）
# 依据 app-service.js：{"0x05":1,"0x11":1,"0x06":2,"0x07":2,"0x08":2,"0x09":2,
#   "0x0b":2,"0x21":1,"0x25":1,"0x30":1,"0x22":1,"0x20":2,"0x32":1,
#   "0x34":1,"0x35":1,"0x36":1,"0x37":1}
STATUS_INDEX = {
    0x05: 1, 0x11: 1, 0x06: 2, 0x07: 2, 0x08: 2, 0x09: 2, 0x0A: 2, 0x0B: 2,
    0x21: 1, 0x25: 1, 0x30: 1, 0x22: 1, 0x20: 2, 0x32: 1,
    0x34: 1, 0x35: 1, 0x36: 1, 0x37: 1,
}


def status_index(cmd: int) -> int:
    """某命令的响应状态字节下标（缺省 1）。"""
    return STATUS_INDEX.get(int(cmd) & 0xFF, 1)


# ------------------------------------------------------------------ 错误码
ERR = {
    0: "成功",
    1: "错误 Code:1",
    2: "指令长度错误 Code:2",
    3: "指令 CRC 校验错误 Code:3",
    4: "固件升级地址错误 Code:4",
    5: "固件升级扇区擦除失败 Code:5",
    6: "固件升级写入失败 Code:6",
    7: "IC 寻卡失败 Code:7",
    8: "密钥认证失败 Code:8",
    9: "IC 读卡失败 Code:9",
    10: "IC 写卡失败 Code:10",
    11: "写 UID 卡失败 Code:11",
    12: "写 GTU 卡失败 Code:12",
    13: "写 IC 卡控制段错误 Code:13",
    14: "写 IC 卡厂商数据段错误 Code:14",
    15: "参数超出范围 Code:15",
    16: "采集随机数失败 Code:16",
    17: "检测不到卡片，请重新摆放后再试",
}


def err_text(code: int) -> str:
    return ERR.get(code, f"未知状态码 {code}")


CARD_TYPE = {
    0: "未知",
    1: "IC 卡 (Mifare Classic)",
    2: "UID 卡",
    3: "ID 卡",
    4: "7 字节 UID 卡",
}

KEY_TYPE = {0: "KEY A", 1: "KEY B"}

DEFAULT_KEY = bytes(b"\xFF" * 6)


# ------------------------------------------------------------------ 帧编解码
def build_frame(data: bytes, msgid: int) -> bytes:
    """把 DATA 区封装成完整帧。"""
    if not 1 <= len(data) <= 255:
        raise NfcError("DATA 长度必须为 1..255 字节")
    if not MSGID_MIN <= msgid <= MSGID_MAX:
        raise NfcError("MSGID 必须为 1..253")
    checksum = sum(data) & 0xFF
    return bytes([FRAME_HEAD, len(data), msgid]) + data + bytes([checksum, FRAME_TAIL])


class FrameParser:
    """流式帧解析器：喂入任意分片的 BLE 通知数据，吐出完整 DATA 区。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._need = 0
        self.noise = 0

    def feed(self, chunk: bytes) -> List[Tuple[int, bytes]]:
        """返回 [(msgid, data), ...]，data 为校验通过的 DATA 区。"""
        out: List[Tuple[int, bytes]] = []
        self._buf += chunk

        while True:
            if self._need == 0:
                if len(self._buf) < 5:
                    break
                if self._buf[0] != FRAME_HEAD:
                    self._buf.pop(0)
                    self.noise += 1
                    continue
                self._need = self._buf[1] + 5
            if len(self._buf) < self._need:
                break

            pkt = bytes(self._buf[: self._need])
            del self._buf[: self._need]
            self._need = 0

            if pkt[-1] != FRAME_TAIL:
                self.noise += 1
                continue
            ln, mid = pkt[1], pkt[2]
            payload, checksum = pkt[3:3 + ln], pkt[3 + ln]
            if sum(payload) & 0xFF != checksum:
                self.noise += 1
                continue
            out.append((mid, payload))

        return out

    def reset(self) -> None:
        self._buf.clear()
        self._need = 0


# ------------------------------------------------------------------ 工具
def uid_reverse(raw: bytes) -> str:
    """设备返回的 UID 为反序 4 字节，还原成正序大写 hex。"""
    return bytes(raw)[::-1].hex().upper()


def hex_to_bytes(text: str) -> bytes:
    """容忍空格 / 冒号 / 连字符的 hex 串转 bytes。"""
    clean = text.replace(" ", "").replace(":", "").replace("-", "").replace("\n", "").strip()
    if not clean:
        raise NfcError("Hex 内容为空")
    if len(clean) % 2:
        raise NfcError("Hex 长度必须为偶数")
    try:
        return bytes.fromhex(clean)
    except ValueError as exc:
        raise NfcError(f"非法的 Hex 内容: {text}") from exc


def fmt_hex(data: bytes, sep: str = " ", upper: bool = True) -> str:
    s = bytes(data).hex(sep)
    return s.upper() if upper else s


# ================================================================
# v2.0.0 新增：卡片类型判定 + 写块候选帧格式
# ================================================================

CARD_CLASSES = {
    # class: (中文名, 频段, 是否扇区卡, 是否可破解, 是否可写)
    "IC": ("IC 卡 · Mifare Classic 扇区卡", "13.56 MHz", True, True, True),
    "UID": ("UID 卡 · 可改写 UID 的兼容卡", "13.56 MHz", True, True, True),
    "ID": ("ID 卡 · EM4100/EM4200 只读卡", "125 kHz", False, False, False),
    "UID7": ("7 字节 UID 卡 · Ultralight/NTAG 页式卡", "13.56 MHz", False, False, True),
    "UNKNOWN": ("未知卡片", "未知", False, False, False),
}

# 固件 CARD_TYPE 表（1..4），与 README 协议表一致
FIRMWARE_CARD_TYPES = {1: "IC", 2: "UID", 3: "ID", 4: "UID7"}

# SAK 兜底推断：固件没给出 type 时靠 SAK + UID 长度自行判断
IC_SAK = {0x08, 0x18, 0x09, 0x28, 0x88, 0x98}


def classify_card(card_type_id: int = 0, uid: str = "", sak: int = 0) -> dict:
    """把固件返回的卡类型 / SAK / UID 规整成统一结构。

    判据优先级：固件 CARD_TYPE 字段 > SAK/UID 长度推断。
    两条路都拿不到依据时如实返回 UNKNOWN，绝不猜。
    """
    cls = FIRMWARE_CARD_TYPES.get(int(card_type_id or 0), "")
    source = "固件 CARD_TYPE"
    if not cls:
        source = "SAK/UID 推断"
        uid_len = len((uid or "").replace(" ", "")) // 2
        if uid_len == 7:
            cls = "UID7"
        elif uid_len in (4, 10) and int(sak or 0) in IC_SAK:
            cls = "IC"
        else:
            cls = "UNKNOWN"

    label, band, sector_card, crackable, writable = CARD_CLASSES.get(
        cls, CARD_CLASSES["UNKNOWN"])

    if cls == "ID":
        note = ("125 kHz 卡不受 13.56 MHz 读写芯片支持：本机能报告它的存在与 UID，"
                "但读扇区 / 写块 / 密钥破解对它均不适用（需另配 125 kHz 模块）。")
    elif cls == "UID7":
        note = ("7 字节 UID 页式卡（Ultralight/NTAG）：可按页读写，"
                "但没有扇区密钥体系，因此不进入 KEY A/B 破解级联。")
    elif cls in ("IC", "UID"):
        note = "扇区卡：寻卡/读卡的同时后台自动级联破解各扇区密钥，密钥用于后续写块认证。"
    else:
        note = "未能判定卡片类型，不做任何猜测，仅展示固件原始返回值。"

    return {
        "class": cls,
        "label": label,
        "band": band,
        "sector_card": sector_card,
        "crackable": crackable,
        "writable": writable,
        "source": source,
        "note": note,
    }


# ---- 写块帧候选格式 ----------------------------------------------------
# 背景：v1.0.0 只有一个硬编码猜测（cmd=0x08 + 块号 + 密钥类型 + 密钥 + 数据），
# 若固件实际按 (块号, 密钥类型, 数据16, 密钥6) 解析，就会因"数据长度不等于 16"直接报错，
# 症状正是「读卡正常、写卡必失败」。v2.0.0 改为按顺序自适应试格式：
# 每种格式先做密钥认证（KEY A 失败自动换 KEY B），写完立刻回读比对校验；
# 命中后把格式 id 记进会话缓存，后续写块直接用命中格式，不再逐个重试。
WRITE_FORMATS = [
    {"id": "D-K16", "cmd": 0x08, "layout": "data_then_key", "pre_auth": True,
     "desc": "认证后 0x08(块, 密钥类型, 数据16, 密钥6)"},
    {"id": "K16-D", "cmd": 0x08, "layout": "key_then_data", "pre_auth": True,
     "desc": "认证后 0x08(块, 密钥类型, 密钥6, 数据16)"},
    {"id": "D-ONLY", "cmd": 0x08, "layout": "data_only", "pre_auth": True,
     "desc": "认证后 0x08(块, 密钥类型, 数据16)，密钥由认证会话承载"},
    {"id": "D-K16-NA", "cmd": 0x08, "layout": "data_then_key", "pre_auth": False,
     "desc": "不预认证 0x08(块, 密钥类型, 数据16, 密钥6)"},
    {"id": "K16-D-NA", "cmd": 0x08, "layout": "key_then_data", "pre_auth": False,
     "desc": "不预认证 0x08(块, 密钥类型, 密钥6, 数据16)"},
    {"id": "D-K16-16", "cmd": 0x16, "layout": "data_then_key", "pre_auth": True,
     "desc": "认证后 0x16(块, 密钥类型, 数据16, 密钥6)（写块备用指令）"},
    {"id": "D-K16-18", "cmd": 0x18, "layout": "data_then_key", "pre_auth": True,
     "desc": "认证后 0x18(块, 密钥类型, 数据16, 密钥6)（写块备用指令）"},
]
# 注意：0x17 已从候选写块格式中移除。小程序语义下 0x17 是单字节「封卡(UFUID/类魔术卡)」，
# 一旦误发即不可逆，不能再作为写块的兜底试探格式。0x24 同理是单字节卡号校验，非写块。


def build_write_payload(fmt: dict, block: int, key_type: int,
                        key: bytes, data: bytes) -> list:
    """按候选格式拼出写块命令的 DATA 段。"""
    data = bytes(data)[:16].ljust(16, b"\x00")
    key = bytes(key)[:6].ljust(6, b"\x00")
    layout = fmt.get("layout", "data_then_key")
    head = [int(block) & 0xFF, int(key_type) & 0xFF]
    if layout == "data_then_key":
        return head + list(data) + list(key)
    if layout == "key_then_data":
        return head + list(key) + list(data)
    if layout == "data_only":
        return head + list(data)
    raise NfcError(f"未知写块格式: {layout}", 5)


def normalize_hex(text: str, size: int = 16, exact: bool = False) -> bytes:
    """把用户输入的十六进制串规整成定长 bytes（不足补 0，超长截断）。"""
    s = "".join(ch for ch in (text or "") if ch in "0123456789abcdefABCDEF")
    if len(s) % 2:
        s = "0" + s
    try:
        raw = bytes.fromhex(s)
    except Exception as exc:                      # noqa: BLE001
        raise NfcError(f"十六进制格式错误：{text!r}", 6) from exc
    if exact and len(raw) != size:
        raise NfcError(f"长度必须是 {size} 字节，当前 {len(raw)} 字节", 6)
    return raw[:size].ljust(size, b"\x00")


# ================================================================
# v2.2.1 新增：P0 写卡 / 读 ID 卡 / 写手机手环 NFC（语义取自微信小程序
#             「宜新 NFC Reader Tool」wxc3dd33d9d4ae0fd6，命令码以小程序为准）
# ================================================================

# ---- 写块档位（A2）----------------------------------------------------
# 极速档 0x16：DATA = cmd, 块号, 数据16            （18 字节）
# 普通档 0x18：DATA = cmd, 块号, 0x00, 数据16      （19 字节）
# 带密钥类型档 0x08：DATA = cmd, 块号, 密钥类型, 数据16（19 字节，小程序 card-write 页 IC 写块指令）
#
# ⚠ 语义修正（对齐小程序）：历史上把 0x24 当成「延迟写块」是错的。小程序里
#   0x24 是**单字节卡号校验**命令（返回 1=校验通过 / 2=未知 / 7=卡片不符），
#   真正的第三个写块指令是 0x08（带密钥类型）。旧档位名 delay / 0x24 仅作兼容别名保留。
WRITE_MODES = {
    "fast": {"cmd": 0x16, "layout": "block_data", "timeout": 8.0,
             "label": "极速写卡", "hint": "0x16 · 写入后立即返回，速度最快"},
    "normal": {"cmd": 0x18, "layout": "block_zero_data", "timeout": 12.0,
               "label": "普通写卡", "hint": "0x18 · 标准写块时序"},
    "keyed": {"cmd": 0x08, "layout": "key_type_data", "timeout": 12.0,
              "label": "带密钥类型写卡",
              "hint": "0x08 · cmd, 块号, 密钥类型, 数据16（小程序 IC 写块指令）"},
}

# 手机 / 手环 NFC 写录只用小程序 card-write 页的两档
PHONE_WRITE_MODES = ("fast", "normal")

# 中文档位名与命令码别名，前端传值一律容错
WRITE_MODE_ALIASES = {
    "极速": "fast", "极速写卡": "fast", "fast": "fast", "0x16": "fast", "16": "fast",
    "普通": "normal", "普通写卡": "normal", "normal": "normal", "0x18": "normal", "18": "normal",
    "带密钥": "keyed", "带密钥类型写卡": "keyed", "keyed": "keyed",
    "0x08": "keyed", "08": "keyed", "8": "keyed",
    # 旧前端遗留值：delay / 延迟写卡 / 0x24 一律纠正到正确指令 0x08，
    # 避免误发单字节 0x24（卡号校验）导致的「写卡无反应」。
    "延迟": "keyed", "延迟写卡": "keyed", "delay": "keyed", "0x24": "keyed", "24": "keyed",
}


def resolve_write_mode(mode) -> Tuple[str, dict]:
    """把前端传来的档位（fast/normal/keyed 或中文/命令码）规整成 (key, 配置)。"""
    raw = str(mode if mode is not None else "normal").strip()
    key = WRITE_MODE_ALIASES.get(raw.lower(), WRITE_MODE_ALIASES.get(raw, raw.lower()))
    conf = WRITE_MODES.get(key)
    if not conf:
        raise NfcError(f"未知写卡档位：{mode}（可选 fast / normal / keyed）", 15)
    return key, conf


def build_block_write_payload(mode, block: int, data: bytes,
                              key_type: int = 0) -> Tuple[bytes, float]:
    """构造 0x16 / 0x18 / 0x08 写块帧的 DATA 段，返回 (DATA, 建议超时)。"""
    _key, conf = resolve_write_mode(mode)
    blk = int(block) & 0xFF
    body = bytes(data)[:16].ljust(16, b"\x00")
    if conf["layout"] == "block_data":
        payload = bytes([conf["cmd"], blk]) + body
    elif conf["layout"] == "key_type_data":
        payload = bytes([conf["cmd"], blk, int(key_type) & 0xFF]) + body
    else:
        payload = bytes([conf["cmd"], blk, 0x00]) + body
    return payload, float(conf["timeout"])


def write_mode_table() -> List[dict]:
    """给前端列档位用。"""
    return [{"id": k, "cmd": f"0x{v['cmd']:02X}", "label": v["label"],
             "hint": v["hint"], "timeout": v["timeout"]}
            for k, v in WRITE_MODES.items()]


# ---- 0x0C 写卡（A1）---------------------------------------------------
def norm_fixed_bytes(value, size: int, name: str = "数据") -> bytes:
    """把卡号 / UID 规整成定长 bytes：支持 bytes、hex 串、十进制整数。"""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, int):
        if value < 0:
            raise NfcError(f"{name}不能为负数", 15)
        raw = value.to_bytes(size, "big")
    else:
        text = str(value or "").strip()
        if not text:
            raise NfcError(f"{name}不能为空", 15)
        clean = "".join(ch for ch in text if ch in "0123456789abcdefABCDEF")
        if len(clean) % 2:
            clean = "0" + clean
        try:
            raw = bytes.fromhex(clean)
        except ValueError as exc:
            raise NfcError(f"{name}格式错误：{text!r}", 6) from exc
    if len(raw) > size:
        raise NfcError(f"{name}长度必须是 {size} 字节，当前 {len(raw)} 字节", 6)
    return raw.rjust(size, b"\x00")


def build_card_no_payload(card_no) -> bytes:
    """0x0C 写卡：DATA = cmd + 5 字节卡号（小程序 y() 构造，共 6 字节）。"""
    return bytes([CMD.WRITE_CARD]) + norm_fixed_bytes(card_no, 5, "卡号")


# ---- 0x27 写 UID / 写手机手环 NFC（A4）--------------------------------
def build_write_uid_payload(uid) -> bytes:
    """0x27 写 UID：DATA = cmd, 0x00, 6 字节 UID（大端），共 8 字节。"""
    return bytes([CMD.WRITE_UID, 0x00]) + norm_fixed_bytes(uid, 6, "UID")


# ---- 0x26 读 ID 卡（B1）----------------------------------------------
ID_FREQ_MAP = {1: "125 kHz", 2: "250 kHz", 3: "375 kHz", 4: "500 kHz"}
ID_CARD_TYPES = {1: "EM4100 / EM4200（标准 ID 卡）", 2: "TK4100 兼容卡",
                 3: "低频 ID 卡", 4: "HID / 其它低频卡"}


def parse_id_card(d: bytes) -> dict:
    """解析 0x26 读 ID 卡响应。

    响应布局（小程序 idx26 解析一致）：
        [0] 命令码 0x26 | [1] 状态码 | [2..6] 卡号(40bit) | [7] 频率档位
        [8..11] 卡代码 | [12..15] 设施码
    """
    if not d:
        raise NfcError("读 ID 卡空响应")
    if d[0] != CMD.READ_ID_CARD:
        raise NfcError(f"读 ID 卡响应命令码不符：0x{d[0]:02X}")
    code = int(d[1]) if len(d) > 1 else 2
    if code != 0:
        raise NfcError(err_text(code), code)
    if len(d) < 16:
        raise NfcError(f"读 ID 卡响应过短（{len(d)} 字节）：{fmt_hex(d)}")

    card_id = (d[2] << 32) | (d[3] << 24) | (d[4] << 16) | (d[5] << 8) | d[6]
    freq_id = int(d[7])
    card_code = (d[8] << 24) | (d[9] << 16) | (d[10] << 8) | d[11]
    facility = (d[12] << 24) | (d[13] << 16) | (d[14] << 8) | d[15]
    return {
        "id": card_id,
        "id_hex": f"{card_id:010X}",
        "id_dec": f"{card_id:010d}",
        "freq_id": freq_id,
        "freq": ID_FREQ_MAP.get(freq_id, f"未知档位 {freq_id}"),
        "card_type_id": freq_id,
        "card_type": ID_CARD_TYPES.get(freq_id, "低频 ID 卡"),
        "card_code": card_code,
        "facility_code": facility,
        "raw": fmt_hex(d),
    }


# ---- 0x24 卡号校验（单字节）------------------------------------------
def build_check_card_no_payload() -> bytes:
    """0x24 卡号校验：DATA 只有命令码 1 字节（小程序 `y(0x24)`）。"""
    return bytes([CMD.CHECK_CARD_NO])


def parse_check_card_no(d: bytes) -> dict:
    """解析 0x24 卡号校验响应。

    小程序判定：status==1 → 校验通过；其它 → 未通过（2=未知、7=卡片不符/寻卡失败）。
    """
    if not d:
        raise NfcError("卡号校验空响应")
    if d[0] != CMD.CHECK_CARD_NO:
        raise NfcError(f"卡号校验响应命令码不符：0x{d[0]:02X}")
    code = int(d[1]) if len(d) > 1 else 2
    return {
        "ok": code == 1,
        "status": code,
        "err": None if code == 1 else (err_text(code) if code != 2 else "卡号校验未通过 Code:2"),
    }


# ---- 0x17 封卡 / 0x23 锁卡（单字节，不可逆）----------------------------
def build_ufuid_lock_payload() -> bytes:
    """0x17 封卡：DATA 只有命令码 1 字节（类魔术卡 UFUID 封卡，不可逆）。"""
    return bytes([CMD.UFUID_LOCK])


def build_lock_card_payload() -> bytes:
    """0x23 锁卡：DATA 只有命令码 1 字节（不可逆）。"""
    return bytes([CMD.LOCK_CARD])


def parse_lock_result(d: bytes, cmd: int = CMD.UFUID_LOCK) -> dict:
    """解析封卡/锁卡响应：status==0 表示成功。"""
    if not d:
        raise NfcError("封卡空响应")
    if d[0] != cmd:
        raise NfcError(f"封卡响应命令码不符：0x{d[0]:02X}")
    code = int(d[1]) if len(d) > 1 else 2
    return {"ok": code == 0, "status": code,
            "err": None if code == 0 else err_text(code),
            "cmd": f"0x{cmd:02X}"}


# ---- 0x38 自动识别卡片（单字节）---------------------------------------
def build_auto_identify_payload() -> bytes:
    """0x38 自动识别卡片：DATA 只有命令码 1 字节。

    与 staticnested 变体同码（0x38），设备靠 DATA 长度区分：
    自动识别为 1 字节，破解为 5 字节。
    """
    return bytes([CMD.AUTO_IDENTIFY])


def parse_auto_identify(d: bytes) -> dict:
    """解析 0x38 自动识别卡片响应。

    响应布局（小程序 idx38 解析）：[2..6] 卡号(40bit) 5 字节大端 | [7] 卡类型
    | [8..11] 卡代码 | [12..15] 设施码。
    """
    if not d:
        raise NfcError("自动识别空响应")
    if d[0] != CMD.AUTO_IDENTIFY:
        raise NfcError(f"自动识别响应命令码不符：0x{d[0]:02X}")
    code = int(d[1]) if len(d) > 1 else 7
    if code != 0:
        raise NfcError(err_text(code), code)
    if len(d) < 16:
        raise NfcError(f"自动识别响应过短（{len(d)} 字节）：{fmt_hex(d)}")

    card_id = (d[2] << 32) | (d[3] << 24) | (d[4] << 16) | (d[5] << 8) | d[6]
    type_id = int(d[7])
    card_code = (d[8] << 24) | (d[9] << 16) | (d[10] << 8) | d[11]
    facility = (d[12] << 24) | (d[13] << 16) | (d[14] << 8) | d[15]
    return {
        "id": card_id,
        "id_hex": f"{card_id:010X}",
        "id_dec": f"{card_id:010d}",
        "card_type_id": type_id,
        "card_type": ID_CARD_TYPES.get(type_id, CARD_TYPE.get(type_id, f"未知类型 {type_id}")),
        "card_type_label": ID_CARD_TYPES.get(type_id, CARD_TYPE.get(type_id, "未知类型")),
        "card_code": card_code,
        "facility_code": facility,
        "raw": fmt_hex(d),
    }


# ---- 0x06 / 0x09 写密钥（手机 NFC 写录前置数据准备）--------------------
def build_write_key_payload(block: int, key, key_type: int = 0) -> bytes:
    """0x06/0x09 写密钥：DATA = cmd, 块号, 密钥6（小程序 card-write 写密钥）。"""
    cmd = CMD.KEY_B_CHECK if int(key_type) else CMD.KEY_A_CHECK
    return bytes([cmd, int(block) & 0xFF]) + norm_fixed_bytes(key, 6, "密钥")


# ---- 手机 / 手环 NFC 写录编排（A4，与写 UID / 魔术卡区分）-------------
# 语义对照（易混淆，务必区分）：
#   · 写手机 / 手环 NFC  → 「转接卡」两步法：先用 0x0C 把卡号写进空白卡，
#                          再用 0x27 生成转接卡（模拟卡），最后按 0x16/0x18 写 IC 数据，
#                          手机 / 手环贴上去即可读出该卡号。
#   · 写 UID 卡 / 魔术卡 → 0x27 直接改写卡片 UID（部分卡不可逆）。
#   · 封卡              → 0x17 单字节（UFUID 类魔术卡封卡，不可逆）。
PHONE_NFC_STEPS = (
    {"id": "find", "cmd": "0x11", "label": "寻卡（0x11）",
     "desc": "确认转接卡/目标卡已就位并识别卡类型"},
    {"id": "card_no", "cmd": "0x0C", "label": "写卡号（0x0C）",
     "desc": "把要模拟的 5 字节卡号写入转接卡"},
    {"id": "verify_no", "cmd": "0x26", "label": "回读卡号（0x26）",
     "desc": "读 ID 卡指令回读，比对写入值与当前值"},
    {"id": "bridge", "cmd": "0x27", "label": "生成转接卡（0x27）",
     "desc": "0x27 写 UID 8 字节，使空白卡可被手机 / 手环读取"},
    {"id": "blocks", "cmd": "0x16/0x18", "label": "写 IC 数据（0x16/0x18）",
     "desc": "按档位逐块写入数据并把卡号落在第 0 块"},
    {"id": "verify", "cmd": "0x07", "label": "回读校验（0x07）",
     "desc": "逐块回读数据，确认写入结果"},
)


def phone_nfc_plan(modes=PHONE_WRITE_MODES) -> List[dict]:
    """手机 / 手环 NFC 写录的步骤清单（给前端展示与自检用）。"""
    allow = set(modes or PHONE_WRITE_MODES)
    out = []
    for step in PHONE_NFC_STEPS:
        if step["id"] == "blocks":
            for k in PHONE_WRITE_MODES:
                if k in allow:
                    conf = WRITE_MODES[k]
                    out.append({"id": f"blocks:{k}", "cmd": f"0x{conf['cmd']:02X}",
                                "label": f"{conf['label']}（档位 {k}）", "desc": conf["hint"]})
        else:
            out.append(dict(step))
    return out


# ---- 写块响应解析（0x16 / 0x18 / 0x08）--------------------------------
def parse_block_write_result(d: bytes, cmd: int = 0) -> dict:
    """解析写块响应。

    响应布局：[0] 命令码 | [1] 出错块号 | [2] 状态码。
    小程序的失败定位就是把 [1] 当块号、[2] 当状态码展示。
    """
    if not d:
        raise NfcError("写块空响应")
    if cmd and d[0] != cmd:
        raise NfcError(f"写块响应命令码不符：0x{d[0]:02X}（期望 0x{cmd:02X}）")
    block = int(d[1]) if len(d) > 1 else 0
    code = int(d[2]) if len(d) > 2 else 2
    return {"cmd": f"0x{d[0]:02X}", "block": block, "status": code,
            "ok": code == 0, "err": None if code == 0 else err_text(code)}
