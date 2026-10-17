# -*- coding: utf-8 -*-
"""
传输层 · 手机当读卡器（NetTransport）
=====================================
把一台带 NFC 的安卓手机变成读卡器：手机端 APK 开一个 TCP 服务，
桌面端连上去后，收发**和蓝牙完全一样**的私有协议帧，业务代码零改动。

拓扑
----
    手机(NFC 读卡模式)  <--TCP 8765-->  桌面端 NetTransport
    手机每秒向 UDP 8766 广播 «NFCMINI/1 <设备名> <ip> <port>»，
    桌面端靠这个广播自动发现手机，不用手输 IP。

为什么能这样
------------
Android 的 NFC Reader Mode 能直接对 MIFARE Classic 做认证/读块/写块，
和原厂读卡器固件提供的命令一一对应，所以把帧转发过去即可。

限制（APK 里和文档里都会写清楚）
--------------------------------
* 读不了 125kHz 低频 ID 卡（手机没有低频天线）→ 0x26 返回「非 ID 卡」。
* 手机天线小，贴卡位置要对准；MIFARE Classic 需要卡片保持贴近。
* 不支持固件级随机数采集类破解（0x20/0x22/0x30/0x32），
  但桌面端可以拿到普通认证结果自行跑 mfkey32。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from .protocol import NfcError, fmt_hex
from .transport import Transport

# 广播/服务默认端口
BEACON_PORT = 8766
TCP_PORT = 8765
BEACON_MAGIC = "NFCMINI/1"


class NetTransport(Transport):
    """通过 TCP 把协议帧转发给手机 APK 执行。"""

    kind = "net"
    label = "手机读卡器"

    CONNECT_TIMEOUT = 6.0

    def __init__(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        super().__init__()
        self.on_log = on_log
        self.host: str = ""
        self.port: int = TCP_PORT
        self.device_name: str = ""
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self._send_lock = threading.Lock()
        self.last_error = ""
        self._beacon_sock: Optional[socket.socket] = None
        self._beacon_alive = False

    # ------------------------------------------------------------ 属性
    @property
    def connected(self) -> bool:
        return bool(self._sock and self._alive)

    @property
    def display_label(self) -> str:
        return f"手机 · {self.device_name or self.host}"

    # ------------------------------------------------------------ 发现
    @classmethod
    def discover(cls, timeout: float = 3.0) -> List[dict]:
        """监听手机广播，返回可用手机读卡器列表。"""
        found: Dict[str, dict] = {}
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.bind(("", BEACON_PORT))
            sock.settimeout(0.3)
            deadline = time.time() + max(0.5, float(timeout))
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = cls._parse_beacon(data, addr)
                if info:
                    found[info["address"]] = info
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        return sorted(found.values(), key=lambda d: d.get("name", ""))

    @staticmethod
    def _parse_beacon(data: bytes, addr) -> Optional[dict]:
        try:
            text = data.decode("utf-8", "replace").strip()
        except Exception:
            return None
        if not text.startswith(BEACON_MAGIC):
            return None
        parts = text.split()
        name = ""
        port = TCP_PORT
        host = addr[0]
        # 形如：NFCMINI/1 <名字> <ip> <port>，名字里可能有空格的话不做支持（用下划线）
        # 手机端广播格式固定 4 段：NFCMINI/1 <名字> <ip> <port>
        if len(parts) >= 2:
            name = parts[1]
        if len(parts) >= 3 and parts[2]:
            host = parts[2]
        if len(parts) >= 4:
            try:
                port = int(parts[3])
            except (TypeError, ValueError):
                port = TCP_PORT
        return {
            "address": f"{host}:{port}",
            "host": host,
            "port": port,
            "name": name or f"手机读卡器 {host}",
            "rssi": 0,
            "likely_reader": True,
            "transport": "net",
            "device_type": "phone-reader",
        }

    def scan(self, timeout: float = 3.0) -> List[dict]:
        self._log("info", f"正在搜索局域网内的手机读卡器（{timeout:.0f} 秒）…")
        devs = self.discover(timeout)
        if devs:
            self._log("ok", f"发现 {len(devs)} 台手机读卡器")
        else:
            self._log("warn", "没发现手机读卡器：请确认手机 APK 已开启服务、"
                              "且手机和电脑在同一个 Wi-Fi 下")
        return devs

    # ------------------------------------------------------------ 打开/关闭
    def open(self, address: str = "", host: str = "", port: int = 0,
             timeout: float = CONNECT_TIMEOUT, **kw) -> None:
        addr = str(address or "").strip()
        h, p = str(host or "").strip(), int(port or 0)
        if addr:
            if ":" in addr:
                h2, p2 = addr.rsplit(":", 1)
                h = h or h2
                try:
                    p = p or int(p2)
                except ValueError:
                    p = p or TCP_PORT
            else:
                h = h or addr
        h = h or "127.0.0.1"
        p = p or TCP_PORT

        self.close()
        self._log("info", f"连接手机读卡器 {h}:{p} …")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(timeout)
        try:
            sock.connect((h, p))
        except OSError as exc:
            try:
                sock.close()
            except Exception:
                pass
            raise NfcError(f"连接手机读卡器失败：{exc}") from exc
        sock.settimeout(None)
        self._sock = sock
        self.host, self.port = h, p
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop,
                                       name="net-reader", daemon=True)
        self._reader.start()
        # 握手：手机端会回一条 NFCMINI/1 文本 hello 或直接等帧，这里不强制
        self._log("ok", f"已连接手机读卡器 {h}:{p}")

    def close(self) -> None:
        self._alive = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._reader = None

    # ------------------------------------------------------------ 收
    def _read_loop(self) -> None:
        buf = b""
        while self._alive and self._sock is not None:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # 手机端可能先发一行文本问候，剥掉
            if buf.startswith(b"NFCMINI"):
                nl = buf.find(b"\n")
                if nl < 0:
                    continue
                hello = buf[:nl].decode("utf-8", "replace")
                buf = buf[nl + 1:]
                self._log("info", f"手机读卡器：{hello}")
            if buf and self.on_data:
                data, buf = buf, b""
                try:
                    self.on_data(data)
                except Exception as exc:               # noqa: BLE001
                    self._log("err", f"手机读卡器数据回调异常：{exc}")
        self._alive = False
        if self._sock is not None:
            self._log("warn", "手机读卡器连接已断开")

    # ------------------------------------------------------------ 发
    def send(self, frame: bytes) -> None:
        if not self.connected or self._sock is None:
            raise NfcError("手机读卡器未连接")
        with self._send_lock:
            try:
                self._sock.sendall(bytes(frame))
            except OSError as exc:
                self._alive = False
                raise NfcError(f"发送到手机读卡器失败：{exc}") from exc
        self._log("trace", f"-> 手机 {fmt_hex(frame[:32])}"
                           + ("…" if len(frame) > 32 else ""))


# ================================================================ 局域网工具
def local_ip() -> str:
    """取本机局域网 IP（给用户看「手机该连哪个地址」用）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def probe(timeout: float = 2.0) -> dict:
    """探测局域网里有没有手机读卡器在广播。"""
    devs = NetTransport.discover(timeout)
    return {
        "supported": True,
        "found": devs,
        "local_ip": local_ip(),
        "beacon_port": BEACON_PORT,
        "tcp_port": TCP_PORT,
        "hint": ("" if devs else
                 "没发现手机读卡器。请：1) 手机安装本项目 APK 并打开；"
                 "2) 点「启动读卡服务」；3) 手机和电脑连同一个 Wi-Fi；"
                 "4) 若路由器开了「AP 隔离」，需要关掉。"),
    }


__all__ = ["NetTransport", "probe", "local_ip", "BEACON_PORT", "TCP_PORT",
           "BEACON_MAGIC"]
