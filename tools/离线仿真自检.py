# -*- coding: utf-8 -*-
"""
NFC Mini Studio 离线仿真自检（不接真机）
==========================================
用假 BLE 传输驱动 app.device.NfcDevice，把「扫描 -> 连接 -> 各业务命令」
整条宿主机链路跑一遍。用来把故障切成两半：

    * 宿主机代码有问题 -> 这里就会 FAIL，能直接定位到是哪条命令
    * 这里全 PASS      -> 读写逻辑是好的，问题在蓝牙链路/设备侧

用法：
    python tools/离线仿真自检.py
报告：
    项目根目录下 离线仿真自检报告.json
"""
import asyncio
import json
import sys
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEAD, TAIL = 0xFF, 0xFE
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


# ------------------------------------------------------------------ 帧工具
def build_frame(data: bytes, mid: int) -> bytes:
    return bytes([HEAD, len(data), mid]) + bytes(data) + bytes([sum(data) & 0xFF, TAIL])


def parse_frames(buf: bytearray):
    """流式解析，模拟真实分片到达"""
    out = []
    while True:
        if len(buf) < 5:
            break
        if buf[0] != HEAD:
            del buf[0]
            continue
        n = buf[1] + 5
        if len(buf) < n:
            break
        pkt = bytes(buf[:n])
        del buf[:n]
        if pkt[-1] != TAIL:
            continue
        ln, mid = pkt[1], pkt[2]
        payload = pkt[3:3 + ln]
        if sum(payload) & 0xFF != pkt[3 + ln]:
            continue
        out.append((mid, payload))
    return out


# ------------------------------------------------------------------ 假设备
class FakePeer:
    """模拟读卡器固件：按命令码回包"""

    def __init__(self, silent: bool = False):
        self.silent = silent
        self.calls = []
        self.blocks = {}          # 模拟卡片存储：块号 -> 16 字节，供写后回读校验

    def reply(self, d: bytes):
        if not d:
            return b""
        cmd = d[0]
        self.calls.append(hex(cmd))
        if self.silent:
            return None                      # 装死：连得上但不应答
        if cmd == 0x01:                      # 设备信息
            return bytes([0x01, 0, 1, 2]) + bytes(range(0x10, 0x1C))
        if cmd == 0x02:                      # 设备状态
            return bytes([0x02, 0, 0, 0])
        if cmd == 0x11:                      # 寻卡
            return bytes([0x11, 0, 1, 0xDD, 0xCC, 0xBB, 0xAA, 0x08, 0])
        if cmd == 0x0A:                      # 密钥认证
            return bytes([0x0A, 0])
        if cmd == 0x07:                      # 读块：优先回读已写入的数据
            blk = d[1] & 0xFF
            data = self.blocks.get(blk) or bytes([(blk * 16 + i) & 0xFF for i in range(16)])
            return bytes([0x07, blk, 0]) + data
        if cmd in (0x08, 0x16, 0x18):        # 写块：记录数据，供写后回读校验
            blk = d[1] & 0xFF
            self.blocks[blk] = bytes(d[2:18]).ljust(16, b"\x00")
            return bytes([cmd, 0])
        if cmd in (0x20, 0x30, 0x32, 0x38, 0x22, 0x37):   # 破解类
            return bytes([cmd, 0]) + bytes.fromhex("FFFFFFFFFFFF")
        if cmd == 0x25:                      # 漏洞卡检测
            return bytes([0x25, 1])
        if cmd == 0x27:                      # 写 UID
            return bytes([0x27, 0])
        return bytes([cmd, 0])


