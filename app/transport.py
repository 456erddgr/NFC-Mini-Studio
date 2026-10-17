# -*- coding: utf-8 -*-
"""
传输层：NFC Mini 2.0 读卡器的 BLE（低功耗蓝牙）通道。

BLE 侧的异步操作统一跑在后台事件循环线程里，避免阻塞 pywebview 的 UI 线程。
"""
from __future__ import annotations

import asyncio
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
    # 单次 BLE 写入的分片大小：长帧（如 0x40 灌 500 把密钥 = 3005 字节）必须分片，
    # 小程序侧也是按 MTU-3 分片、失败再折半重试。
    CHUNK_NORMAL = 180
    CHUNK_SMALL = 20

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
        """懒加载：未显式传入 runner 时按需创建事件循环线程。"""
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
        from bleak import BleakClient, BleakScanner

        await self._close()
        # Windows 上直接按 MAC 连经常报 "device not found"（缓存过期/广播没抓到），
        # 这里失败一次后现场扫一遍拿到真实 device 对象再连，成功率明显更高。
        client = BleakClient(mac, timeout=timeout)
        try:
            await client.connect()
        except Exception as first:
            dev = await BleakScanner.find_device_by_address(mac, timeout=max(6.0, timeout))
            if dev is None:
                raise first
            client = BleakClient(dev, timeout=timeout)
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

        async def _write(size: int):
            for i in range(0, len(frame), size):
                await client.write_gatt_char(WRITE_UUID, frame[i:i + size], response=False)

        long_frame = len(frame) > 255
        try:
            self.runner.call(_write(self.CHUNK_NORMAL), timeout=25 if long_frame else 8)
        except Exception as exc:                       # noqa: BLE001
            if len(frame) <= self.CHUNK_NORMAL:
                raise
            # 大分片被拒（部分适配器 MTU 小）：退回 20 字节小分片重发，兼容优先
            self._log("warn", f"大分片写入失败（{exc}），改用 20 字节小分片重试")
            self.runner.call(_write(self.CHUNK_SMALL), timeout=60)


def handshake(dev, timeout: float = 2.5) -> dict:
    """协议握手：发一条信息帧，看设备是否按 FF…FE 协议回话。

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
    "AsyncRunner", "Transport", "BleTransport", "handshake",
    "SERVICE_UUID", "WRITE_UUID", "NOTIFY_UUID",
]
