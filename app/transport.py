# -*- coding: utf-8 -*-
"""
连接传输层（v2.0.0 新增）
=========================
把「怎么把一帧字节送出去、怎么把字节收回来」从业务逻辑里剥出来，
让上层 NfcDevice 同时支持两条通道：

    · BleTransport    —— 蓝牙 BLE（GATT 透传，FFE0/FFE1/FFE2）
    · SerialTransport —— USB 有线串口（同一套 FF…FE 帧直接走 UART）

工程约束：bleak 是 asyncio 风格 API，pywebview 的 JS API 跑在同步线程上，
所以 BLE 侧用独立后台线程跑事件循环（AsyncRunner），对外暴露同步接口；
串口侧用常驻读线程把字节回调上来。两条通道的入站数据都统一交给
NfcDevice 的帧解析器，业务层完全无感。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional

from .protocol import NfcError, fmt_hex

# ---------------------------------------------------------------- BLE 常量
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# 名字里带这些关键字，或广播里带 FFE0 服务的，判为读卡器候选
NAME_HINTS = ("nfc", "rfid", "mini", "reader", "icread", "mfrc", "pn532")

# ---------------------------------------------------------------- 串口常量
# 蓝牙虚拟串口（系统里的"标准串行 over 蓝牙链接"）要从 USB 列表里剔掉，
# 否则用户会对着一个连不上读卡器的假端口猛点连接。
BT_PORT_HINTS = ("bth", "bluetooth", "bluetooth spp", "蓝牙", "bthenum", "bcbtums")

# USB 转串口芯片特征：命中这些说明是"真插着线"的串口
USB_CHIP_HINTS = (
    "ch340", "ch341", "ch342", "ch343", "cp210", "cp2102", "cp2105", "ft232",
    "ft231", "ftdi", "silicon labs", "silabs", "usb-serial", "usb serial",
    "usb 串行", "usb 增强型", "prolific", "pl2303", "esp32", "espressif",
    "usb acm", "acm", "cdc",
)

BAUD_CHOICES = [115200, 9600, 57600, 230400, 460800, 921600]
DEFAULT_BAUD = 115200


# ================================================================ 串口枚举
def _powershell_json(script: str, timeout: float = 8.0) -> Any:
    """跑一段 PowerShell 并解析 JSON 输出；失败返回 None（绝不抛）。"""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def list_usb_devices() -> List[dict]:
    """枚举本机 USB / 端口类 PnP 设备（用于设备页展示"电脑认到了什么"）。

    纯展示用途：拿不到就返回空列表，不影响串口列表本身。
    """
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPClass -eq 'Ports' -or $_.PNPClass -eq 'USB' -or "
        "$_.PNPClass -eq 'USBDevice' } | "
        "Select-Object Name,DeviceID,PNPClass,Status | ConvertTo-Json -Compress"
    )
    data = _powershell_json(script)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    items: List[dict] = []
    for row in data or []:
        try:
            name = str(row.get("Name") or "")
            dev_id = str(row.get("DeviceID") or "")
            blob = (name + " " + dev_id).lower()
            if any(h in blob for h in BT_PORT_HINTS):
                continue
            items.append({
                "name": name,
                "device_id": dev_id,
                "class": str(row.get("PNPClass") or ""),
                "status": str(row.get("Status") or ""),
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["name"])
    return items[:60]


def list_serial_ports(include_bluetooth: bool = False) -> List[dict]:
    """枚举本机串口；默认剔除蓝牙虚拟串口，并按"像不像读卡器"排序。"""
    try:
        from serial.tools import list_ports as _lp
    except Exception as exc:                       # noqa: BLE001
        raise NfcError(f"缺少 pyserial，无法枚举串口（{exc}）") from exc

    items: List[dict] = []
    for p in _lp.comports():
        try:
            name = str(getattr(p, "device", "") or "")
            desc = str(getattr(p, "description", "") or "")
            hwid = str(getattr(p, "hwid", "") or "")
            manu = str(getattr(p, "manufacturer", "") or "")
        except Exception:
            continue
        blob = " ".join(x.lower() for x in (name, desc, hwid, manu))
        is_bt = any(h in blob for h in BT_PORT_HINTS)
        if is_bt and not include_bluetooth:
            continue
        is_usb = any(h in blob for h in USB_CHIP_HINTS)
        items.append({
            "port": name,
            "description": desc,
            "hwid": hwid,
            "manufacturer": manu,
            "is_bluetooth": is_bt,
            "is_usb": is_usb,
            "likely_reader": (not is_bt) and is_usb,
        })
    items.sort(key=lambda d: (not d["likely_reader"], not d["is_usb"], d["port"]))
    return items


# ================================================================ 后台事件循环
class AsyncRunner:
    """后台线程 + 常驻事件循环，提供同步调用入口（BLE 侧用）。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ble-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout: Optional[float] = None) -> Any:
        """同步等待协程完成（超时抛 TimeoutError）。"""
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def spawn(self, coro) -> Future:
        """投递协程但不等待，返回 concurrent Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def shutdown(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


# ================================================================ 传输基类
class Transport:
    """统一接口：open / close / send + on_data(入站字节) 回调。"""
    kind = "?"
    label = "未连接"

    def __init__(self) -> None:
        self.on_data: Optional[Callable[[bytes], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None

    def _log(self, level: str, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, text)
            except Exception:
                pass

    @property
    def connected(self) -> bool:                   # pragma: no cover - 抽象
        raise NotImplementedError

    def open(self, **kw) -> None:                  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:                       # pragma: no cover - 抽象
        raise NotImplementedError

    def send(self, frame: bytes) -> None:          # pragma: no cover - 抽象
        raise NotImplementedError


# ---------------------------------------------------------------- 蓝牙
class BleTransport(Transport):
    kind = "ble"

    def __init__(self, runner: Optional[AsyncRunner] = None,
                 on_log: Optional[Callable[[str, str], None]] = None) -> None:
        super().__init__()
        self._runner = runner
        self._client = None
        self.mac: Optional[str] = None
        self.name: Optional[str] = None
        self.on_log = on_log

    @property
    def runner(self) -> AsyncRunner:
        """懒加载：只用 USB 时不必额外起一个蓝牙事件循环线程。"""
        if self._runner is None:
            self._runner = AsyncRunner()
        return self._runner

    @property
    def connected(self) -> bool:
        try:
            return bool(self._client and self._client.is_connected)
        except Exception:
            return False

    @property
    def label(self) -> str:
        return f"蓝牙 {self.name or self.mac or ''}".strip()

    async def _scan(self, timeout: float) -> List[dict]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        result: List[dict] = []
        for address, pair in found.items():
            dev, adv = pair
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            name = adv.local_name or dev.name or ""
            likely = SERVICE_UUID in uuids or any(h in name.lower() for h in NAME_HINTS)
            result.append({
                "address": address,
                "name": name or "(未知设备)",
                "rssi": getattr(adv, "rssi", -127),
                "likely_reader": likely,
                "services": uuids[:8],
                "transport": "ble",
            })
        result.sort(key=lambda d: (not d["likely_reader"], -d["rssi"]))
        return result

    def scan(self, timeout: float = 6.0) -> List[dict]:
        self._log("info", f"开始扫描 BLE 设备，持续 {timeout:.0f} 秒…")
        devices = self.runner.call(self._scan(timeout), timeout=timeout + 15)
        self._log("ok", f"扫描完成，发现 {len(devices)} 个设备")
        return devices

    async def _open(self, mac: str, timeout: float) -> None:
        from bleak import BleakClient

        await self._close()
        client = BleakClient(mac, timeout=timeout)
        await client.connect()
        await client.start_notify(NOTIFY_UUID, self._on_notify)
        self._client = client
        self.mac = mac

    async def _close(self) -> None:
        if self._client:
            try:
                if self._client.is_connected:
                    await self._client.stop_notify(NOTIFY_UUID)
                    await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def open(self, mac: str = "", timeout: float = 20.0, **kw) -> None:
        mac = str(mac or "").strip()
        if not mac:
            raise NfcError("缺少蓝牙 MAC 地址")
        self._log("info", f"连接 {mac} …")
        self.runner.call(self._open(mac, timeout), timeout=timeout + 10)
        self._log("ok", f"已连接 {mac}")

    def close(self) -> None:
        self.runner.call(self._close(), timeout=15)

    def _on_notify(self, _sender, data: bytearray) -> None:
        if self.on_data:
            try:
                self.on_data(bytes(data))
            except Exception as exc:               # noqa: BLE001
                self._log("err", f"BLE 数据回调异常：{exc}")

    def send(self, frame: bytes) -> None:
        if not self.connected:
            raise NfcError("设备未连接")
        client = self._client

        async def _write():
            await client.write_gatt_char(WRITE_UUID, frame, response=False)

        self.runner.call(_write(), timeout=8)


# ---------------------------------------------------------------- USB 串口
class SerialTransport(Transport):
    kind = "usb"

    def __init__(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        super().__init__()
        self.port: str = ""
        self.baud: int = DEFAULT_BAUD
        self._ser = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.on_log = on_log
        self.rx_bytes = 0
        self.tx_bytes = 0

    @property
    def connected(self) -> bool:
        try:
            return bool(self._ser and self._ser.is_open)
        except Exception:
            return False

    @property
    def label(self) -> str:
        return f"USB {self.port}@{self.baud}" if self.port else "USB 未连接"

    def open(self, port: str = "", baud: int = DEFAULT_BAUD, **kw) -> None:
        try:
            import serial
        except Exception as exc:                   # noqa: BLE001
            raise NfcError(f"缺少 pyserial，无法使用 USB 连接（{exc}）") from exc

        port = str(port or "").strip()
        if not port:
            raise NfcError("缺少串口号")
        self.close()
        self._log("info", f"打开串口 {port} @ {baud} …")
        try:
            self._ser = serial.Serial(port=port, baudrate=int(baud),
                                      bytesize=8, parity="N", stopbits=1,
                                      timeout=0.05, write_timeout=2.0)
        except Exception as exc:                   # noqa: BLE001
            self._ser = None
            raise NfcError(f"打开串口失败：{exc}") from exc
        self.port, self.baud = port, int(baud)
        self.rx_bytes = self.tx_bytes = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="serial-rx",
                                        daemon=True)
        self._thread.start()
        self._log("ok", f"串口已打开 {self.label}")

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _reader(self) -> None:
        while not self._stop.is_set():
            ser = self._ser
            if ser is None:
                break
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting or 1)
            except Exception as exc:               # noqa: BLE001
                if not self._stop.is_set():
                    self._log("err", f"串口读取中断：{exc}")
                break
            if chunk:
                self.rx_bytes += len(chunk)
                if self.on_data:
                    try:
                        self.on_data(bytes(chunk))
                    except Exception as exc:       # noqa: BLE001
                        self._log("err", f"串口数据回调异常：{exc}")

    def send(self, frame: bytes) -> None:
        if not self.connected:
            raise NfcError("串口未打开")
        try:
            self._ser.write(bytes(frame))
            self._ser.flush()
            self.tx_bytes += len(frame)
        except Exception as exc:                   # noqa: BLE001
            raise NfcError(f"串口写入失败：{exc}") from exc


# ================================================================ USB 有线（厂商自定义类 bulk）
# 实测硬件：VID_0483 / PID_66AA，USB 描述「佳点V1PRO」，interface0 为厂商自定义类
# （bInterfaceClass=0xFF），bulk OUT 0x01 / bulk IN 0x81，单包 64 字节；
# interface1 是 HID 键盘接口（不用）。本类走 libusb(WinUSB) 直接收发，与串口通道并列。
WIRED_CANDIDATES = [(0x0483, 0x66AA, "佳点 V1PRO（NFC Mini 2.0 有线）")]
WIRED_EP_SIZE = 64
# 通用兜底枚举时要剔除的接口类：大容量存储(0x08) / 无线蓝牙(0xE0) —— 明显不是读卡器
_BLOCKED_IFACE_CLASSES = {0x08, 0xE0}
WIRED_RAW_LOG_MAX = 400


def _usb_backend():
    """优先用 libusb_package 自带 DLL，退化到系统 libusb；都拿不到返回 None。"""
    try:
        import libusb_package
        import usb.backend.libusb1 as libusb1_backend
        return libusb1_backend.get_backend(find_library=libusb_package.find_library)
    except Exception:
        pass
    try:
        import usb.backend.libusb1 as libusb1_backend
        return libusb1_backend.get_backend()
    except Exception:
        return None


def _usb_modules():
    try:
        import usb.core
        import usb.util
        return usb.core, usb.util
    except Exception as exc:                       # noqa: BLE001
        raise NfcError(f"缺少 pyusb，无法使用 USB 有线通道（{exc}）") from exc


def _describe_usb(dev) -> dict:
    """把一个 pyusb 设备描述成前端可展示的字典（全程不抛异常）。"""
    vid, pid = int(dev.idVendor), int(dev.idProduct)
    info: Dict[str, Any] = {
        "vid": vid, "pid": pid, "vid_pid": f"{vid:04X}:{pid:04X}",
        "manufacturer": "", "product": "", "serial": "", "label": "",
        "known": False, "interfaces": [], "bulk_out": 0, "bulk_in": 0,
        "ep_size": WIRED_EP_SIZE,
    }
    for key, getter in (("manufacturer", "manufacturer"), ("product", "product"),
                        ("serial", "serial_number")):
        try:
            info[key] = str(getattr(dev, getter) or "")
        except Exception:
            info[key] = ""
    for vid_k, pid_k, label in WIRED_CANDIDATES:
        if (vid, pid) == (vid_k, pid_k):
            info["known"] = True
            info["label"] = label
    if not info["label"]:
        info["label"] = info["product"] or info["vid_pid"]

    try:
        cfg = dev.get_active_configuration()
    except Exception:
        try:
            cfg = dev[0]
        except Exception:
            cfg = None
    if cfg is not None:
        try:
            for intf in cfg:
                ifaces = {
                    "num": int(intf.bInterfaceNumber),
                    "class": int(intf.bInterfaceClass),
                    "sub": int(intf.bInterfaceSubClass),
                    "proto": int(intf.bInterfaceProtocol),
                    "vendor": int(intf.bInterfaceClass) == 0xFF,
                    "endpoints": [],
                }
                for ep in intf:
                    is_in = bool(ep.bEndpointAddress & 0x80)
                    kind = {0: "ctrl", 1: "iso", 2: "bulk", 3: "int"}.get(
                        int(ep.bmAttributes), "?")
                    ifaces["endpoints"].append({
                        "addr": int(ep.bEndpointAddress), "kind": kind,
                        "dir": "in" if is_in else "out",
                        "size": int(ep.wMaxPacketSize),
                    })
                    if kind == "bulk":
                        if is_in and not info["bulk_in"]:
                            info["bulk_in"] = int(ep.bEndpointAddress)
                        elif not is_in and not info["bulk_out"]:
                            info["bulk_out"] = int(ep.bEndpointAddress)
                info["interfaces"].append(ifaces)
        except Exception:
            pass
    return info


def list_wired_devices() -> List[dict]:
    """枚举可用于「USB 有线」通道的设备。

    两条来源：
      1) 已知 VID/PID（WIRED_CANDIDATES，实测的 NFC Mini 2.0 有线设备）；
      2) 通用兜底：带「厂商自定义类(0xFF)接口 + bulk 端点」的复合设备（供未知变体手动选）。
    通用兜底会剔除明显不是读卡器的设备（大容量存储 / 无线蓝牙类接口），减少列表噪声。
    """
    try:
        usb_core, _usb_util = _usb_modules()
    except NfcError:
        return []
    backend = _usb_backend()
    found: List[dict] = []
    seen = set()

    def _collect(kwargs: dict, require_vendor: bool = False) -> None:
        try:
            devs = usb_core.find(backend=backend, find_all=True, **kwargs)
        except Exception:
            return
        for dev in devs or []:
            try:
                info = _describe_usb(dev)
            except Exception:
                continue
            key = (info["vid_pid"], info["serial"])
            if key in seen or not info.get("bulk_out") or not info.get("bulk_in"):
                continue
            iface = info.get("interfaces") or []
            if require_vendor:
                if not any(i.get("vendor") for i in iface):
                    continue
                if {int(i.get("class", 0)) for i in iface} & _BLOCKED_IFACE_CLASSES:
                    continue
            seen.add(key)
            info["generic"] = not info["known"]
            found.append(info)

    for vid, pid, _label in WIRED_CANDIDATES:
        _collect({"idVendor": vid, "idProduct": pid})
    # 通用兜底：任何"厂商自定义类 + 有 bulk 端点"的设备都列出来，供用户手动选
    _collect({}, require_vendor=True)
    found.sort(key=lambda d: (not d["known"], d["vid_pid"]))
    return found


class WiredUsbTransport(Transport):
    """USB 有线通道：libusb 打开厂商自定义接口，bulk 端点收发私有帧。"""

    kind = "wired"

    def __init__(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        super().__init__()
        self.on_log = on_log
        self._dev = None
        self._ep_out = None
        self._ep_in = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.vid = 0
        self.pid = 0
        self.iface_num = -1
        self.describe: Dict[str, Any] = {}
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.raw_log: List[dict] = []
        self._log_lock = threading.Lock()

    # ---------------------------------------------------------- 状态
    @property
    def connected(self) -> bool:
        return bool(self._dev is not None and self._ep_out is not None)

    @property
    def label(self) -> str:
        if not self.connected:
            return "USB 有线 未连接"
        name = self.describe.get("product") or self.describe.get("label") or ""
        return f"{name} {self.vid:04X}:{self.pid:04X}".strip()

    # ---------------------------------------------------------- 原始日志
    def _raw(self, direction: str, data: bytes) -> None:
        item = {"dir": direction, "hex": bytes(data).hex(" ").upper(),
                "len": len(data), "ts": time.strftime("%H:%M:%S")}
        with self._log_lock:
            self.raw_log.append(item)
            if len(self.raw_log) > WIRED_RAW_LOG_MAX:
                del self.raw_log[:len(self.raw_log) - WIRED_RAW_LOG_MAX]
        self._log("trace" if direction == "rx" else "info",
                  f"[USB有线] {'<-' if direction == 'rx' else '->'} {item['hex']}")

    def raw_snapshot(self, limit: int = 200, clear: bool = False) -> List[dict]:
        with self._log_lock:
            items = self.raw_log[-int(limit):]
            if clear:
                self.raw_log = []
        return [dict(x) for x in items]

    def raw_count(self) -> int:
        with self._log_lock:
            return len(self.raw_log)

    def raw_since(self, index: int) -> List[dict]:
        with self._log_lock:
            items = self.raw_log[int(index):]
        return [dict(x) for x in items]

    # ---------------------------------------------------------- 打开 / 关闭
    def open(self, vid: int = 0, pid: int = 0, serial: str = "", **kw) -> None:
        usb_core, usb_util = _usb_modules()
        backend = _usb_backend()
        vid, pid = int(vid), int(pid)
        if not vid or not pid:
            if not WIRED_CANDIDATES:
                raise NfcError("没有可用的 USB 有线设备配置")
            vid, pid = WIRED_CANDIDATES[0][0], WIRED_CANDIDATES[0][1]

        self.close()
        self._log("info", f"打开 USB 有线设备 {vid:04X}:{pid:04X} …")
        try:
            dev = usb_core.find(idVendor=vid, idProduct=pid, backend=backend)
        except Exception as exc:                   # noqa: BLE001
            raise NfcError(f"USB 设备查找失败：{exc}") from exc
        if dev is None:
            raise NfcError(f"未找到 USB 设备 {vid:04X}:{pid:04X}，请确认已插好数据线")
        if serial:
            try:
                if str(dev.serial_number or "") != serial:
                    dev = usb_core.find(idVendor=vid, idProduct=pid, backend=backend,
                                        serial_number=serial) or dev
            except Exception:
                pass

        self.describe = _describe_usb(dev)
        try:
            dev.set_configuration()
        except Exception as exc:                   # noqa: BLE001
            # Windows 上已由驱动配置过，重复设置会报错，不影响后续收发
            self._log("warn", f"set_configuration 跳过：{exc}")

        cfg = dev.get_active_configuration()
        intf = None
        for candidate in cfg:
            has_out = has_in = False
            for ep in candidate:
                if int(ep.bmAttributes) != 2:
                    continue
                if int(ep.bEndpointAddress) & 0x80:
                    has_in = True
                else:
                    has_out = True
            if has_out and has_in:
                if int(candidate.bInterfaceClass) == 0xFF:
                    intf = candidate
                    break
                intf = intf or candidate
        if intf is None:
            raise NfcError("该 USB 设备没有可用的 bulk 收发端点")

        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except Exception:
            pass
        try:
            usb_util.claim_interface(dev, intf.bInterfaceNumber)
        except Exception as exc:                   # noqa: BLE001
            raise NfcError(f"占用 USB 接口失败（可能被其他程序占用）：{exc}") from exc

        ep_out = ep_in = None
        for ep in intf:
            if int(ep.bmAttributes) != 2:
                continue
            if int(ep.bEndpointAddress) & 0x80:
                ep_in = ep_in or ep
            else:
                ep_out = ep_out or ep

        self._dev, self._ep_out, self._ep_in = dev, ep_out, ep_in
        self.vid, self.pid = vid, pid
        self.iface_num = int(intf.bInterfaceNumber)
        self.tx_bytes = self.rx_bytes = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="wired-usb-rx",
                                        daemon=True)
        self._thread.start()
        self._log("ok", f"USB 有线已打开：接口 {self.iface_num}、"
                        f"OUT {int(ep_out.bEndpointAddress):#04x}、"
                        f"IN {int(ep_in.bEndpointAddress):#04x}")

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        dev = self._dev
        self._dev = self._ep_out = self._ep_in = None
        if dev is not None:
            try:
                _usb_modules()[1].dispose_resources(dev)
            except Exception:
                pass

    # ---------------------------------------------------------- 收发
    def _reader(self) -> None:
        while not self._stop.is_set():
            ep = self._ep_in
            if ep is None:
                break
            try:
                chunk = bytes(ep.read(WIRED_EP_SIZE, timeout=300))
            except Exception as exc:               # noqa: BLE001
                msg = str(exc)
                if "timeout" in msg.lower() or "timed out" in msg.lower():
                    continue
                if not self._stop.is_set():
                    self._log("err", f"USB 有线读取中断：{exc}")
                break
            if not chunk:
                continue
            self.rx_bytes += len(chunk)
            self._raw("rx", chunk)
            if self.on_data:
                try:
                    self.on_data(chunk)
                except Exception as exc:           # noqa: BLE001
                    self._log("err", f"USB 有线数据回调异常：{exc}")

    def drain(self, rounds: int = 8) -> None:
        """把设备里积压的旧数据读掉，避免试发帧时把陈年缓冲当成应答。"""
        for _ in range(max(0, int(rounds))):
            ep = self._ep_in
            if ep is None:
                return
            try:
                ep.read(WIRED_EP_SIZE, timeout=20)
            except Exception:
                return

    def send(self, frame: bytes) -> None:
        ep = self._ep_out
        if ep is None:
            raise NfcError("USB 有线设备未打开")
        try:
            ep.write(bytes(frame), timeout=1500)
            self.tx_bytes += len(frame)
            self._raw("tx", bytes(frame))
        except Exception as exc:                   # noqa: BLE001
            raise NfcError(f"USB 有线写入失败：{exc}") from exc


def wired_handshake(dev, timeout: float = 2.5) -> dict:
    """USB 有线握手：发 INFO / STATE 帧并等待同协议应答。

    返回 {"ok", "detail", "sent", "reply", "raw"}，不抛异常。
    注意：目前硬件对私有帧暂无应答（探测脚本实测），此时如实回报"已发出、无应答"。
    """
    from .protocol import CMD, build_frame

    sent: List[str] = []
    for cmd in (CMD.INFO, CMD.STATE):
        frame = build_frame(bytes([cmd]), cmd)
        sent.append(frame.hex(" ").upper())
        try:
            dev.transport.send(frame)
        except Exception as exc:                   # noqa: BLE001
            return {"ok": False, "detail": f"发送失败：{exc}", "sent": sent,
                    "reply": "", "raw": ""}
        try:
            raw = dev.request(bytes([cmd]), timeout=timeout)
        except Exception:                          # noqa: BLE001
            continue
        if len(raw) >= 4 and raw[0] == cmd:
            return {"ok": True, "detail": "握手成功：设备按 NFC Mini 协议应答",
                    "sent": sent, "reply": fmt_hex(raw), "raw": fmt_hex(raw)}
    return {"ok": False, "sent": sent, "reply": "",
            "detail": "帧已发出，但设备暂未按本协议应答（可能固件仍使用私有帧）",
            "raw": ""}


def handshake(dev, timeout: float = 2.5) -> dict:
    """串口握手：发一条信息帧，看设备是否按 FF…FE 协议回话。

    返回 {"ok", "detail", "raw"}；不抛异常，方便 UI 直接展示探测结论。
    """
    from .protocol import CMD

    try:
        raw = dev.request(bytes([CMD.INFO]), timeout=timeout)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "detail": f"未收到符合协议的应答：{exc}", "raw": ""}
    if len(raw) >= 4 and raw[0] == CMD.INFO:
        return {"ok": True, "detail": "握手成功：设备按 NFC Mini 协议应答",
                "raw": fmt_hex(raw)}
    return {"ok": False, "detail": f"收到数据但不是本协议应答：{fmt_hex(raw[:16])}",
            "raw": fmt_hex(raw[:32])}


__all__ = [
    "AsyncRunner", "Transport", "BleTransport", "SerialTransport",
    "WiredUsbTransport", "list_wired_devices", "wired_handshake",
    "list_serial_ports", "list_usb_devices", "handshake",
    "BAUD_CHOICES", "DEFAULT_BAUD", "WIRED_CANDIDATES", "WIRED_EP_SIZE",
    "SERVICE_UUID", "WRITE_UUID", "NOTIFY_UUID",
]
