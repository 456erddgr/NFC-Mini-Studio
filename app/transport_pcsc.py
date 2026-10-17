# -*- coding: utf-8 -*-
"""
传输层 · 通用 PC/SC 读卡器通道
==============================
把「NFC Mini 私有协议帧」翻译成「标准 PC/SC 伪 APDU」，让 device.py 里
所有业务逻辑（寻卡/读块/写块/整卡 dump/写 UID…）在**通用读卡器**上照常跑。

设计要点
--------
1. 上层只认 `Transport.send(frame)` / `on_data(bytes)`，与 BLE 通道完全一致。
2. 本通道在 send() 时同步地把一帧拆成命令，跑完 APDU 序列后**自己合成一帧应答**
   回灌给 `on_data`，因此 device.py 的 msgid 配对/超时/重试逻辑一行都不用改。
3. 密钥认证状态在本地维护（按扇区缓存 KEY A/B），因为通用读卡器不像原厂固件
   那样"认证一次记住整卡"，每个扇区需要单独 load_key + authenticate。

支持的读卡器
------------
任何 Windows PC/SC 读卡器，只要实现了 MIFARE 伪 APDU 扩展：
  FF 82 LOAD KEY / FF 86 GENERAL AUTHENTICATE /
  FF B0 READ BINARY / FF D6 UPDATE BINARY / FF CA GET DATA
绝大多数 ACR122U / ACR1252 / ACR1255 / PN532 方案都支持。

不需要任何第三方 Python 包：直接 ctypes 调 winscard.dll。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from .protocol import (
    CMD, DEFAULT_KEY, NfcError, build_frame, classify_card, err_text, fmt_hex,
    status_index,
)

# ================================================================ PC/SC 常量
SCARD_SCOPE_USER = 0x0000
SCARD_SCOPE_SYSTEM = 0x0002

SCARD_SHARE_SHARED = 0x0002
SCARD_SHARE_EXCLUSIVE = 0x0001

SCARD_PROTOCOL_T0 = 0x0001
SCARD_PROTOCOL_T1 = 0x0002

SCARD_LEAVE_CARD = 0x0000
SCARD_RESET_CARD = 0x0001
SCARD_UNPOWER_CARD = 0x0002

SCARD_STATE_UNAWARE = 0x0000
SCARD_STATE_PRESENT = 0x0020

# 错误码中文解释（给用户看得懂的原因）
SCARD_ERR = {
    0x00000000: ("SCARD_S_SUCCESS", "成功"),
    0x80100001: ("SCARD_F_INTERNAL_ERROR", "PC/SC 内部错误"),
    0x80100002: ("SCARD_E_CANCELLED", "操作被取消"),
    0x80100003: ("SCARD_E_INVALID_HANDLE", "无效句柄"),
    0x80100004: ("SCARD_E_INVALID_PARAMETER", "参数无效"),
    0x80100005: ("SCARD_E_INVALID_TARGET", "无效目标"),
    0x80100006: ("SCARD_E_NO_MEMORY", "内存不足"),
    0x80100007: ("SCARD_F_WAITED_TOO_LONG", "等待超时"),
    0x80100008: ("SCARD_E_INSUFFICIENT_BUFFER", "缓冲区不足"),
    0x80100009: ("SCARD_E_UNKNOWN_READER", "未知读卡器"),
    0x8010000A: ("SCARD_E_TIMEOUT", "读卡器超时"),
    0x8010000B: ("SCARD_E_SHARING_VIOLATION", "读卡器被其它程序占用"),
    0x8010000C: ("SCARD_E_NO_SMARTCARD", "读卡器里没有卡片"),
    0x8010000D: ("SCARD_E_UNKNOWN_CARD", "未知卡片"),
    0x8010000E: ("SCARD_E_CANT_DISPOSE", "无法释放资源"),
    0x8010000F: ("SCARD_E_PROTO_MISMATCH", "协议不匹配"),
    0x80100010: ("SCARD_E_NOT_READY", "读卡器未就绪"),
    0x80100011: ("SCARD_E_INVALID_VALUE", "数值无效"),
    0x80100012: ("SCARD_E_SYSTEM_CANCELLED", "系统取消"),
    0x80100013: ("SCARD_F_COMM_ERROR", "通信错误"),
    0x80100014: ("SCARD_F_UNKNOWN_ERROR", "未知错误"),
    0x80100015: ("SCARD_E_INVALID_ATR", "ATR 无效"),
    0x80100016: ("SCARD_E_NOT_TRANSACTED", "未进行事务"),
    0x80100017: ("SCARD_E_READER_UNAVAILABLE", "读卡器不可用"),
    0x80100018: ("SCARD_P_SHUTDOWN", "PC/SC 正在关闭"),
    0x8010001D: ("SCARD_E_NO_SERVICE", "PC/SC 服务(SCardSvr)未启动"),
    0x8010001E: ("SCARD_E_SERVICE_STOPPED", "PC/SC 服务已停止"),
    0x80100065: ("SCARD_W_UNPOWERED_CARD", "卡片未上电"),
    0x80100066: ("SCARD_W_UNRESPONSIVE_CARD", "卡片无响应"),
    0x80100067: ("SCARD_W_UNSUPPORTED_CARD", "不支持的卡片"),
    0x80100068: ("SCARD_W_RESET_CARD", "卡片已复位"),
    0x80100069: ("SCARD_W_REMOVED_CARD", "卡片已移开"),
    0x8010006A: ("SCARD_W_SECURITY_VIOLATION", "安全违规"),
    0x8010006B: ("SCARD_W_WRONG_CHV", "口令错误"),
    0x8010006C: ("SCARD_W_CHV_BLOCKED", "口令已锁定"),
    0x8010006D: ("SCARD_W_EOF", "已到文件末尾"),
    0x8010006E: ("SCARD_W_CANCELLED_BY_USER", "用户取消"),
    0x8010006F: ("SCARD_W_CARD_NOT_AUTHENTICATED", "卡片未认证"),
}

LONG = wintypes.LONG
DWORD = wintypes.DWORD
LPBYTE = ctypes.POINTER(ctypes.c_ubyte)
LPDWORD = ctypes.POINTER(DWORD)
SCARDCONTEXT = ctypes.c_void_p
SCARDHANDLE = ctypes.c_void_p


class SCARD_READERSTATE(ctypes.Structure):
    _fields_ = [
        ("szReader", wintypes.LPCWSTR),
        ("pvUserData", ctypes.c_void_p),
        ("dwCurrentState", DWORD),
        ("dwEventState", DWORD),
        ("cbAtr", DWORD),
        ("rgbAtr", ctypes.c_ubyte * 36),
    ]


class PcscError(NfcError):
    """PC/SC 调用失败，带错误码与中文原因。"""

    def __init__(self, code: int, where: str = "") -> None:
        self.code = code & 0xFFFFFFFF
        sym, zh = SCARD_ERR.get(self.code, ("UNKNOWN", f"未知错误 0x{self.code:08X}"))
        self.symbol = sym
        self.zh = zh
        super().__init__(f"{where + '：' if where else ''}{zh}（{sym}）")


# ================================================================ winscard 绑定
class WinSCard:
    """winscard.dll 的惰性绑定。"""

    _inst: Optional["WinSCard"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        try:
            self.dll = ctypes.WinDLL("winscard.dll")
        except OSError as exc:                     # pragma: no cover - 非 Windows
            raise NfcError(f"找不到 PC/SC 运行库 winscard.dll：{exc}") from exc

        d = self.dll
        d.SCardEstablishContext.argtypes = [DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(SCARDCONTEXT)]
        d.SCardEstablishContext.restype = LONG
        d.SCardReleaseContext.argtypes = [SCARDCONTEXT]
        d.SCardReleaseContext.restype = LONG
        d.SCardIsValidContext.argtypes = [SCARDCONTEXT]
        d.SCardIsValidContext.restype = LONG
        d.SCardListReadersW.argtypes = [SCARDCONTEXT, wintypes.LPCWSTR,
                                        wintypes.LPWSTR, LPDWORD]
        d.SCardListReadersW.restype = LONG
        d.SCardGetStatusChangeW.argtypes = [SCARDCONTEXT, DWORD,
                                            ctypes.POINTER(SCARD_READERSTATE),
                                            DWORD]
        d.SCardGetStatusChangeW.restype = LONG
        d.SCardConnectW.argtypes = [SCARDCONTEXT, wintypes.LPCWSTR, DWORD, DWORD,
                                    ctypes.POINTER(SCARDHANDLE), LPDWORD]
        d.SCardConnectW.restype = LONG
        d.SCardReconnect.argtypes = [SCARDHANDLE, DWORD, DWORD, DWORD, LPDWORD]
        d.SCardReconnect.restype = LONG
        d.SCardDisconnect.argtypes = [SCARDHANDLE, DWORD]
        d.SCardDisconnect.restype = LONG
        d.SCardStatusW.argtypes = [SCARDHANDLE, wintypes.LPWSTR, LPDWORD, LPDWORD,
                                   LPDWORD, LPDWORD, LPBYTE, LPDWORD]
        d.SCardStatusW.restype = LONG
        d.SCardTransmit.argtypes = [SCARDHANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    DWORD, LPBYTE, DWORD, ctypes.c_void_p, LPDWORD]
        d.SCardTransmit.restype = LONG
        d.SCardBeginTransaction.argtypes = [SCARDHANDLE]
        d.SCardBeginTransaction.restype = LONG
        d.SCardEndTransaction.argtypes = [SCARDHANDLE, DWORD]
        d.SCardEndTransaction.restype = LONG

    @classmethod
    def get(cls) -> "WinSCard":
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    # ---- 高层封装
    def establish(self) -> SCARDCONTEXT:
        ctx = SCARDCONTEXT()
        rc = self.dll.SCardEstablishContext(SCARD_SCOPE_USER, None, None,
                                            ctypes.byref(ctx))
        if (rc & 0xFFFFFFFF) != 0:
            raise PcscError(rc, "建立 PC/SC 上下文失败")
        return ctx

    def release(self, ctx: SCARDCONTEXT) -> None:
        try:
            self.dll.SCardReleaseContext(ctx)
        except Exception:
            pass

    def list_readers(self, ctx: SCARDCONTEXT) -> List[str]:
        n = DWORD(0)
        rc = self.dll.SCardListReadersW(ctx, None, None, ctypes.byref(n))
        code = rc & 0xFFFFFFFF
        if code in (0x8010001D, 0x8010001E):
            raise PcscError(rc, "PC/SC 服务未启动")
        if code != 0 and n.value == 0:
            raise PcscError(rc, "枚举读卡器失败")
        if n.value == 0:
            return []
        n = DWORD(max(n.value, 8192))
        buf = ctypes.create_unicode_buffer(n.value)
        rc = self.dll.SCardListReadersW(ctx, None, buf, ctypes.byref(n))
        if (rc & 0xFFFFFFFF) != 0:
            raise PcscError(rc, "枚举读卡器失败")
        out: List[str] = []
        i = 0
        total = n.value
        while i < total and buf[i]:
            s = buf[i]
            while i < total and buf[i]:
                i += 1
            i += 1
            out.append(s)
        return out

    def status(self, ctx: SCARDCONTEXT, reader: str) -> dict:
        """读卡器 + 卡片状态（不建连接，只查询）。"""
        st = SCARD_READERSTATE()
        st.szReader = reader
        st.pvUserData = None
        st.dwCurrentState = SCARD_STATE_UNAWARE
        st.dwEventState = SCARD_STATE_UNAWARE
        st.cbAtr = 0
        rc = self.dll.SCardGetStatusChangeW(ctx, DWORD(0), ctypes.byref(st), DWORD(1))
        code = rc & 0xFFFFFFFF
        # 0x8010000A = SCARD_E_TIMEOUT：首次查询必然超时，但 dwEventState 已填好
        if code not in (0, 0x8010000A):
            return {"ok": False, "code": code,
                    "error": SCARD_ERR.get(code, ("", "查询状态失败"))[1]}
        atr = bytes(st.rgbAtr[:st.cbAtr]) if st.cbAtr else b""
        return {
            "ok": True,
            "state": st.dwEventState,
            "state_bits": _state_bits(st.dwEventState),
            "protocol": 0,
            "atr": atr,
            "atr_hex": fmt_hex(atr) if atr else "",
            "card_present": bool(st.dwEventState & SCARD_STATE_PRESENT),
        }

    def wait_change(self, ctx: SCARDCONTEXT, reader: str,
                    timeout_ms: int = 5000) -> dict:
        """等读卡器状态变化（插卡/拔卡）。"""
        st = SCARD_READERSTATE()
        st.szReader = reader
        st.pvUserData = None
        st.dwCurrentState = SCARD_STATE_UNAWARE
        st.dwEventState = SCARD_STATE_UNAWARE
        st.cbAtr = 0
        rc = self.dll.SCardGetStatusChangeW(ctx, DWORD(int(timeout_ms)),
                                            ctypes.byref(st), DWORD(1))
        code = rc & 0xFFFFFFFF
        if code == 0x8010000A:                     # 超时 = 没有变化
            return {"ok": True, "changed": False,
                    "card_present": bool(st.dwEventState & SCARD_STATE_PRESENT)}
        if code != 0:
            return {"ok": False, "code": code,
                    "error": SCARD_ERR.get(code, ("", "等待状态失败"))[1]}
        return {"ok": True, "changed": True,
                "card_present": bool(st.dwEventState & SCARD_STATE_PRESENT),
                "atr_hex": fmt_hex(bytes(st.rgbAtr[:st.cbAtr])) if st.cbAtr else ""}

    def connect(self, ctx: SCARDCONTEXT, reader: str,
                share: int = SCARD_SHARE_SHARED) -> Tuple[SCARDHANDLE, int]:
        h = SCARDHANDLE()
        proto = DWORD(0)
        rc = self.dll.SCardConnectW(ctx, reader, DWORD(share),
                                    DWORD(SCARD_PROTOCOL_T0 | SCARD_PROTOCOL_T1),
                                    ctypes.byref(h), ctypes.byref(proto))
        if (rc & 0xFFFFFFFF) != 0:
            raise PcscError(rc, f"连接读卡器 {reader} 失败")
        return h, proto.value

    def disconnect(self, h: SCARDHANDLE,
                   disposition: int = SCARD_LEAVE_CARD) -> None:
        try:
            self.dll.SCardDisconnect(h, DWORD(disposition))
        except Exception:
            pass

    def transmit(self, h: SCARDHANDLE, proto: int, apdu: bytes,
                 note: str = "") -> bytes:
        send = (ctypes.c_ubyte * max(1, len(apdu)))(*apdu)
        recv = (ctypes.c_ubyte * 8192)()
        rlen = DWORD(8192)
        rc = self.dll.SCardTransmit(h, None, None, DWORD(proto),
                                    ctypes.cast(send, LPBYTE), DWORD(len(apdu)),
                                    None, ctypes.byref(rlen))
        if (rc & 0xFFFFFFFF) != 0:
            raise PcscError(rc, note or "APDU 收发失败")
        return bytes(recv[:rlen.value])


def _state_bits(v: int) -> List[str]:
    names = [
        (0x001, "IGNORE"), (0x002, "CHANGED"), (0x004, "UNKNOWN"),
        (0x008, "UNAVAILABLE"), (0x010, "EMPTY"), (0x020, "PRESENT"),
        (0x040, "ATRMATCH"), (0x080, "EXCLUSIVE"), (0x100, "INUSE"),
        (0x200, "MUTE"),
    ]
    return [n for b, n in names if v & b]


# ================================================================ APDU 常量
SW_OK = 0x9000


def _sw(resp: bytes) -> int:
    if len(resp) < 2:
        return -1
    return (resp[-2] << 8) | resp[-1]


def sw_text(sw: int) -> str:
    table = {
        0x9000: "成功",
        0x6300: "认证失败（密钥错误）",
        0x6301: "操作失败",
        0x6302: "卡片已锁定",
        0x6400: "执行超时",
        0x6600: "无响应",
        0x6700: "数据长度错误",
        0x6800: "不支持的操作",
        0x6900: "命令不允许",
        0x6982: "安全状态不满足（未认证）",
        0x6983: "认证方法已锁定",
        0x6985: "使用条件不满足",
        0x6986: "命令不允许（当前状态）",
        0x6A00: "参数错误",
        0x6A80: "数据无效",
        0x6A81: "功能不支持",
        0x6A82: "文件/块未找到",
        0x6A83: "记录未找到",
        0x6A86: "参数 P1/P2 错误",
        0x6A88: "引用数据未找到",
        0x6B00: "参数错误（P1/P2）",
        0x6D00: "指令不支持",
        0x6E00: "类不支持",
        0x6F00: "未知错误",
    }
    return table.get(sw, f"SW=0x{sw:04X}")


# ================================================================ 传输实现
class PcscTransport:
    """
    通用 PC/SC 读卡器传输层。

    对外暴露与 BleTransport 相同的接口：
        .kind / .label / .connected / .open(reader=...) / .close() / .send(frame)
        .on_data(bytes) —— 收完整帧字节，与 BLE 通道语义一致
                          （device.py 的 _on_data 会丢进 FrameParser 解析）。
    """

    kind = "pcsc"
    label = "通用读卡器"

    TRANSMIT_RETRY = 2

    def __init__(self, on_log=None) -> None:
        self.on_data = None
        self.on_log = on_log
        self.reader: Optional[str] = None
        self._scard: Optional[WinSCard] = None
        self._ctx: Optional[SCARDCONTEXT] = None
        self._card: Optional[SCARDHANDLE] = None
        self._proto = 0
        self._lock = threading.RLock()
        self._connected = False
        self._atr = b""
        self._authed: Dict[Tuple[int, int], bool] = {}
        self._loaded_key: Optional[Tuple[bytes, int]] = None
        self._uid_cache: bytes = b""
        self._default_key: bytes = DEFAULT_KEY
        self._default_key_type: int = 0
        self.last_error = ""

    # ------------------------------------------------------------ 日志
    def _log(self, level: str, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, text)
            except Exception:
                pass

    # ------------------------------------------------------------ 属性
    @property
    def connected(self) -> bool:
        return bool(self._connected and self._card)

    @property
    def display_label(self) -> str:
        return f"PC/SC · {self.reader or ''}".strip()

    # ------------------------------------------------------------ 静态探测
    @classmethod
    def probe(cls) -> dict:
        """检测 PC/SC 环境（不建连接），给 UI 判断「能否用」。"""
        info = {"supported": False, "service_running": False, "readers": [],
                "error": "", "hint": ""}
        try:
            sc = WinSCard.get()
        except NfcError as exc:
            info["error"] = str(exc)
            info["hint"] = "当前系统没有 PC/SC 运行库，通用读卡器不可用"
            return info
        info["supported"] = True
        try:
            ctx = sc.establish()
        except PcscError as exc:
            info["error"] = str(exc)
            if exc.code in (0x8010001D, 0x8010001E):
                info["hint"] = ("PC/SC 服务（SCardSvr）没有启动。"
                                "请以管理员身份运行：sc config SCardSvr start= auto "
                                "然后 net start SCardSvr；或在「服务」里启动 Smart Card。")
            else:
                info["hint"] = exc.zh
            return info
        info["service_running"] = True
        try:
            readers = sc.list_readers(ctx)
            info["readers"] = readers
            if not readers:
                info["hint"] = ("PC/SC 服务正常，但没有检测到读卡器。"
                                "请插上 USB 读卡器并确认驱动已装好"
                                "（设备管理器应出现「智能卡读卡器」）。")
        except PcscError as exc:
            info["error"] = str(exc)
            info["hint"] = exc.zh
        finally:
            sc.release(ctx)
        return info

    @classmethod
    def list_readers(cls) -> List[dict]:
        """枚举读卡器 + 当前卡片在位状态。UI 设备列表直接用。"""
        out: List[dict] = []
        try:
            sc = WinSCard.get()
            ctx = sc.establish()
        except (NfcError, PcscError):
            return out
        try:
            for name in sc.list_readers(ctx):
                st = sc.status(ctx, name)
                out.append({
                    "address": name,
                    "name": name,
                    "rssi": 0,
                    "likely_reader": True,
                    "transport": "pcsc",
                    "card_present": bool(st.get("card_present")),
                    "atr": st.get("atr_hex", ""),
                    "state": st.get("state_bits", []),
                })
        except PcscError:
            pass
        finally:
            sc.release(ctx)
        return out

    # ------------------------------------------------------------ 打开/关闭
    def open(self, reader: str = "", timeout: float = 20.0, **kw) -> None:
        name = str(reader or "").strip()
        self.close()
        self._scard = WinSCard.get()
        self._ctx = self._scard.establish()
        try:
            readers = self._scard.list_readers(self._ctx)
        except PcscError:
            self.close()
            raise
        if not readers:
            self.close()
            raise NfcError("没有检测到 PC/SC 读卡器，请先插上 USB 读卡器")
        target = name if name in readers else readers[0]
        if name and name not in readers:
            self._log("warn", f"指定的读卡器 {name} 不在列表中，改用 {target}")
        self._log("info", f"连接通用读卡器 {target} …")
        self._card, self._proto = self._scard.connect(self._ctx, target)
        self.reader = target
        self._connected = True
        self._authed.clear()
        self._loaded_key = None
        self._uid_cache = b""
        try:
            st = self._scard.status(self._ctx, target)
            self._atr = st.get("atr", b"")
        except Exception:
            self._atr = b""
        self._log("ok", f"已连接 {target}"
                        + (f"  ATR {fmt_hex(self._atr)}" if self._atr
                           else "（当前无卡）"))

    def close(self) -> None:
        with self._lock:
            try:
                if self._scard and self._card:
                    self._scard.disconnect(self._card, SCARD_LEAVE_CARD)
            except Exception:
                pass
            self._card = None
            self._proto = 0
            try:
                if self._scard and self._ctx:
                    self._scard.release(self._ctx)
            except Exception:
                pass
            self._ctx = None
            self._connected = False
            self._authed.clear()
            self._loaded_key = None
            self._uid_cache = b""

    # ------------------------------------------------------------ 卡片在位
    def card_present(self) -> bool:
        if not (self._scard and self._ctx and self.reader):
            return False
        try:
            st = self._scard.status(self._ctx, self.reader)
            return bool(st.get("card_present"))
        except Exception:
            return False

    def wait_card(self, timeout: float = 10.0) -> bool:
        """等卡片贴近（通用读卡器按位，没有"寻卡"命令，只能轮询）。"""
        deadline = time.time() + max(0.2, float(timeout))
        while time.time() < deadline:
            if self.card_present():
                return True
            time.sleep(0.15)
        return self.card_present()

    # ------------------------------------------------------------ APDU 基础
    def _xfer(self, apdu: bytes, note: str = "") -> bytes:
        if not (self._scard and self._card):
            raise NfcError("通用读卡器未连接")
        last: Optional[Exception] = None
        for i in range(self.TRANSMIT_RETRY + 1):
            try:
                self._reconnect_if_needed()
                return self._scard.transmit(self._card, self._proto, apdu, note)
            except PcscError as exc:
                last = exc
                if exc.code in (0x80100069, 0x8010000C, 0x80100017,
                                0x8010000B, 0x8010000D, 0x80100066):
                    raise
                if i < self.TRANSMIT_RETRY:
                    time.sleep(0.05)
        raise last or NfcError(note or "APDU 失败")

    def _reconnect_if_needed(self) -> None:
        """卡片被拿开又放回后，句柄会失效；检测到卡片回来了就重连一次。"""
        if not (self._scard and self._ctx and self.reader):
            return
        try:
            st = self._scard.status(self._ctx, self.reader)
        except Exception:
            return
        if not st.get("ok"):
            return
        if st.get("card_present") and not self._card:
            self._card, self._proto = self._scard.connect(self._ctx, self.reader)
            self._authed.clear()
            self._loaded_key = None

    def _xfer_check(self, apdu: bytes, note: str = "") -> bytes:
        resp = self._xfer(apdu, note)
        sw = _sw(resp)
        if sw != SW_OK:
            raise NfcError(f"{note or 'APDU'}：{sw_text(sw)}")
        return resp[:-2]

    # ---- 卡号
    def read_uid(self) -> bytes:
        """GET DATA(UID)：返回 4 或 7 字节 UID（正序）。"""
        if self._uid_cache:
            return self._uid_cache
        resp = self._xfer(bytes([0xFF, 0xCA, 0x00, 0x00, 0x00]), "读取卡号")
        sw = _sw(resp)
        if sw != SW_OK:
            raise NfcError(f"读取卡号失败：{sw_text(sw)}")
        self._uid_cache = resp[:-2]
        return self._uid_cache

    # ---- 密钥装载 / 认证
    def load_key(self, key: bytes, key_type: int = 0) -> None:
        """FF 82 LOAD KEY：把密钥装进读卡器。"""
        k = bytes(key)[:6].ljust(6, b"\x00")
        slot = 0x00 if (int(key_type) & 1) == 0 else 0x01
        apdu = bytes([0xFF, 0x82, 0x20, slot, 0x06]) + k
        self._xfer_check(apdu, f"装载 {'KEY B' if slot else 'KEY A'}")
        self._loaded_key = (k, int(key_type) & 1)

    def authenticate(self, block: int, key: bytes, key_type: int = 0) -> bool:
        """FF 86 GENERAL AUTHENTICATE：认证某个块所在扇区。"""
        k = bytes(key)[:6].ljust(6, b"\x00")
        kt = int(key_type) & 1
        slot = 0x00 if kt == 0 else 0x01
        if self._loaded_key != (k, kt):
            try:
                self.load_key(k, kt)
            except NfcError:
                return False
        apdu = bytes([0xFF, 0x86, 0x00, 0x00, 0x05,
                      slot, 0x00, int(block) & 0xFF, kt, 0x00])
        try:
            resp = self._xfer(apdu, f"认证块 {block}")
        except NfcError:
            return False
        if _sw(resp) == SW_OK:
            self._authed[(int(block) >> 2, kt)] = True
            return True
        return False

    def _ensure_auth(self, block: int, key: bytes = DEFAULT_KEY,
                     key_type: int = 0) -> None:
        sector = int(block) >> 2
        if self._authed.get((sector, int(key_type) & 1)):
            return
        if self.authenticate(block, key, key_type):
            return
        raise NfcError(err_text(8))

    # ---- 读写块
    def read_block_raw(self, block: int, key: bytes = DEFAULT_KEY,
                       key_type: int = 0) -> bytes:
        self._ensure_auth(block, key, key_type)
        apdu = bytes([0xFF, 0xB0, 0x00, int(block) & 0xFF, 0x10])
        data = self._xfer_check(apdu, f"读块 {block}")
        if len(data) < 16:
            raise NfcError(f"读块 {block} 返回长度异常：{len(data)}")
        return data[:16]

    def write_block_raw(self, block: int, data: bytes, key: bytes = DEFAULT_KEY,
                        key_type: int = 0) -> None:
        self._ensure_auth(block, key, key_type)
        body = bytes(data)[:16].ljust(16, b"\x00")
        apdu = bytes([0xFF, 0xD6, 0x00, int(block) & 0xFF, 0x10]) + body
        self._xfer_check(apdu, f"写块 {block}")

    # ------------------------------------------------------------ 卡片识别
    def identify(self) -> dict:
        """按 ATR / UID 判断卡片类型，产出与 0x11 寻卡对齐的字典。"""
        atr = self._atr
        uid = b""
        try:
            uid = self.read_uid()
        except NfcError:
            uid = b""
        is_mf = False
        if uid and len(uid) in (4, 7):
            is_mf = True
        if not is_mf and atr:
            s = atr.hex().upper()
            if s.startswith("3B8F8001") or len(atr) >= 15:
                is_mf = True
        card_type_id = 1 if is_mf else 0
        info = classify_card(card_type_id, uid.hex().upper(), 0x08 if is_mf else 0)
        info["uid"] = uid.hex().upper()
        info["sak"] = "08" if is_mf else ""
        info["sak_raw"] = 0x08 if is_mf else 0
        info["atr"] = fmt_hex(atr)
        info["source"] = "pcsc"
        info["transport"] = "pcsc"
        info["identified"] = bool(uid)
        return info

    # ------------------------------------------------------------ 默认密钥
    def set_default_key(self, key: bytes, key_type: int = 0) -> None:
        self._default_key = bytes(key)[:6].ljust(6, b"\x00")
        self._default_key_type = int(key_type) & 1

    def _key_from_state(self) -> Tuple[bytes, int]:
        return self._default_key, self._default_key_type

    # ================================================================
    # 协议帧 → APDU 命令分发
    # ================================================================
    def send(self, frame: bytes) -> None:
        """解析出 (msgid, data)，执行等价 APDU 操作，再合成应答帧回灌 on_data。"""
        msgid, data = _unframe(frame)
        try:
            payload = self._dispatch(data)
        except NfcError as exc:
            self.last_error = str(exc)
            self._log("err", f"通用读卡器执行失败：{exc}")
            payload = self._error_payload(data, exc)
        except Exception as exc:                   # noqa: BLE001
            self.last_error = str(exc)
            self._log("err", f"通用读卡器异常：{exc}")
            payload = self._error_payload(data, NfcError(str(exc)))

        reply = build_frame(payload, msgid)
        if self.on_data:
            try:
                self.on_data(reply)
            except Exception as exc:               # noqa: BLE001
                self._log("err", f"回灌应答失败：{exc}")

    def _error_payload(self, data: bytes, exc: NfcError) -> bytes:
        """按命令的应答结构合成一个「失败」应答，让上层走正常错误分支。"""
        cmd = int(data[0]) if data else 0
        code = 7
        text = str(exc)
        if "认证失败" in text or "密钥" in text:
            code = 8
        elif "没有卡片" in text or "卡片已移开" in text or "无卡" in text:
            code = 17
        elif "写" in text:
            code = 10
        elif "读" in text:
            code = 9
        idx = status_index(cmd)
        body = bytearray(max(idx + 1, 2))
        body[0] = cmd
        body[idx] = code
        return bytes(body)

    def _dispatch(self, data: bytes) -> bytes:
        """把一条私有协议命令映射成 PC/SC 操作，返回等价应答 DATA 区。"""
        if not data:
            raise NfcError("空命令")
        cmd = int(data[0])
        self._log("trace", f"PC/SC 命令 0x{cmd:02X}  {fmt_hex(data[:24])}")

        # ---- 0x01 设备信息
        if cmd == CMD.INFO:
            name = (self.reader or "PC/SC Reader").encode("utf-8", "replace")
            body = bytearray([CMD.INFO, 0])
            body += name[:24].ljust(24, b"\x00")
            body += bytes([0x02, 0x00])
            body += b"PCSC"
            return bytes(body)

        # ---- 0x02 设备状态
        if cmd == CMD.STATE:
            present = 1 if self.card_present() else 0
            return bytes([CMD.STATE, 0, present, 0x64])

        # ---- 0x11 寻卡
        if cmd == CMD.FIND_CARD:
            if not self.card_present() and not self.wait_card(3.0):
                raise NfcError(err_text(17))
            info = self.identify()
            if not info.get("uid"):
                raise NfcError(err_text(7))
            uid = bytes.fromhex(info["uid"])
            body = bytearray([CMD.FIND_CARD, 0, len(uid)])
            body += uid
            body += bytes([1 if info.get("sak_raw") == 0x08 else 0])
            body += bytes([0x08])
            body += b"\x04\x00"
            return bytes(body)

        # ---- 0x26 读 ID 卡：通用高频读卡器没有低频能力
        if cmd == CMD.READ_ID_CARD:
            return bytes([CMD.READ_ID_CARD, 14])

        # ---- 0x24 卡号校验
        if cmd == CMD.CHECK_CARD_NO:
            if not self.card_present():
                return bytes([CMD.CHECK_CARD_NO, 7])
            return bytes([CMD.CHECK_CARD_NO, 1])

        # ---- 0x07 读块
        if cmd == CMD.READ_BLOCK:
            if len(data) < 2:
                raise NfcError(err_text(2))
            block = int(data[1])
            key, kt = self._key_from_state()
            raw = self.read_block_raw(block, key, kt)
            return bytes([CMD.READ_BLOCK, block & 0xFF, 0]) + raw

        # ---- 0x08 写块（DATA = cmd, 块号, 密钥类型, 数据16）
        if cmd == CMD.WRITE_BLOCK:
            if len(data) < 3:
                raise NfcError(err_text(2))
            block = int(data[1])
            kt = int(data[2]) & 1
            body = bytes(data[3:19]).ljust(16, b"\x00")
            key, _ = self._key_from_state()
            self.write_block_raw(block, body, key, kt)
            return bytes([CMD.WRITE_BLOCK, block & 0xFF, 0, 0])

        # ---- 0x16 / 0x18 写块（DATA = cmd, 块号, [密钥类型,] 数据16）
        if cmd in (CMD.WRITE_BLOCK_16, CMD.WRITE_BLOCK_18):
            if len(data) < 2:
                raise NfcError(err_text(2))
            block = int(data[1])
            if cmd == CMD.WRITE_BLOCK_18:
                body = bytes(data[3:19]).ljust(16, b"\x00")
                kt = int(data[2]) & 1 if len(data) > 2 else 0
            else:
                body = bytes(data[2:18]).ljust(16, b"\x00")
                kt = 0
            key, _ = self._key_from_state()
            self.write_block_raw(block, body, key, kt)
            return bytes([cmd, block & 0xFF, 0])

        # ---- 0x0B 全卡扫描：通用读卡器只能扫有密钥的扇区，交给上层逐块读
        if cmd == CMD.SCAN_CARD:
            return bytes([CMD.SCAN_CARD, 0])

        # ---- 0x0C 写卡号（DATA = cmd + 5 字节卡号）：写进块 0 的 UID 位
        if cmd == CMD.WRITE_CARD:
            if len(data) < 6:
                raise NfcError(err_text(2))
            card_no = bytes(data[1:6])
            uid4 = card_no[-4:]
            bcc = uid4[0] ^ uid4[1] ^ uid4[2] ^ uid4[3]
            blk0 = uid4 + bytes([bcc, 0x08, 0x04, 0x00]) + b"\x00" * 8
            key, kt = self._key_from_state()
            try:
                self.write_block_raw(0, blk0, key, kt)
            except NfcError as exc:
                raise NfcError("该读卡器/卡片无法写卡号（块 0 写保护）：" + str(exc))
            return bytes([CMD.WRITE_CARD, 0])

        # ---- 0x06 / 0x09 校验密钥
        if cmd in (CMD.KEY_A_CHECK, CMD.KEY_B_CHECK):
            if len(data) < 2:
                raise NfcError(err_text(2))
            block = int(data[1])
            kt = 0 if cmd == CMD.KEY_A_CHECK else 1
            key = bytes(data[2:8]).ljust(6, b"\x00")
            ok = self.authenticate(block, key, kt)
            return bytes([cmd, block & 0xFF, 0 if ok else 8])

        # ---- 0x0A 认证
        if cmd == CMD.AUTH:
            if len(data) < 2:
                raise NfcError(err_text(2))
            block = int(data[1])
            key = bytes(data[2:8]).ljust(6, b"\x00")
            ok = self.authenticate(block, key, 0)
            return bytes([CMD.AUTH, block & 0xFF, 0 if ok else 8])

        # ---- 0x27 写 UID
        if cmd == CMD.WRITE_UID:
            if len(data) < 8:
                raise NfcError(err_text(2))
            uid = bytes(data[2:8])
            bcc = uid[0] ^ uid[1] ^ uid[2] ^ uid[3]
            blk0 = bytes(uid) + bytes([bcc, 0x08, 0x04, 0x00]) + b"\x00" * 8
            key, kt = self._key_from_state()
            try:
                self.write_block_raw(0, blk0, key, kt)
            except NfcError as exc:
                raise NfcError("该读卡器无法改写 UID（MIFARE 块 0 写保护）：" + str(exc))
            return bytes([CMD.WRITE_UID, 0])

        # ---- 0x23 锁卡 / 0x17 封卡：通用读卡器没有等价固件命令，保守拒绝
        if cmd in (CMD.LOCK_CARD, CMD.UFUID_LOCK):
            return bytes([cmd, 15])

        # ---- 0x38 自动识别
        if cmd == CMD.AUTO_IDENTIFY:
            info = self.identify()
            if not info.get("uid"):
                raise NfcError(err_text(7))
            uid = bytes.fromhex(info["uid"])
            body = bytearray([CMD.AUTO_IDENTIFY, 0])
            body += uid.ljust(5, b"\x00")[:5]
            body += bytes([1])
            body += b"\x00" * 8
            return bytes(body)

        # ---- 0x40 / 0x41：通用读卡器无设备端密钥表，交给 PC 端自己做
        if cmd == CMD.KEY_CACHE:
            return bytes([CMD.KEY_CACHE, 0])
        if cmd == CMD.BATCH_FIND:
            return bytes([CMD.BATCH_FIND, 0x11])

        # ---- 0x7F 复位
        if cmd == CMD.RESET:
            self._authed.clear()
            self._loaded_key = None
            return bytes([CMD.RESET, 0])

        # ---- 破解类命令：需要固件采集随机数，通用读卡器做不到
        if cmd in (CMD.HARDNESTED, CMD.FULL_DECODE, CMD.HALF_DECODE, CMD.STATICNEST):
            raise NfcError("通用读卡器不具备随机数采集能力，该类破解需用 PC 端算法")

        raise NfcError(f"通用读卡器暂不支持命令 0x{cmd:02X}")


def _unframe(frame: bytes) -> Tuple[int, bytes]:
    """从完整帧里取 (msgid, data)，与 protocol.build_frame 对称。"""
    if not frame:
        raise NfcError("空帧")
    head = frame[0]
    if head == 0xFF:
        if len(frame) < 5:
            raise NfcError("短帧长度不足")
        ln = frame[1]
        msgid = frame[2]
        return msgid, frame[3:3 + ln]
    if head == 0xFD:
        if len(frame) < 7:
            raise NfcError("长帧长度不足")
        ln = (frame[1] << 8) | frame[2]
        msgid = frame[3]
        return msgid, frame[4:4 + ln]
    raise NfcError(f"未知帧头 0x{head:02X}")


__all__ = ["PcscTransport", "PcscError", "WinSCard", "SW_OK", "sw_text",
           "SCARD_ERR", "_unframe"]
