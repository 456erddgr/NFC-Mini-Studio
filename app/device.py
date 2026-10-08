# -*- coding: utf-8 -*-
"""
设备层（v2.0.0 重写）
====================
职责：把「一帧字节进、一帧字节出」封装成本软件要用的十几条业务命令，
并保证 v2.0.0 的三条硬需求：

  1. 写卡不再赌单一帧格式 —— WRITE_FORMATS 候选格式自适应 + 写入后回读校验；
  2. 读卡顺带识别卡类型（IC/ID/UID/7字节 UID），并据此决定后续动作；
  3. 破解走后台线程池，不阻塞读卡；支持级联多方式与中途取消。

连接通道由 transport 层提供（蓝牙 BLE / USB 串口），本层不区分。
"""
from __future__ import annotations

import threading
import time
import uuid as _uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import (
    CARD_TYPE,
    CMD,
    DEFAULT_KEY,
    ID_FREQ_MAP,
    KEY_TYPE,
    WRITE_FORMATS,
    WRITE_MODES,
    FrameParser,
    NfcError,
    PHONE_WRITE_MODES,
    build_auto_identify_payload,
    build_block_write_payload,
    build_card_no_payload,
    build_check_card_no_payload,
    build_frame,
    build_lock_card_payload,
    build_ufuid_lock_payload,
    build_write_payload,
    build_write_uid_payload,
    classify_card,
    err_text,
    fmt_hex,
    hex_to_bytes,
    norm_fixed_bytes,
    parse_auto_identify,
    parse_block_write_result,
    parse_check_card_no,
    parse_id_card,
    parse_lock_result,
    phone_nfc_plan,
    resolve_write_mode,
    uid_reverse,
)
from .transport import (
    BAUD_CHOICES,                    # noqa: F401  （对外透出，UI 选波特率用）
    DEFAULT_BAUD,
    WIRED_CANDIDATES,
    AsyncRunner,
    BleTransport,
    SerialTransport,
    Transport,
    WiredUsbTransport,
    handshake,
    list_serial_ports,
    list_usb_devices,
    list_wired_devices,
    wired_handshake,
)

# 密钥破解级联顺序（任务书指定）：前一种失败自动切换下一种
CRACK_CHAIN = ["staticnested", "staticnested2", "half_decode", "hardnested",
               "full_decode", "fm11rf08s"]

CRACK_LABELS = {
    "staticnested": "staticnested（静态嵌套）",
    "staticnested2": "staticnested 变体",
    "half_decode": "half_decode（半解）",
    "hardnested": "hardnested（硬嵌套）",
    "full_decode": "full_decode（全卡解码）",
    "fm11rf08s": "FM11RF08S 后门",
}

# 写块响应里这些状态码说明"帧根本没被解析"，此时回读校验没有意义
FRAME_REJECT_CODES = (2, 3, 15)