class FakeBleakClient:
    def __init__(self, address, timeout=30, **kwargs):
        self.address = address
        self._cb = None
        self._connected = False
        self._rx = bytearray()
        self.peer = FakePeer()

    async def connect(self):
        self._connected = True

    @property
    def is_connected(self):
        return self._connected

    async def start_notify(self, uuid, callback):
        if not self._connected:
            raise RuntimeError("client not connected")
        if uuid != NOTIFY_UUID:
            raise RuntimeError(f"characteristic {uuid} not found")
        self._cb = callback

    async def stop_notify(self, uuid):
        self._cb = None

    async def disconnect(self):
        self._connected = False

    async def write_gatt_char(self, uuid, data, response=False):
        if not self._connected:
            raise RuntimeError("client not connected")
        if uuid != WRITE_UUID:
            raise RuntimeError(f"characteristic {uuid} not found")
        self._rx += data
        for mid, payload in parse_frames(self._rx):
            rep = self.peer.reply(payload)
            if rep is None:
                continue
            frame = build_frame(rep, mid)
            loop = asyncio.get_running_loop()
            cut = max(1, len(frame) // 2)          # 故意分两片推送
            loop.call_later(0.004, self._cb, None, bytearray(frame[:cut]))
            loop.call_later(0.012, self._cb, None, bytearray(frame[cut:]))


class FakeAdv:
    def __init__(self, name, uuids, rssi):
        self.local_name = name
        self.service_uuids = uuids
        self.rssi = rssi


class FakeBLEDevice:
    def __init__(self, address, name):
        self.address = address
        self.name = name


class FakeBleakScanner:
    @staticmethod
    async def discover(timeout=10.0, return_adv=True):
        other = FakeBLEDevice("BF:CA:86:9F:92:10", "Wuzhi Audio APP")
        reader = FakeBLEDevice("D0:0C:5E:94:0B:A8", "NFC Mini")
        return {
            other.address: (other, FakeAdv("Wuzhi Audio APP", [], -61)),
            reader.address: (reader, FakeAdv("NFC Mini", [SERVICE_UUID], -55)),
        }


# ------------------------------------------------------------------ 注入假 bleak
fake_bleak = types.ModuleType("bleak")
fake_bleak.BleakClient = FakeBleakClient
fake_bleak.BleakScanner = FakeBleakScanner
sys.modules["bleak"] = fake_bleak

import app.device as D          # noqa: E402  此时拿到的是假 bleak


# ------------------------------------------------------------------ 跑测试
def run_case(name, fn, results, silent=False):
    dev = D.NfcDevice(on_log=lambda lv, tx: None)
    if silent:
        dev.connect("D0:0C:5E:94:0B:A8")   # 先连上再置沉默
        dev._ble._client.peer.silent = True
    try:
        value = fn(dev)
        results.append({"case": name, "ok": True, "value": value})
        print(f"[PASS] {name}")
    except Exception as exc:
        results.append({"case": name, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=3)})
        print(f"[FAIL] {name}  -> {type(exc).__name__}: {exc}")
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass


def main():
    results = []

    def c_scan(dev):
        devs = dev.scan(1.0)
        assert len(devs) == 2, f"扫描结果异常 {devs}"
        return [{"name": d["name"], "likely": d["likely_reader"]} for d in devs]
    run_case("scan 扫描并标记读卡器", c_scan, results)

    def c_connect(dev):
        dev.connect("D0:0C:5E:94:0B:A8")
        info = dev.device_info()
        assert info["version"] == "0.1.2", info
        return info
    run_case("connect 连接并读设备信息 0x01", c_connect, results)

    def c_biz(dev):
        dev.connect("D0:0C:5E:94:0B:A8")
        out = {}
        out["state"] = dev.device_state()["raw"]
        out["find"] = dev.find_card()
        out["auth"] = dev.auth(4, bytes(b"\xff" * 6), 0)
        out["read"] = dev.read_block(4, 0).hex().upper()
        dev.write_block(4, bytes(16), bytes(b"\xff" * 6), 0)
        out["write"] = "ok"
        out["crack"] = dev.crack("staticnested", 0)
        out["bug"] = dev.detect_bug_card()
        out["write_uid"] = dev.write_uid()
        return out
    run_case("业务命令全集（状态/寻卡/认证/读写/破解/漏洞卡/写UID）", c_biz, results)

    def c_dump(dev):
        dev.connect("D0:0C:5E:94:0B:A8")
        t0 = time.time()
        res = dev.dump_card(include_trailers=False)
        return {"blocks": len([v for v in res["blocks"].values() if v]),
                "errors": len(res["errors"]), "sec": round(time.time() - t0, 2)}
    run_case("dump_card 整卡读取 48 块", c_dump, results)

    def c_silent(dev):
        try:
            dev.device_info()
        except D.NfcError as exc:
            return f"如期报错 -> {exc}"
        raise AssertionError("沉默设备竟然没报错")
    run_case("故障复现：BLE 连上但设备不应答", c_silent, results, silent=True)

    ok = sum(1 for r in results if r["ok"])
    print("\n================ 结论 ================")
    print(f"通过 {ok}/{len(results)}")
    out = {"passed": ok, "total": len(results), "results": results}
    report = ROOT / "离线仿真自检报告.json"
    report.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写出：{report}")


if __name__ == "__main__":
    main()