class AsyncJobRunner:
    """极简后台任务池：给前端一个 handle，前端轮询取结果。

    破解动辄几十秒，必须让读卡/寻卡不被阻塞，所以破解走这里。
    """

    def __init__(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        self._jobs: Dict[str, dict] = {}
        self._lock = threading.RLock()
        self._on_log = on_log

    def _log(self, level: str, text: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, text)
            except Exception:
                pass

    def start(self, name: str, fn: Callable[[Callable[[str, dict], None], Callable[[], bool]], Any],
              meta: Optional[dict] = None) -> str:
        """fn(progress, should_cancel) -> result；返回 job handle。"""
        handle = _uuid.uuid4().hex[:12]
        job = {
            "handle": handle,
            "name": name,
            "state": "running",
            "progress": 0,
            "message": "已启动",
            "result": None,
            "error": "",
            "cancel": False,
            "meta": dict(meta or {}),
            "started": time.time(),
            "finished": None,
        }
        with self._lock:
            self._jobs[handle] = job

        def _progress(pct: int, message: str = "") -> None:
            with self._lock:
                j = self._jobs.get(handle)
                if not j:
                    return
                j["progress"] = max(0, min(100, int(pct)))
                if message:
                    j["message"] = message

        def _cancelled() -> bool:
            with self._lock:
                j = self._jobs.get(handle)
                return bool(j and j["cancel"])

        def _worker() -> None:
            try:
                result = fn(_progress, _cancelled)
                with self._lock:
                    j = self._jobs.get(handle)
                    if not j:
                        return
                    if j["cancel"]:
                        j["state"], j["message"] = "cancelled", "已取消"
                    else:
                        j["state"], j["result"] = "done", result
                        j["progress"] = 100
                        j["message"] = j["message"] or "完成"
            except Exception as exc:                # noqa: BLE001
                with self._lock:
                    j = self._jobs.get(handle)
                    if j:
                        j["state"] = "error"
                        j["error"] = str(exc)
                        j["message"] = str(exc)
            finally:
                with self._lock:
                    j = self._jobs.get(handle)
                    if j:
                        j["finished"] = time.time()

        thread = threading.Thread(target=_worker, name=f"job-{name}", daemon=True)
        thread.start()
        return handle

    def poll(self, handle: str) -> dict:
        with self._lock:
            job = self._jobs.get(handle)
            if not job:
                raise NfcError(f"任务不存在：{handle}")
            out = dict(job)
            out.pop("cancel", None)
            if job["state"] == "running":
                out["elapsed"] = round(time.time() - job["started"], 1)
            elif job["finished"]:
                out["elapsed"] = round(job["finished"] - job["started"], 1)
        return out

    def cancel(self, handle: str) -> dict:
        with self._lock:
            job = self._jobs.get(handle)
            if not job:
                raise NfcError(f"任务不存在：{handle}")
            job["cancel"] = True
            job["message"] = "正在取消…"
        return {"handle": handle, "cancelling": True}

    def active(self) -> List[dict]:
        with self._lock:
            return [{"handle": j["handle"], "name": j["name"], "state": j["state"],
                     "progress": j["progress"], "message": j["message"]}
                    for j in self._jobs.values() if j["state"] == "running"]


class NfcDevice:
    """NFC Mini 2.0 同步门面（蓝牙 / USB 双通道）。"""

    def __init__(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        self._on_log = on_log
        self.jobs = AsyncJobRunner(on_log=on_log)
        self._ble = BleTransport(on_log=on_log)
        self._serial: Optional[SerialTransport] = None
        self._wired: Optional[WiredUsbTransport] = None
        self.transport: Optional[Transport] = None
        self.mode: str = ""                        # "ble" / "usb" / "wired" / ""
        self._parser = FrameParser()
        self._msgid = 0
        self._waiters: Dict[int, "threading.Event"] = {}
        self._payloads: Dict[int, bytes] = {}
        self._lock = threading.RLock()
        self.mac: Optional[str] = None
        self.name: Optional[str] = None
        # 命中过的写块帧格式（同一张卡后续写块直接用，不再逐个试）
        self._write_format: Optional[str] = None
        self._write_format_log: List[str] = []
        self.last_nonces: dict = {}          # 最近一次破解采集结果（供云端解算链路取用）
        self._multi: Dict[int, Callable[[bytes], bool]] = {}   # msgid -> 多包回调(返回 True 结束)
        self._multi_done: Dict[int, bool] = {}
        # P0：最近一次寻卡结果（写卡 / 写手机手环 NFC 需要复用 UID 与卡类型）
        self._last_card: Optional[dict] = None

    # ------------------------------------------------------------ 日志
    def _log(self, level: str, text: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, text)
            except Exception:
                pass

    # ------------------------------------------------------------ 状态
    @property
    def connected(self) -> bool:
        return bool(self.transport and self.transport.connected)

    @property
    def transport_label(self) -> str:
        if not self.transport:
            return "未连接"
        label = getattr(self.transport, "label", "")
        if self.mode == "ble":
            return f"蓝牙 · {self.name or self.mac or label}"
        if self.mode == "wired":
            return f"USB 有线 · {label}"
        return f"USB · {label}" if label else "USB"

    def snapshot(self) -> dict:
        """给前端的状态快照。"""
        wired = self._wired
        return {
            "connected": self.connected,
            "mode": self.mode,
            "transport_label": self.transport_label,
            "mac": self.mac,
            "name": self.name,
            "port": self._serial.port if (self._serial and self.mode == "usb") else "",
            "baud": self._serial.baud if (self._serial and self.mode == "usb") else DEFAULT_BAUD,
            "wired": {
                "vid_pid": (f"{wired.vid:04X}:{wired.pid:04X}" if wired and wired.connected else ""),
                "label": (wired.label if wired and wired.connected else ""),
                "iface": (wired.iface_num if wired and wired.connected else -1),
                "tx_bytes": (wired.tx_bytes if wired else 0),
                "rx_bytes": (wired.rx_bytes if wired else 0),
            },
            "write_format": self._write_format,
            "jobs": self.jobs.active(),
        }

    # ------------------------------------------------------------ 扫描 / 枚举
    def scan(self, timeout: float = 6.0) -> List[dict]:
        """蓝牙扫描（内部会切到 BLE 传输）。"""
        devices = self._ble.scan(timeout)
        return devices

    def list_ports(self) -> dict:
        """USB 串口枚举：自动排除蓝牙虚拟串口。"""
        return {
            "ports": list_serial_ports(include_bluetooth=False),
            "usb_devices": list_usb_devices(),
            "baud_choices": BAUD_CHOICES,
            "default_baud": DEFAULT_BAUD,
        }

    def probe_serial(self, port: str, baud: int = DEFAULT_BAUD) -> dict:
        """握手探测：开机一次串口，发 INFO，看对方是否按本协议回话。"""
        dev = _SerialProbe(port, baud, on_log=self._log)
        try:
            return handshake(dev, timeout=2.5)
        finally:
            dev.disconnect()

    # ------------------------------------------------------------ 连接
    def connect(self, mac: str, timeout: float = 20.0) -> dict:
        self.disconnect()
        self._bind(self._ble)
        self._ble.open(mac=mac, timeout=timeout)
        self.mode = "ble"
        self.mac = mac
        self.name = None
        return self.snapshot()

    def connect_serial(self, port: str, baud: int = DEFAULT_BAUD) -> dict:
        self.disconnect()
        serial = SerialTransport(on_log=self._log)
        self._bind(serial)
        serial.open(port=port, baud=baud)
        self._serial = serial
        self.mode = "usb"
        self.mac = None
        self.name = f"USB {port}"
        return self.snapshot()

    def list_wired(self) -> dict:
        """枚举可用的「USB 有线」设备（libusb 视角，非串口）。"""
        return {
            "devices": list_wired_devices(),
            "candidates": [{"vid": v, "pid": p, "label": l} for v, p, l in WIRED_CANDIDATES],
        }

    def connect_wired(self, vid: int = 0, pid: int = 0, serial: str = "") -> dict:
        self.disconnect()
        wired = WiredUsbTransport(on_log=self._log)
        self._bind(wired)
        wired.open(vid=int(vid or 0), pid=int(pid or 0), serial=str(serial or ""))
        self._wired = wired
        self.mode = "wired"
        self.mac = None
        self.name = wired.describe.get("product") or "USB 有线读卡器"
        return self.snapshot()

    def probe_wired(self, vid: int = 0, pid: int = 0, serial: str = "") -> dict:
        """USB 有线握手探测：开一次通道发 INFO/STATE，看设备是否按协议回话。"""
        probe = NfcDevice(on_log=self._log)
        try:
            probe.connect_wired(vid=vid, pid=pid, serial=serial)
            if probe._wired is not None:
                probe._wired.drain()
            return wired_handshake(probe, timeout=2.5)
        finally:
            probe.disconnect()

    def wired_send_raw(self, frame_hex: str, wait: float = 1.5) -> dict:
        """试发帧：把十六进制原文直接写到 bulk OUT，并在 wait 秒内收集原始回包。"""
        if self.mode != "wired" or self._wired is None or not self._wired.connected:
            raise NfcError("请先连接 USB 有线设备")
        data = hex_to_bytes(frame_hex)
        if not data:
            raise NfcError("请填写要发送的十六进制帧")
        wired = self._wired
        wired.drain()
        mark = wired.raw_count()                   # 记录发送前的日志长度
        started = time.time()
        wired.send(data)
        deadline = started + max(0.2, min(float(wait), 10.0))
        while time.time() < deadline:
            time.sleep(0.1)
        elapsed = round(time.time() - started, 2)
        replies = [x for x in wired.raw_since(mark) if x["dir"] == "rx"]
        self._log("ok" if replies else "warn",
                  f"USB 有线试发：{data.hex(' ').upper()} → "
                  + (f"收到 {len(replies)} 包回包" if replies else "无应答"))
        return {
            "sent_hex": data.hex(" ").upper(),
            "sent_len": len(data),
            "replies": replies,
            "elapsed": elapsed,
            "raw": wired.raw_snapshot(limit=80),
        }

    def wired_raw_log(self, limit: int = 120, clear: bool = False) -> List[dict]:
        if self._wired is None:
            return []
        return self._wired.raw_snapshot(limit=limit, clear=clear)

    def _bind(self, transport: Transport) -> None:
        self._parser.reset()
        self._waiters.clear()
        self._payloads.clear()
        transport.on_data = self._on_data
        self.transport = transport
        self._write_format = None
        self._write_format_log = []

    def disconnect(self) -> None:
        with self._lock:
            for transport in (self._ble, self._serial, self._wired):
                if transport is None:
                    continue
                try:
                    transport.close()
                except Exception:
                    pass
            self.transport = None
            self._serial = None
            self._wired = None
            self.mode = ""
            self._parser.reset()
            self._waiters.clear()
        self._log("info", "已断开连接")

    # ------------------------------------------------------------ 收发
    def _on_data(self, chunk: bytes) -> None:
        """入站字节（可能来自 BLE 通知线程，也可能来自串口读线程）。"""
        try:
            frames = self._parser.feed(bytes(chunk))
        except Exception as exc:                   # noqa: BLE001
            self._log("err", f"帧解析异常：{exc}")
            return
        for mid, payload in frames:
            with self._lock:
                cb = self._multi.get(mid)
            if cb is not None:
                try:
                    if cb(payload):
                        with self._lock:
                            self._multi_done[mid] = True
                except Exception:                      # noqa: BLE001
                    pass
                continue
            with self._lock:
                event = self._waiters.get(mid)
                if event is not None:
                    self._payloads[mid] = payload
            if event is not None:
                event.set()
            else:
                self._log("trace", f"<- 未匹配帧 msgid={mid}: {fmt_hex(payload[:24])}")

    def _next_id(self) -> int:
        self._msgid = self._msgid + 1 if self._msgid < 253 else 1
        return self._msgid

    def request(self, data: bytes, timeout: float = 5.0, retries: int = 0) -> bytes:
        with self._lock:
            if not self.connected:
                raise NfcError("设备未连接，请先连接读卡器")
            mid = self._next_id()
            frame = build_frame(bytes(data), mid)
            event = threading.Event()
            self._waiters[mid] = event
            self._payloads.pop(mid, None)
        try:
            attempt = 0
            while True:
                self._log("trace", f"-> {fmt_hex(frame)}   [{_cmd_label(data)}]")
                try:
                    self.transport.send(frame)
                except NfcError:
                    raise
                except Exception as exc:           # noqa: BLE001
                    raise NfcError(f"发送失败：{exc}") from exc
                if event.wait(timeout):
                    with self._lock:
                        payload = self._payloads.pop(mid, b"")
                    self._log("trace", f"<- {fmt_hex(payload)}")
                    return payload
                if attempt >= retries:
                    raise NfcError(f"等待设备响应超时（{timeout:.0f}s）")
                attempt += 1
                self._log("warn", f"无应答，重试 {attempt}/{retries}")
        finally:
            with self._lock:
                self._waiters.pop(mid, None)

    def request_multi(self, data: bytes, on_packet, timeout: float = 600.0,
                      should_stop=None) -> int:
        """多包请求：同一 msgid 连续回多包时使用（hardnested 采集 256 包）。

        on_packet(payload) 返回 True 表示采集结束。返回收到的包数。
        """
        with self._lock:
            if not self.connected:
                raise NfcError("设备未连接，请先连接读卡器")
            mid = self._next_id()
            frame = build_frame(bytes(data), mid)
            self._multi[mid] = on_packet
            self._multi_done.pop(mid, None)
            count = {"n": 0}

            def wrapped(payload: bytes) -> bool:
                count["n"] += 1
                return bool(on_packet(payload))

            self._multi[mid] = wrapped
        try:
            self._log("trace", f"-> {fmt_hex(frame)}   [{_cmd_label(data)} · 多包]")
            self.transport.send(frame)
            deadline = time.time() + max(1.0, float(timeout))
            while time.time() < deadline:
                if should_stop and should_stop():
                    break
                with self._lock:
                    if self._multi_done.pop(mid, False):
                        break
                time.sleep(0.05)
        finally:
            with self._lock:
                self._multi.pop(mid, None)
                self._multi_done.pop(mid, None)
        return count["n"]

    # ------------------------------------------------------------ 设备信息
    def device_info(self) -> dict:
        d = self.request(bytes([CMD.INFO]), timeout=4.0)
        if len(d) < 16:
            raise NfcError(f"设备信息响应过短（收到 {len(d)} 字节）")
        return {
            "version": ".".join(str(b) for b in d[1:4]),
            "sn": d[4:16].hex().upper(),
            "raw": fmt_hex(d),
            "mac": self.mac,
            "mode": self.mode,
        }

    def device_state(self) -> dict:
        """0x02 设备状态。实测响应形如 02 19 00 00 00 00 01：
        第 2 字节在 1..100 区间时视为电量百分比（对齐小程序「电量显示」开关）。"""
        d = self.request(bytes([CMD.STATE]), timeout=4.0)
        battery = None
        if len(d) >= 2:
            val = int(d[1])
            if 1 <= val <= 100:
                battery = val
        return {"raw": fmt_hex(d), "status": _status_code(d), "status_text": _status_text(d),
                "battery": battery,
                "battery_text": (f"{battery}%" if battery is not None else "—")}

    # ------------------------------------------------------------ 寻卡 / 识卡
    def find_card(self) -> dict:
        """0x11 寻卡：返回卡类型 / UID / SAK，并给出 IC / ID 归类与动作建议。"""
        d = self.request(bytes([CMD.FIND_CARD]), timeout=5.0)
        if not d:
            raise NfcError("寻卡空响应")
        if d[0] != CMD.FIND_CARD:
            raise NfcError(f"寻卡响应命令码不符：0x{d[0]:02X}")
        if len(d) < 3:
            raise NfcError("寻卡响应过短")
        code = int(d[1])
        if code != 0:
            raise NfcError(err_text(code), code)
        if len(d) < 9:
            raise NfcError(f"寻卡响应缺少卡数据（{len(d)} 字节）：{fmt_hex(d)}")

        type_id = int(d[2])
        if type_id == 4 and len(d) >= 12:
            # 7 字节 UID 卡：固件按低字节在前返回，显示时保持原序
            uid = bytes(d[3:10]).hex().upper()
        else:
            uid = uid_reverse(d[3:7])
        sak = int(d[7])
        info = {
            "card_type": CARD_TYPE.get(type_id, f"未知({type_id})"),
            "card_type_id": type_id,
            "uid": uid,
            "uid_len": len(uid) // 2,
            "sak": f"0x{sak:02X}",
            "sak_raw": sak,
            "is_uid_card": bool(d[8]) if len(d) > 8 else False,
            "raw": fmt_hex(d),
        }
        cls = classify_card(type_id, uid, sak)
        info["class"] = cls["class"]
        info["class_label"] = cls["label"]
        info["band"] = cls["band"]
        info["sector_card"] = cls["sector_card"]
        info["crackable"] = cls["crackable"]
        info["writable"] = cls["writable"]
        info["class_source"] = cls["source"]
        info["class_note"] = cls["note"]
        # 换卡后旧的写块格式结论作废
        self._write_format = None
        self._write_format_log = []
        self._last_card = info
        return info

    def detect_card_class(self) -> dict:
        """只做「这是什么卡」的判定，不读扇区，供 UI 秒回。"""
        return self.find_card()

    # ------------------------------------------------------------ 认证 / 读写
    def auth(self, block: int, key: bytes = DEFAULT_KEY, key_type: int = 0,
             timeout: float = 8.0) -> bool:
        key = _as_key(key)
        if not 0 <= int(block) <= 255:
            raise NfcError("块号范围 0..255")
        # 实测 0x0A 是空命令（恒返回 0A 00 00，不校验密钥）；真正的密钥校验是
        # 0x06(KEY A) / 0x09(KEY B)，请求 [cmd, block, key0..5]，状态在 DATA[2]。
        cmd = CMD.KEY_A_CHECK if (int(key_type) & 1) == 0 else CMD.KEY_B_CHECK
        payload = bytes([cmd, int(block) & 0xFF]) + key
        d = self.request(payload, timeout=timeout)
        code = _status_code(d)
        if code != 0:
            raise NfcError(f"{KEY_TYPE.get(key_type, 'KEY')} 认证失败：{err_text(code)}", code)
        return True

    def auth_auto(self, block: int, key: bytes = DEFAULT_KEY,
                  key_type: int = 0, timeout: float = 8.0) -> int:
        """KEY A 失败自动换 KEY B，返回最终认证通过的密钥类型。"""
        order = [int(key_type) & 1, 1 - (int(key_type) & 1)]
        last = ""
        for kt in order:
            try:
                self.auth(block, key, kt, timeout=timeout)
                if kt != order[0]:
                    self._log("warn", f"KEY A 未通过，已自动切换 {KEY_TYPE.get(kt)} 认证成功")
                return kt
            except NfcError as exc:
                last = f"{KEY_TYPE.get(kt)}: {exc}"
                continue
        raise NfcError(f"密钥认证失败（已依次尝试 KEY A / KEY B）：{last}", 8)

    def batch_verify(self, keys, block: int = 0, key_type: int = 0, timeout: float = 4.0,
                     progress=None, should_stop=None, stop_on_hit: bool = True) -> dict:
        """批量验证密钥（对齐小程序「密钥缓存 + 批量验证」0x40/0x41 的功能语义）。

        逐个用 0x0A 认证设备端验证；命中的密钥即该扇区可用密钥。
        比直接下发 0x40 缓存更稳——不依赖固件对批量指令的实现程度。
        """
        keys = list(keys or [])
        hits, tried, errors = [], 0, 0
        total = len(keys)
        for idx, raw in enumerate(keys, start=1):
            if should_stop and should_stop():
                break
            try:
                key = _as_key(raw)
            except Exception:
                continue
            tried += 1
            try:
                self.auth(block, key, key_type, timeout=timeout)
                hits.append({"key": key.hex().upper(), "key_type": int(key_type) & 1,
                             "block": int(block) & 0xFF})
                if stop_on_hit:
                    # 命中即停：官方同款策略，避免为一把密钥把几百把全试一遍
                    break
            except NfcError:
                errors += 1
            if progress:
                try:
                    progress(idx, total, f"验证 {key.hex().upper()}（{idx}/{total}）")
                except Exception:
                    pass
        return {"ok": bool(hits), "hits": hits, "tried": tried, "failed": errors,
                "total": total, "block": int(block) & 0xFF, "key_type": int(key_type) & 1}

    def read_block(self, block: int, key_type: int = 0, timeout: float = 8.0) -> bytes:
        d = self.request(bytes([CMD.READ_BLOCK, int(block) & 0xFF, int(key_type) & 1]),
                         timeout=timeout)
        ok, code, data = _extract_block(d)
        if not ok:
            raise NfcError(err_text(code), code)
        if len(data) < 16:
            raise NfcError(f"读块数据不足 16 字节（收到 {len(data)}）")
        return data

    def read_block_verbose(self, block: int, key_type: int = 0,
                           timeout: float = 8.0) -> bytes:
        d = self.request(bytes([CMD.READ_BLOCK, int(block) & 0xFF, int(key_type) & 1]),
                         timeout=timeout)
        self._last_read_raw = fmt_hex(d)
        ok, code, data = _extract_block(d)
        if not ok:
            raise NfcError(err_text(code), code)
        if len(data) < 16:
            raise NfcError(f"读块数据不足 16 字节（收到 {len(data)}）")
        return data

    # ---- 写块：候选帧格式自适应 + 回读校验 --------------------------------
    def write_block(self, block: int, data, key: bytes = DEFAULT_KEY,
                    key_type: int = 0, verify: bool = True,
                    adaptive: bool = True, progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
        """写单块。

        v1.0.0 只发一种硬编码帧（密钥在前、不认证），一旦固件解析格式不同就必失败。
        v2.0.0 改为：候选格式逐个试 → 写后立刻回读比对 → 命中即缓存。
        """
        block = int(block)
        if not 0 <= block <= 255:
            raise NfcError("块号范围 0..255")
        payload_data = _as_block(data)
        key = _as_key(key)

        formats = self._candidate_formats(adaptive)
        errors: List[str] = []
        tried: List[str] = []
        total = len(formats)
        for idx, fmt in enumerate(formats, start=1):
            if progress:
                progress(idx - 1, total, f"尝试写块格式 {fmt['id']}（{fmt['desc']}）")
            tried.append(fmt["id"])

            if fmt.get("pre_auth"):
                try:
                    self.auth_auto(block, key, key_type)
                except NfcError as exc:
                    errors.append(f"{fmt['id']}: 认证未通过（{exc}）")
                    continue

            status, detail = self._try_send_write(fmt, block, key, key_type, payload_data)
            if status is not None and status in FRAME_REJECT_CODES:
                errors.append(f"{fmt['id']}: 设备未接受该帧（{detail}）")
                continue
            if not verify:
                self._write_format = fmt["id"]
                return {"ok": True, "format": fmt["id"], "format_desc": fmt["desc"],
                        "verified": None, "status_text": detail, "tried": tried,
                        "block": block}

            try:
                back = self.read_block_verbose(block, key_type, timeout=8.0)
            except NfcError as exc:
                errors.append(f"{fmt['id']}: 写后回读失败（{exc}）")
                continue
            if bytes(back) == payload_data:
                if idx > 1:
                    self._log("warn", f"前 {idx - 1} 种写块帧格式无效，"
                                      f"已命中 {fmt['id']}（{fmt['desc']}）")
                self._write_format = fmt["id"]
                return {"ok": True, "format": fmt["id"], "format_desc": fmt["desc"],
                        "verified": True, "read_back": fmt_hex(back),
                        "status_text": detail, "tried": tried, "block": block}
            # 写了但内容不对 —— 先把原内容尽力写回，避免把块写脏
            errors.append(f"{fmt['id']}: 回读不一致（期望 {fmt_hex(payload_data[:4])}… "
                          f"实读 {fmt_hex(back[:4])}…）")
            self._restore_block(block, fmt, key, key_type)

        if not adaptive and self._write_format:
            raise NfcError("写块失败：" + "；".join(errors), 10)
        raise NfcError(f"写块失败，已依次尝试 {len(tried)} 种帧格式：" + "；".join(errors), 10)

    def write_block_verbose(self, block: int, data, key: bytes = DEFAULT_KEY,
                            key_type: int = 0, verify: bool = True) -> dict:
        """写块 + 回读，返回写与读的完整原始帧（排查用）。"""
        result = self.write_block(block, data, key=key, key_type=key_type, verify=verify)
        payload_data = _as_block(data)
        read_raw = ""
        try:
            self.read_block_verbose(block, key_type)
            read_raw = getattr(self, "_last_read_raw", "")
        except NfcError:
            read_raw = ""
        return {
            "write": result,
            "write_raw": getattr(self, "_last_write_raw", ""),
            "read_raw": read_raw,
            "block": int(block),
            "data": fmt_hex(payload_data),
        }

    def _candidate_formats(self, adaptive: bool) -> List[dict]:
        if adaptive and self._write_format:
            hit = [f for f in WRITE_FORMATS if f["id"] == self._write_format]
            if hit:
                # 命中格式优先，其余作为兜底（换卡/换固件后仍能自适应）
                rest = [f for f in WRITE_FORMATS if f["id"] != self._write_format]
                return hit + rest
        return list(WRITE_FORMATS)

    def _try_send_write(self, fmt: dict, block: int, key: bytes, key_type: int,
                        data: bytes) -> Tuple[Optional[int], str]:
        """发一次写块，返回 (状态码, 描述)；无应答返回 (None, "无应答")。"""
        payload = build_write_payload(fmt, block, key_type, key, data)
        msgid_cmd = int(fmt.get("cmd", CMD.WRITE_BLOCK))
        frame_data = bytes([msgid_cmd]) + bytes(payload)
        try:
            d = self.request(frame_data, timeout=12.0)
        except NfcError as exc:
            self._last_write_raw = ""
            return None, f"无应答（{exc}）"
        self._last_write_raw = fmt_hex(d)
        code = _status_code(d)
        if code == 0:
            return 0, "写入指令已被设备接受"
        return code, err_text(code)

    def _restore_block(self, block: int, fmt: dict, key: bytes, key_type: int) -> None:
        """回读不一致时，把原始内容尽力写回（失败仅记日志，不抛）。"""
        try:
            original = self.read_block(block, key_type)
        except NfcError:
            return
        try:
            payload = build_write_payload(fmt, block, key_type, key, original)
            self.request(bytes([int(fmt.get("cmd", CMD.WRITE_BLOCK))]) + bytes(payload),
                         timeout=12.0)
            self._log("warn", f"块 {block} 已尝试回滚为原内容（原块数据 {fmt_hex(original[:4])}…）")
        except NfcError as exc:
            self._log("err", f"块 {block} 回滚失败：{exc}")

    # ---- 整卡读写 --------------------------------------------------------
    def dump_card(self, key: bytes = DEFAULT_KEY, key_type: int = 0,
                  include_trailers: bool = True, total_blocks: int = 64,
                  sector_keys: Optional[Dict[int, Any]] = None,
                  progress: Optional[Callable[[int, int, int], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None) -> dict:
        """整卡读取（默认 16 扇区 × 4 块 = 64 块），逐块回调进度。

        sector_keys：破解得到的"扇区号 -> 密钥"表。读该扇区前先用它认证
        （KEY A 不通自动换 KEY B），这样非默认密钥的块也读得出来；
        没有破解结果的扇区用默认密钥尽力认证一次，认证失败仍然尝试读块。
        """
        total = int(total_blocks)
        blocks: Dict[int, Optional[str]] = {}
        errors: Dict[int, str] = {}
        sector_notes: Dict[int, str] = {}
        done = 0
        started = time.time()
        cur_sector = -1
        for block in range(total):
            if should_stop and should_stop():
                errors[-1] = "用户取消"
                break
            sector = block // 4
            if sector != cur_sector:
                cur_sector = sector
                sk = (sector_keys or {}).get(sector)
                target = _as_key(sk) if sk else _as_key(key)
                try:
                    self.auth_auto(sector * 4, target, key_type)
                    if sk:
                        sector_notes[sector] = f"扇区 {sector} 用破解密钥认证通过"
                except NfcError as exc:
                    sector_notes[sector] = f"扇区 {sector} 认证未通过（{exc}）"
            if not include_trailers and block % 4 == 3:
                done += 1
                if progress:
                    progress(done, total, block)
                continue
            try:
                blocks[block] = fmt_hex(self.read_block(block, key_type))
            except NfcError as exc:
                blocks[block] = None
                errors[block] = str(exc)
            done += 1
            if progress:
                progress(done, total, block)
        ok_count = sum(1 for v in blocks.values() if v)
        return {
            "blocks": blocks,
            "errors": errors,
            "sector_notes": sector_notes,
            "total": total,
            "ok_count": ok_count,
            "elapsed": round(time.time() - started, 2),
        }

    # ------------------------------------------------------------ 破解
    def crack(self, method: str, sector: int = 0, key_type: int = 0,
              timeout: float = 90.0) -> dict:
        cmd = {
            "staticnested": CMD.STATICNEST,
            "staticnested2": CMD.STATICNEST2,
            "half_decode": CMD.HALF_DECODE,
            "halfdecode": CMD.HALF_DECODE,
            "hardnested": CMD.HARDNESTED,
            "fulldecode": CMD.FULL_DECODE,
            "full_decode": CMD.FULL_DECODE,
            "fm11rf08s": CMD.FM11RF08S,
        }.get(method)
        if cmd is None:
            raise NfcError(f"不支持的破解方式：{method}")

        payload = bytes([cmd, int(sector) & 0xFF, int(key_type) & 1])
        started = time.time()
        d = self.request(payload, timeout=timeout)
        cost = round(time.time() - started, 2)
        code = _status_code(d)
        if code != 0:
            raise NfcError(err_text(code), code)
        keys = []
        body = d[2:]
        for i in range(0, len(body) - 5, 6):
            keys.append(fmt_hex(body[i:i + 6]))
        return {
            "method": method,
            "label": CRACK_LABELS.get(method, method),
            "sector": int(sector),
            "keys": keys,
            "elapsed": cost,
            "raw": fmt_hex(d),
        }

    def crack_chain(self, sector: int = 0, key_type: int = 0,
                    methods: Optional[List[str]] = None, timeout: float = 45.0,
                    progress: Optional[Callable[[int, int, str], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None,
                    on_attempt: Optional[Callable[[str, bool, float, str], None]] = None) -> dict:
        """级联破解：按 CRACK_CHAIN 顺序自动切换，前一种失败自动换下一种。

        返回 {ok, method, keys, attempts:[...], elapsed}
        """
        chain = list(methods or CRACK_CHAIN)
        attempts: List[dict] = []
        started = time.time()
        total = len(chain)
        timeouts = 0
        summary = ""
        for idx, method in enumerate(chain, start=1):
            if should_stop and should_stop():
                attempts.append({"method": method, "ok": False, "elapsed": 0.0,
                                 "detail": "用户取消"})
                break
            label = CRACK_LABELS.get(method, method)
            if progress:
                progress(idx, total, f"正在用 {label} 破解扇区 {sector}…")
            t0 = time.time()
            try:
                res = self.crack(method, sector=sector, key_type=key_type, timeout=timeout)
                cost = res["elapsed"]
                if res["keys"]:
                    attempts.append({"method": method, "label": label, "ok": True,
                                     "elapsed": cost, "keys": res["keys"],
                                     "detail": "成功"})
                    if on_attempt:
                        on_attempt(method, True, cost, "成功")
                    return {"ok": True, "method": method, "label": label,
                            "keys": res["keys"], "sector": sector,
                            "attempts": attempts,
                            "elapsed": round(time.time() - started, 2)}
                detail = "未返回密钥"
                attempts.append({"method": method, "label": label, "ok": False,
                                 "elapsed": cost, "detail": detail})
                if on_attempt:
                    on_attempt(method, False, cost, detail)
            except NfcError as exc:
                cost = round(time.time() - t0, 2)
                if should_stop and should_stop():
                    attempts.append({"method": method, "label": label, "ok": False,
                                     "elapsed": cost, "detail": "用户取消"})
                    break
                attempts.append({"method": method, "label": label, "ok": False,
                                 "elapsed": cost, "detail": str(exc)})
                if on_attempt:
                    on_attempt(method, False, cost, str(exc))
                # 同一套指令连续无应答，说明固件根本没实现这些命令，别让用户干等
                if "超时" in str(exc) or "无应答" in str(exc):
                    timeouts += 1
                    if timeouts >= 3:
                        summary = ("连续 3 种破解指令均无应答，固件很可能未实现这些命令，"
                                   "已停止继续尝试")
                        self._log("err", summary)
                        break
                else:
                    timeouts = 0
        return {"ok": False, "method": "", "label": "", "keys": [], "sector": sector,
                "attempts": attempts, "elapsed": round(time.time() - started, 2),
                "summary": summary or "所有破解方式均未成功"}

    # ------------------------------------------------------------ 其它命令
    # ------------------------------------------------------------ 破解采集（喂云端算法）
    # 官方流程：设备采集原始密文 → 上传云端算。这里按小程序 X() 的帧格式还原采集。
    def collect_staticnest(self, block: int = 0, n: int = 0,
                           key_type_a: int = 0, key_type_b: int = 0) -> dict:
        """0x32 staticnest 采集（无漏洞卡）。

        请求 DATA = [0x32, t, n, keyTypeA, keyTypeB]（5 字节，逆向自小程序 X()）。
        响应 e[2]===9 表示采集完成，随后 e[3:7]/e[7:11] 是 enc1/ks1（第二组在 e[11:15]/e[15:19]）。
        """
        payload = bytes([CMD.STATICNEST, int(block) & 0xFF, int(n) & 0xFF,
                         int(key_type_a) & 1, int(key_type_b) & 1])
        d = self.request(payload, timeout=20.0)
        code = _status_code(d)
        if code != 0:
            raise NfcError(f"staticnest 采集失败：{err_text(code)}", code)
        done = len(d) > 2 and int(d[2]) == 9
        def u32(offset):
            if len(d) < offset + 4:
                return 0
            return int.from_bytes(bytes(d[offset:offset + 4]), "big")
        out = {
            "ok": done, "raw": fmt_hex(d), "done_flag": (int(d[2]) if len(d) > 2 else -1),
            "uid": (self._last_card or {}).get("uid", ""),
            "enc1_bytes_0": u32(3), "ks1_bytes_0": u32(7),
            "enc1_bytes_1": u32(11), "ks1_bytes_1": u32(15),
            "cmd": "0x32", "payload": fmt_hex(payload),
        }
        self.last_nonces = out          # 供 api._collect_nonces / 云端链路取用
        self._log("info", f"staticnest 采集：done={done} enc1={out['enc1_bytes_0']:#010x} ks1={out['ks1_bytes_0']:#010x}")
        return out

    def collect_hardnested(self, block: int = 0, key_type_a: int = 0, key_type_b: int = 1,
                           timeout: float = 600.0, progress=None, should_stop=None) -> dict:
        """0x20 hardnested 采集（兼容卡 / 密钥全未知卡的正解）。

        请求 DATA = [0x20, t=block, keyTypeA, n=0, keyTypeB]。
        设备连续回 256 包：e[1]=进度(0..255)、e[2]=状态、e[3:7]+e[7] 为密文；
        进度 255 结束。拼成的文本行整批交云端 /hardnested。
        """
        payload = bytes([CMD.HARDNESTED, int(block) & 0xFF,
                         int(key_type_a) & 1, 0, int(key_type_b) & 1])
        lines: List[str] = []
        state = {"ok": True, "err": "", "progress": 0}

        def on_packet(d: bytes) -> bool:
            if len(d) < 3:
                return False
            prog = int(d[1])
            status = int(d[2])
            if status != 0:
                state["ok"] = False
                state["err"] = err_text(status)
                return True
            if len(d) >= 8:
                lines.append(f"{d[3:7].hex().upper()} 000000{d[7]:02X}")
            state["progress"] = prog
            if progress:
                try:
                    progress(prog, 255, f"hardnested 采集 {prog}/255")
                except Exception:
                    pass
            return prog == 255

        try:
            n = self.request_multi(payload, on_packet, timeout=timeout, should_stop=should_stop)
        except NfcError as exc:
            n = 0
            state["ok"] = False
            state["err"] = str(exc)
        out = {"ok": bool(state["ok"] and lines), "lines": lines, "text": "\n".join(lines),
               "packets": n, "progress": state["progress"], "error": state["err"],
               "uid": (self._last_card or {}).get("uid", ""), "cmd": "0x20",
               "payload": fmt_hex(payload)}
        self.last_nonces = out
        self._log("info" if out["ok"] else "warn",
                  f"hardnested 采集：{n} 包 / {len(lines)} 行" + (f"（{state['err']}）" if state["err"] else ""))
        return out
    def collect_full_decode(self, block: int = 0, key_type: int = 0,
                            timeout: float = 30.0, progress=None) -> dict:
        """0x22 full_decode 采集（pang 全加密卡用，3 包）。

        请求 [0x22, block, keyType]；设备回 3 包，package_number 0→1→2。
        包 0/1 取 uid/nt/nr_1/ks_list，包 2 取 nr_3/ks_list_30..37。
        """
        payload = bytes([CMD.FULL_DECODE, int(block) & 0xFF, int(key_type) & 1])
        packets: List[bytes] = []
        state = {"ok": True, "err": ""}

        def on_packet(d: bytes) -> bool:
            if len(d) < 3:
                return False
            code = int(d[1])
            pkg = int(d[2])
            if code != 0:
                state["ok"] = False
                state["err"] = err_text(code)
                return True
            packets.append(bytes(d))
            if progress:
                try:
                    progress(pkg, 3, f"full_decode 采集包 {pkg}")
                except Exception:
                    pass
            return pkg >= 2 or len(packets) >= 3

        try:
            n = self.request_multi(payload, on_packet, timeout=timeout)
        except NfcError as exc:
            n = 0
            state["ok"] = False
            state["err"] = str(exc)
        # 按官方字段映射组装
        out = {"ok": bool(state["ok"] and packets), "packets": n, "error": state["err"],
               "uid": (self._last_card or {}).get("uid", ""), "cmd": "0x22",
               "payload": fmt_hex(payload), "raw": [fmt_hex(x) for x in packets]}
        if packets:
            def u32(pkt, off):
                return int.from_bytes(bytes(pkt[off:off + 4]), "big") if len(pkt) >= off + 4 else 0
            p0 = packets[0]
            out.update({"uid_i": u32(p0, 3), "nt": u32(p0, 7), "nr_1": u32(p0, 11),
                        "ks_list_0": (int(p0[15]) if len(p0) > 15 else 0),
                        "ks_list_1": (int(p0[16]) if len(p0) > 16 else 0)})
            p2 = packets[-1]
            out.update({"nr_3": u32(p2, 3),
                        "ks_list_30": (int(p2[7]) if len(p2) > 7 else 0),
                        "ks_list_37": (int(p2[14]) if len(p2) > 14 else 0)})
        self.last_nonces = out
        self._log("info" if out["ok"] else "warn",
                  f"full_decode 采集：{n} 包" + (f"（{state['err']}）" if state["err"] else ""))
        return out

    # ------------------------------------------------------------ 侦测读卡（0x34/0x35/0x36）    # ------------------------------------------------------------ 侦测读卡（0x34/0x35/0x36）
    # 对齐小程序 data-sniffing 页：0x34 读初始侦测数据 → 0x35 上报模拟卡参数(UID/SAK/ATQA)
    # → 0x36 开始侦测数据流；仅 sniff:true 的机型支持（NFC Max 2.0 支持）。
    def sniff_init(self) -> dict:
        """0x34 读取初始侦测数据（第一步：放原卡读取）。"""
        d = self.request(bytes([0x34]), timeout=8.0)
        return {"raw": fmt_hex(d), "status": _status_code(d),
                "status_text": _status_text(d), "data": list(d)}

    def sniff_set_card(self, uid, sak: int = 0x08, atqa=b"\x04\x00") -> dict:
        """0x35 上报模拟卡参数：DATA = 35 + UID(最多7B) + SAK(1B) + ATQA(2B)。"""
        if isinstance(uid, str):
            uid = bytes.fromhex(uid.replace(" ", ""))
        uid = bytes(uid or b"")[:7]
        atqa = bytes(atqa)[:2].ljust(2, b"\x00")
        payload = bytes([0x35]) + uid + bytes([int(sak) & 0xFF]) + atqa
        d = self.request(payload, timeout=8.0)
        return {"raw": fmt_hex(d), "status": _status_code(d), "status_text": _status_text(d),
                "payload": fmt_hex(payload)}

    def sniff_start(self) -> dict:
        """0x36 开始侦测数据流（把设备贴近门禁/电梯刷卡器采集）。"""
        d = self.request(bytes([0x36]), timeout=20.0)
        return {"raw": fmt_hex(d), "status": _status_code(d),
                "status_text": _status_text(d), "data": list(d)}

    def sniff_run(self, uid=None, sak: int = 0x08, progress=None) -> dict:
        """完整侦测流程：0x34 初始化 → 0x35 设卡 → 0x36 采集。"""
        steps = []
        if progress:
            progress(1, 3, "读取初始侦测数据（0x34）…")
        init = self.sniff_init(); steps.append({"step": "init", **init})
        if progress:
            progress(2, 3, "上报模拟卡参数（0x35）…")
        setc = self.sniff_set_card(uid, sak); steps.append({"step": "set", **setc})
        if progress:
            progress(3, 3, "开始侦测数据流（0x36）…")
        run = self.sniff_start(); steps.append({"step": "run", **run})
        return {"ok": run.get("status") == 0, "steps": steps,
                "raw": run.get("raw", ""), "uid": (uid or "")}

    def detect_bug_card(self) -> dict:
        d = self.request(bytes([CMD.IS_BUG]), timeout=10.0)
        return {"raw": fmt_hex(d), "is_bug": _status_code(d) == 1,
                "status_text": _status_text(d)}

    def write_uid_card(self, uid=None, verify: bool = False) -> dict:
        """0x27 写 UID / 生成手机手环转接卡。

        · 传入 uid：DATA = 0x27, 0x00, 6 字节 UID（大端），共 8 字节
        · 不传 uid：退化为单字节 0x27（沿用旧固件行为，设备自取当前卡 UID）
        """
        started = time.time()
        if uid:
            payload = build_write_uid_payload(uid)
            text = norm_fixed_bytes(uid, 6, "UID").hex().upper()
        else:
            payload = bytes([CMD.WRITE_UID])
            text = ""
        d = self.request(payload, timeout=12.0)
        code = _status_code(d)
        res = {
            "ok": code == 0, "cmd": "0x27", "payload": fmt_hex(payload),
            "uid": text, "raw": fmt_hex(d), "status": code,
            "status_text": err_text(code), "role": "写 UID / 生成转接卡",
            "elapsed": round(time.time() - started, 2),
        }
        if verify and res["ok"]:
            try:
                card = self.find_card()
                res["card_after"] = card
                cur = str(card.get("uid") or "").replace(" ", "").upper()
                res["verified"] = bool(text) and cur.endswith(text)
                res["verify_text"] = ("回读 UID 一致" if res["verified"]
                                      else f"回读 UID {cur or '—'}（目标 {text or '—'}）")
            except NfcError as exc:
                res["verify_text"] = f"回读校验失败：{exc}"
        if code != 0:
            self._log("err", f"0x27 写 UID / 生成转接卡失败：{err_text(code)}")
        return res

    def write_uid(self) -> dict:
        """兼容旧调用：无参 0x27。"""
        d = self.request(bytes([CMD.WRITE_UID]), timeout=10.0)
        code = _status_code(d)
        return {"raw": fmt_hex(d), "ok": code == 0, "status_text": err_text(code)}

    # ------------------------------------------------------------ P0 写卡流程
    # 命令语义取自微信小程序「宜新 NFC Reader Tool」（wxc3dd33d9d4ae0fd6）：
    #   · 0x0C 写卡     DATA = cmd + 5 字节卡号
    #   · 0x16 极速写块 DATA = cmd, 块号, 数据16
    #   · 0x18 普通写块 DATA = cmd, 块号, 0x00, 数据16
    #   · 0x08 带密钥类型写块 DATA = cmd, 块号, 密钥类型, 数据16
    #   · 0x24 卡号校验 DATA = cmd（单字节），status==1 表示校验通过（**不是写块**）
    #   · 0x26 读 ID 卡 DATA = cmd（单字节），返回卡号 / 频率档位 / 卡代码 / 设施码
    #   · 0x27 写 UID / 生成手机手环转接卡 DATA = cmd, 0x00, UID6
    #   · 0x38 自动识别卡片 DATA = cmd（单字节），返回卡号 / 卡类型 / 卡代码 / 设施码
    #   · 0x17 封卡（UFUID 类魔术卡，单字节，不可逆）
    def read_id_card(self) -> dict:
        """0x26 读 ID 卡 / 低频卡：返回卡号、频率档位（125/250/375/500 kHz）等。"""
        d = self.request(bytes([CMD.READ_ID_CARD]), timeout=8.0)
        info = parse_id_card(d)
        self._log("info", f"读 ID 卡成功：卡号 {info['id_hex']} · {info['freq']} "
                          f"· 卡代码 {info['card_code']} · 设施码 {info['facility_code']}")
        return info

    def read_id_card_raw(self) -> dict:
        """读 ID 卡并回传原始帧（排查用）。"""
        info = self.read_id_card()
        info["cmd"] = "0x26"
        return info

    def write_card_no(self, card_no, verify: bool = True,
                      progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
        """0x0C 写卡：把 5 字节卡号写入 ID 卡 / 中介（转接）卡。

        verify=True 时用 0x26 回读卡号比对，如实回报是否一致，绝不假定成功。
        """
        payload = build_card_no_payload(card_no)
        target = int.from_bytes(bytes(payload[1:6]), "big")
        started = time.time()
        if progress:
            progress(0, 2, f"发送 0x0C 写卡命令（卡号 {bytes(payload[1:6]).hex().upper()}）")
        d = self.request(payload, timeout=12.0)
        code = _status_code(d)
        self._last_write_raw = fmt_hex(d)
        if code != 0:
            raise NfcError(f"写卡失败：{err_text(code)}", code)
        res = {
            "ok": True, "cmd": "0x0C", "payload": fmt_hex(payload),
            "card_no": bytes(payload[1:6]).hex().upper(), "raw": fmt_hex(d),
            "status_text": err_text(code), "verified": None,
            "elapsed": round(time.time() - started, 2),
        }
        if progress:
            progress(1, 2, "写卡指令已被设备接受，正在回读校验…")
        if verify:
            try:
                info = self.read_id_card()
                res["read_back"] = info
                res["verified"] = int(info["id"]) == target
                res["verify_text"] = ("回读卡号一致" if res["verified"]
                                      else f"回读卡号不一致（当前 {info['id_hex']}）")
            except NfcError as exc:
                res["verify_error"] = str(exc)
                res["verify_text"] = f"回读校验失败：{exc}"
        if progress:
            progress(2, 2, "写卡完成")
        return res

    def write_block_mode(self, block: int, data, mode: str = "normal",
                         key: bytes = DEFAULT_KEY, key_type: int = 0,
                         verify: bool = True, pre_auth: bool = True,
                         progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
        """0x16 / 0x18 / 0x08 三档写块（极速 / 普通 / 带密钥类型）。"""
        mode_key, conf = resolve_write_mode(mode)
        block = int(block)
        if not 0 <= block <= 255:
            raise NfcError("块号范围 0..255")
        payload_data = _as_block(data)
        key = _as_key(key)
        payload, timeout = build_block_write_payload(mode_key, block, payload_data,
                                                     key_type=key_type)
        started = time.time()
        if progress:
            progress(0, 3, f"{conf['label']}：认证扇区密钥")
        if pre_auth:
            self.auth_auto(block, key, key_type)
        if progress:
            progress(1, 3, f"{conf['label']}：发送 0x{conf['cmd']:02X} 写第 {block} 块")
        d = self.request(payload, timeout=timeout)
        code = _status_code(d)
        self._last_write_raw = fmt_hex(d)
        if code != 0:
            raise NfcError(f"{conf['label']}失败（第 {block} 块）：{err_text(code)}", code)
        res = {
            "ok": True, "mode": mode_key, "mode_label": conf["label"],
            "cmd": f"0x{conf['cmd']:02X}", "block": block,
            "payload": fmt_hex(payload), "raw": fmt_hex(d),
            "status_text": err_text(code), "verified": None,
            "elapsed": round(time.time() - started, 2),
        }
        if progress:
            progress(2, 3, f"{conf['label']}：回读校验第 {block} 块")
        if verify:
            back = self.read_block_verbose(block, key_type, timeout=8.0)
            res["read_back"] = fmt_hex(back)
            res["verified"] = bytes(back) == payload_data
            res["verify_text"] = ("回读一致" if res["verified"]
                                  else f"回读不一致（实读 {fmt_hex(back[:4])}…）")
            if not res["verified"]:
                self._log("warn", f"{conf['label']}：第 {block} 块回读不一致，"
                                  f"实读 {fmt_hex(back)}")
        if progress:
            progress(3, 3, f"{conf['label']}：第 {block} 块完成")
        return res

    def check_card_no(self, card_no, card: Optional[dict] = None) -> dict:
        """写前卡号校验（A3）：读回目标卡当前卡号，与待写卡号比对。

        · ID 卡  → 0x26 读 ID 卡，比对 40bit 卡号
        · IC/UID → 0x11 寻卡，比对 UID 低位
        """
        target = norm_fixed_bytes(card_no, 5, "卡号")
        target_hex = target.hex().upper()
        target_int = int.from_bytes(target, "big")
        card = card or self._last_card or self.find_card()
        if card.get("class") == "ID":
            info = self.read_id_card()
            current = int(info["id"])
            return {"ok": current == target_int, "source": "0x26 读 ID 卡",
                    "current": info["id_hex"], "target": target_hex,
                    "freq": info.get("freq"), "detail": info}
        uid = str(card.get("uid") or "").replace(" ", "").upper()
        current = uid[-len(target_hex):] if uid else ""
        return {"ok": bool(current) and current == target_hex,
                "source": "0x11 寻卡 UID", "current": uid or "—",
                "target": target_hex, "detail": card}

    def write_card(self, mode: str = "normal", card_no=None, blocks: Optional[dict] = None,
                   verify: bool = True, check_card_no: bool = True,
                   key: bytes = DEFAULT_KEY, key_type: int = 0,
                   progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
        """独立写卡流程（0x0C + 0x16/0x18/0x08，含写前卡号校验）。

        流程：0x11 寻卡判类型 →（可选）写前卡号校验 → 按档位写入：
          · ID 卡      → 0x0C 写 5 字节卡号，写后 0x26 回读比对
          · IC / UID 卡 → 0x16 / 0x18 / 0x08 逐块写入 blocks
        """
        mode_key, conf = resolve_write_mode(mode)
        card = self._last_card or self.find_card()
        cls = card.get("class") or ""

        data_blocks: Dict[int, bytes] = {}
        for k, v in dict(blocks or {}).items():
            raw = "".join(ch for ch in str(v) if ch in "0123456789abcdefABCDEF")
            if len(raw) == 32:
                data_blocks[int(k)] = _as_block(raw)

        steps: List[tuple] = []
        if check_card_no and card_no:
            steps.append(("check", None))
        if cls == "ID" or (not data_blocks and card_no):
            steps.append(("card_no", None))
        else:
            steps.extend(("block", b) for b in sorted(data_blocks))
        if not steps:
            raise NfcError("没有可写入的内容：请填写卡号（ID 卡 / 转接卡）"
                           "或先读取整卡以提供扇区数据（IC 卡）", 15)

        total = len(steps)
        done = 0
        result: Dict[str, Any] = {
            "ok": True, "mode": mode_key, "mode_label": conf["label"],
            "cmd": f"0x{conf['cmd']:02X}", "class": cls,
            "class_label": card.get("class_label"), "uid": card.get("uid"),
            "checked": None, "steps": [], "total": total,
        }

        # ① 写前卡号校验（A3）
        if check_card_no and card_no:
            if progress:
                progress(done, total, "写入前校验卡号…")
            chk = self.check_card_no(card_no, card)
            result["checked"] = chk
            result["steps"].append({"step": "check", "ok": bool(chk["ok"]), "detail": chk})
            if not chk.get("ok"):
                raise NfcError(
                    f"卡号校验未通过：待写 {chk['target']}，当前 {chk['current']}"
                    f"（来源 {chk['source']}）", 11)
            done += 1

        # ② 写入
        if cls == "ID" or (not data_blocks and card_no):
            if progress:
                progress(done, total, "写入卡号（0x0C）…")
            res = self.write_card_no(card_no, verify=verify)
            result["steps"].append({"step": "card_no", "ok": True, "detail": res})
            result["card_no"] = res.get("card_no")
            result["verified"] = res.get("verified")
            result["verify_text"] = res.get("verify_text")
            done += 1
        else:
            for b in sorted(data_blocks):
                if progress:
                    progress(done, total, f"{conf['label']}：写入第 {b} 块…")
                res = self.write_block_mode(b, data_blocks[b], mode_key, key, key_type,
                                            verify=verify, pre_auth=True)
                result["steps"].append({"step": "block", "block": b, "ok": True,
                                        "detail": res})
                if res.get("verified") is False:
                    result["ok"] = False
                done += 1
            result["verified"] = all(s["detail"].get("verified") is not False
                                     for s in result["steps"] if s["step"] == "block")

        if progress:
            progress(total, total, "写卡完成")
        self._log("ok", f"写卡完成：{conf['label']}"
                        f"（{total} 步，卡类型 {card.get('class_label') or '—'}）")
        return result

    # ------------------------------------------------------------ 0x24 / 0x17 / 0x38
    def card_no_valid(self, expected=None) -> dict:
        """0x24 卡号校验（设备侧单字节指令）。

        与 :meth:`check_card_no` 的区别：本方法让**读卡器自己**比对当前卡号，
        不需要主机先读卡；expected 仅用于生成可读文案。
        status==1 表示校验通过，2=未知，7=卡片不符 / 寻卡失败。
        """
        payload = build_check_card_no_payload()
        d = self.request(payload, timeout=8.0)
        res = parse_check_card_no(d)
        res.update({"cmd": "0x24", "payload": fmt_hex(payload), "raw": fmt_hex(d)})
        if expected is not None:
            res["expected"] = norm_fixed_bytes(expected, 5, "卡号").hex().upper()
        self._log("ok" if res["ok"] else "warn",
                  f"0x24 卡号校验：{'通过' if res['ok'] else '未通过'}"
                  f"（status={res['status']}）")
        return res

    def lock_ufuid(self, verify: bool = True) -> dict:
        """0x17 封卡（UFUID / 类魔术卡封卡）。

        ⚠ 不可逆：封卡后该卡不能再被改写，请仅在明确知晓后果时调用。
        """
        payload = build_ufuid_lock_payload()
        started = time.time()
        d = self.request(payload, timeout=12.0)
        res = parse_lock_result(d, CMD.UFUID_LOCK)
        res.update({"payload": fmt_hex(payload), "raw": fmt_hex(d),
                    "elapsed": round(time.time() - started, 2)})
        if verify:
            try:
                card = self.find_card()
                res["card_after"] = card
                res["verify_text"] = (f"封卡后仍可寻卡：{card.get('class_label') or '—'}"
                                      f"（UID {card.get('uid') or '—'}）")
            except NfcError as exc:
                res["verify_text"] = f"封卡后寻卡异常：{exc}"
        self._log("warn" if res["ok"] else "err",
                  f"0x17 封卡：{'完成（不可逆）' if res['ok'] else res['err']}")
        return res

    def lock_card(self) -> dict:
        """0x23 锁卡（不可逆）。"""
        payload = build_lock_card_payload()
        d = self.request(payload, timeout=12.0)
        res = parse_lock_result(d, CMD.LOCK_CARD)
        res.update({"payload": fmt_hex(payload), "raw": fmt_hex(d)})
        self._log("warn" if res["ok"] else "err",
                  f"0x23 锁卡：{'完成（不可逆）' if res['ok'] else res['err']}")
        return res

    def auto_identify(self) -> dict:
        """0x38 自动识别卡片（单字节指令）。

        返回卡号 / 卡类型 / 卡代码 / 设施码，并把结果并入 ``_last_card`` 缓存，
        供后续 0x0C 写卡号、0x26 回读等流程复用。
        """
        payload = build_auto_identify_payload()
        d = self.request(payload, timeout=10.0)
        info = parse_auto_identify(d)
        info.update({"cmd": "0x38", "payload": fmt_hex(payload), "raw": fmt_hex(d)})
        self._last_card = {
            "class": "ID", "class_label": info["card_type"],
            "card_type": info["card_type"], "uid": info["id_hex"],
            "card_no": info["id_hex"], "crackable": False,
            "class_note": "0x38 自动识别：低频 ID 卡，无密钥概念",
            "source": "0x38 自动识别",
        }
        self._log("info", f"0x38 自动识别：卡号 {info['id_hex']} · {info['card_type']} "
                          f"· 卡代码 {info['card_code']} · 设施码 {info['facility_code']}")
        return info

    # ------------------------------------------------------------ 手机 / 手环 NFC 写录
    def phone_nfc_plan(self, modes=None) -> List[dict]:
        """手机 / 手环 NFC 写录步骤清单（透出给前端展示）。"""
        return phone_nfc_plan(modes or PHONE_WRITE_MODES)

    def write_phone_nfc(self, uid=None, card_no=None, blocks: Optional[dict] = None,
                        mode: str = "fast", verify: bool = True,
                        check_card_no: bool = True, key: bytes = DEFAULT_KEY,
                        key_type: int = 0, prep_only: bool = False,
                        progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
        """写录手机 / 手环 NFC（转接卡两步法）。

        与「写 UID 卡 / 魔术卡」「封卡」严格区分：
          · 本流程 = 转接卡两步法：0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡
            （写 UID 8 字节）→ 0x16/0x18 写 IC 数据 → 0x07 回读校验；
            手机 / 手环贴近转接卡即可读到该卡号。
          · 写 UID 卡 / 魔术卡 = 直接改写卡片 UID（:meth:`write_uid_card`，0x27）。
          · 封卡 = 0x17 单字节（:meth:`lock_ufuid`，不可逆）。

        prep_only=True 时只做到「生成转接卡」为止（对应小程序 card-write 页的第一步）。
        """
        card = self._last_card or self.find_card()
        cls = card.get("class") or ""
        if cls == "ID" and not card_no:
            raise NfcError("当前是 ID 卡：请填写要模拟的卡号，或改用「读 ID 卡」复制", 15)

        data_blocks: Dict[int, bytes] = {}
        for k, v in dict(blocks or {}).items():
            raw = "".join(ch for ch in str(v) if ch in "0123456789abcdefABCDEF")
            if len(raw) == 32:
                data_blocks[int(k)] = _as_block(raw)

        steps: List[tuple] = []
        if card_no:
            if check_card_no:
                steps.append(("verify_no", None))
            steps.append(("card_no", None))
        steps.append(("bridge", None))
        if not prep_only:
            steps.extend(("block", b) for b in sorted(data_blocks))

        total = len(steps)
        done = 0
        result: Dict[str, Any] = {
            "ok": True, "flow": "phone_nfc", "prep_only": bool(prep_only),
            "class": cls, "class_label": card.get("class_label"),
            "uid": card.get("uid"), "steps": [], "total": total,
            "plan": self.phone_nfc_plan([mode] if prep_only else None),
        }

        def _tick(label: str) -> None:
            if progress:
                progress(done, total, label)

        # ① 写前卡号校验（0x24 设备侧 + 0x26 回读双保险）
        if card_no and check_card_no:
            _tick("写入前校验卡号…")
            chk_cmd = None
            try:
                chk_cmd = self.card_no_valid(card_no)
            except NfcError as exc:
                chk_cmd = {"ok": False, "error": str(exc), "cmd": "0x24"}
            chk = self.check_card_no(card_no, card)
            result["checked"] = {"cmd": chk_cmd, "read_back": chk}
            result["steps"].append({"step": "check", "ok": bool(chk["ok"]),
                                    "detail": result["checked"]})
            if not chk.get("ok"):
                raise NfcError(f"卡号校验未通过：待写 {chk['target']}，"
                               f"当前 {chk['current']}（来源 {chk['source']}）", 11)
            done += 1

        # ② 0x0C 写卡号（转接卡承载的手机卡号）
        if card_no:
            _tick("写入卡号（0x0C）…")
            res = self.write_card_no(card_no, verify=verify)
            result["steps"].append({"step": "card_no", "ok": True, "detail": res})
            result["card_no"] = res.get("card_no")
            result["verified"] = res.get("verified")
            result["verify_text"] = res.get("verify_text")
            if res.get("verified") is False:
                result["ok"] = False
            done += 1

        # ③ 0x27 生成转接卡（写 UID 8 字节：cmd, 0x00, UID6）
        _tick("生成手机 / 手环转接卡（0x27）…")
        bridge = self.write_uid_card(uid=uid, verify=False)
        bridge["role"] = "转接卡（供手机 / 手环读取，非改写目标卡 UID）"
        result["steps"].append({"step": "bridge", "ok": bool(bridge.get("ok")),
                                "detail": bridge})
        result["bridge"] = bridge
        if not bridge.get("ok"):
            result["ok"] = False
            result["verify_text"] = bridge.get("status_text") or "转接卡生成失败"
        done += 1

        # ④ 0x16 / 0x18 写 IC 数据
        if not prep_only:
            mode_key, conf = resolve_write_mode(mode)
            if mode_key not in PHONE_WRITE_MODES:
                raise NfcError(f"手机 NFC 档位仅支持 fast / normal，当前 {mode}", 15)
            result["mode"] = mode_key
            result["mode_label"] = conf["label"]
            result["cmd"] = f"0x{conf['cmd']:02X}"
            if not data_blocks:
                result["warning"] = "未提供扇区数据：仅完成「卡号 + 转接卡」，手机端为空卡"
            block_ok = True
            for b in sorted(data_blocks):
                _tick(f"{conf['label']}：写入第 {b} 块…")
                res = self.write_block_mode(b, data_blocks[b], mode_key, key, key_type,
                                            verify=verify, pre_auth=True)
                result["steps"].append({"step": "block", "block": b, "ok": True,
                                        "detail": res})
                if res.get("verified") is False:
                    block_ok = False
                done += 1
            if data_blocks:
                result["verified"] = block_ok
                if not block_ok:
                    result["ok"] = False
                    result["verify_text"] = "部分数据块回读不一致"

        if progress:
            progress(total, total, "手机 / 手环 NFC 写录完成")
        self._log("ok", f"手机 / 手环 NFC 写录完成（{'仅转接卡' if prep_only else '含数据'}，"
                        f"{total} 步，卡类型 {card.get('class_label') or '—'}）")
        return result

    # ------------------------------------------------------------ 一键破解扫描
    def crack_scan(self, auto_crack: bool = True, sector: int = 0, key_type: int = 0,
                   method: str = "", progress: Optional[Callable[[int, int, str], None]] = None,
                   steps_total: int = 3) -> dict:
        """自动识别卡片 / 一键破解扫描（0x38 自动识别 + 0x11 寻卡兜底 + 分派）。

        与老流程的差异：老流程只有「连接后自动寻卡」，没有独立的一键入口，
        也**从未调用 0x38 自动识别**。本方法：
          1. 优先 0x38 自动识别卡片（低频 ID 卡一步出卡号 / 卡类型）；
          2. 0x38 失败或返回非 ID 卡时，回落到 0x11 寻卡判类型；
          3. 依据卡类型给出破解建议（IC 卡可破解 → 建议后台级联破解；
             ID 卡无密钥概念 → 跳过）。
        是否真的启动破解由 api 层按 auto_crack 决定，本层只做识别与判定。
        """
        if progress:
            progress(0, steps_total, "发送 0x38 自动识别卡片…")
        identified = None
        identify_error = None
        try:
            identified = self.auto_identify()
        except NfcError as exc:
            identify_error = str(exc)
            self._log("warn", f"0x38 自动识别未命中（{exc}），回落 0x11 寻卡")

        card = None
        if identified is None:
            if progress:
                progress(1, steps_total, "0x38 未命中，回落 0x11 寻卡…")
            card = self.find_card()
        else:
            card = self._last_card

        cls = (card or {}).get("class") or ""
        crackable = bool((card or {}).get("crackable"))
        if identified is not None:
            reason = "0x38 识别为 ID 卡：无密钥概念，无需破解"
            recommend = False
        elif crackable:
            recommend = True
            reason = (f"{card.get('class_label') or card.get('card_type') or 'IC 卡'}"
                      f" 可跑级联破解（目标扇区 {sector} / KEY {'B' if key_type else 'A'}）")
        else:
            recommend = False
            reason = f"卡类型 {(card or {}).get('class_label') or '未知'}：不支持密钥破解"

        if progress:
            progress(steps_total, steps_total, "识别完成")
        res = {
            "ok": True, "identified": identified is not None,
            "source": "0x38 自动识别" if identified is not None else "0x11 寻卡",
            "identify_error": identify_error,
            "card": card, "id_card": identified, "class": cls,
            "crackable": crackable, "crack_recommended": recommend,
            "crack_reason": reason, "auto_crack": bool(auto_crack),
            "sector": sector, "key_type": key_type, "method": method,
        }
        self._log("ok", f"自动识别完成：{res['source']} → "
                        f"{(card or {}).get('class_label') or '—'}（{reason}）")
        return res

    # ------------------------------------------------------------ 后台任务
    def start_job(self, name: str, fn, meta: Optional[dict] = None) -> str:
        return self.jobs.start(name, fn, meta=meta)

    def job_status(self, handle: str) -> dict:
        return self.jobs.poll(handle)

    def cancel_job(self, handle: str) -> dict:
        return self.jobs.cancel(handle)


class _SerialProbe(NfcDevice):
    """轻量探针：复用 NfcDevice 的握手能力，但不影响主设备状态。"""

    def __init__(self, port: str, baud: int, on_log=None) -> None:
        super().__init__(on_log=on_log)
        self.connect_serial(port, baud)


# ---------------------------------------------------------------- 内部工具
def _as_key(key) -> bytes:
    if isinstance(key, (bytes, bytearray)):
        raw = bytes(key)
    else:
        raw = hex_to_bytes(str(key))
    if len(raw) != 6:
        raise NfcError(f"密钥必须为 6 字节，当前 {len(raw)} 字节")
    return raw


def _as_block(data) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raw = hex_to_bytes(str(data))
    if len(raw) != 16:
        raise NfcError(f"块数据必须为 16 字节，当前 {len(raw)} 字节")
    return raw


def _status_code(d: bytes) -> int:
    """取响应状态码：按命令查 STATUS_INDEX 定位（0x07/0x08/0x0B/0x20 等在 DATA[2]，
    其余在 DATA[1]）。这条下标表逆向自小程序 dispatch 表，修掉"写块状态读错字节"的老毛病。"""
    if not d or len(d) < 2:
        return 2
    try:
        from .protocol import status_index
        idx = status_index(d[0])
    except Exception:
        idx = 1
    if idx >= len(d):
        idx = 1
    return int(d[idx])


def _status_text(d: bytes) -> str:
    return err_text(_status_code(d))


def _extract_block(d: bytes) -> Tuple[bool, int, bytes]:
    """从读块响应里取 16 字节数据，兼容两种可能的布局。

    布局 A：[cmd, err, data16]      —— 共 18 字节
    布局 B：[cmd, ?, err, data16]   —— 共 19 字节（v1.0.0 的假设）
    两种都不成立时按错误码解释，绝不硬凑数据。
    """
    n = len(d)
    if n == 18 and d[1] == 0:
        return True, 0, bytes(d[2:18])
    if n == 19 and d[2] == 0:
        return True, 0, bytes(d[3:19])
    if n >= 18 and d[1] == 0:
        return True, 0, bytes(d[2:18])
    if n >= 19 and d[2] == 0:
        return True, 0, bytes(d[3:19])
    if n >= 17 and d[1] == 0:
        return True, 0, bytes(d[-16:])
    return False, _status_code(d), b""


def _cmd_label(data: bytes) -> str:
    from .protocol import CMD_NAME

    if not data:
        return "空帧"
    return CMD_NAME.get(data[0], f"命令 0x{data[0]:02X}")
