# -*- coding: utf-8 -*-
"""暴露给前端 HTML 的 JS API（pywebview js_api）。

接口约定：所有方法统一返回 ``{"ok": bool, ...}``，前端不必处理 Python 异常。
  * 寻卡自动识卡（IC / ID / UID / 7 字节 UID）并自动在**后台**发起破解级联；
  * 读卡、写卡、复制、粘贴、破解全部推送带百分比的实时进度事件；
  * 读/写卡的密钥统一走 _resolve_key()（会话破解 → 本卡历史库 → 密码库 →
    界面输入 → 默认密钥，逐把真机认证），不再让界面默认密钥覆盖破解结果；
  * 写卡走后端自适应帧格式 + 回读校验，并把实际命中的格式回报给界面。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from .crack_engine import (AUTH_HEADER, ENGINE_LABELS, MODE_CHOICES, MODE_CLOUD,
                           MODE_DEVICE, MODE_LOCAL, VENDOR_UA, CrackEngine,
                           _vendor_request, cloud_config, cloud_keygen_lookup, parse_frames,
                           parse_vendor_response, vendor_urls)
from .device import CRACK_CHAIN, CRACK_LABELS, NfcDevice
from .paths import (APP_TITLE, APP_VERSION, sync_wallpapers, wallpapers_dir,
                    web_assets_dir, crack_cache_file)
from .protocol import (DEFAULT_KEY, ERR, KEY_CACHE_MAX, PHONE_WRITE_MODES, NfcError,
                       WRITE_FORMATS, hex_to_bytes, phone_nfc_plan, resolve_write_mode,
                       write_mode_table)
from .store import CardBox, Settings
from .keybox import KeyBox

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

CRACK_METHODS = [
    {"id": "staticnested", "name": "staticnested 静态嵌套",
     "desc": "已知单扇区密钥，恢复 PRNG 后推导全卡密钥（最快）"},
    {"id": "staticnested2", "name": "staticnested 变体",
     "desc": "0x38 指令（带载荷），部分固件版本可用；注意与「0x38 自动识别卡片」"
             "（单字节 DATA）同码不同义，设备靠 DATA 长度区分"},
    {"id": "half_decode", "name": "half_decode 半解",
     "desc": "速度较快，成功率视卡片而定"},
    {"id": "hardnested", "name": "hardnested 硬嵌套",
     "desc": "未知密钥时的兜底方案，耗时较长"},
    {"id": "full_decode", "name": "full_decode 全卡解码",
     "desc": "一次性尝试恢复全部扇区密钥"},
    {"id": "fm11rf08s", "name": "FM11RF08S 后门",
     "desc": "针对复旦微 FM11RF08S 卡片的已知后门"},
]


class Api:
    """所有方法返回 {'ok': bool, ...} 结构，前端无需处理 Python 异常。"""

    # pywebview 暴露 js_api 时会递归遍历本对象的属性；它一遇到 window 这种原生窗口对象，
    # 就会顺着 WinForms 的 AccessibilityObject / DataBindings 一路递归下去，刷出成吨的
    # "Error while processing window.native.…Empty.Empty…" 日志，拖慢启动甚至把栈打爆。
    # 这里给组内对象打上 _serializable = False，让 pywebview 直接跳过；
    # 窗口引用另起下划线名（下划线开头本就会被跳过），前端只用扁平方法名，不受影响。
    _serializable = False

    def __init__(self) -> None:
        self._window = None                      # 由 main.py 注入
        self.settings = Settings()
        self.cardbox = CardBox()
        self.keybox = KeyBox()
        self.device = NfcDevice(on_log=self._on_device_log)
        for _group in (self.settings, self.cardbox, self.device):
            try:
                _group._serializable = False     # 双保险：实例级也标一次
            except Exception:
                pass
        self._store_lock = threading.RLock()
        self._cracked_keys: Dict[int, str] = {}
        self._crack_result: Optional[dict] = None
        self._last_card_id = ""
        self._last_card: dict = {}
        self._crack_job = ""
        # v2.2.0：破解双模式引擎（云端解算 → 本地 mfkey32 → 固件级联 自动降级）
        self._crack_engine = CrackEngine(settings=self.settings.data, log=self._on_device_log)
        self._crack_engine._serializable = False
        self._restore_cache()

    # ------------------------------------------------------------ 内部
    def _push(self, channel: str, payload: Any) -> None:
        if not self._window:
            return
        try:
            js = ("window.__push && window.__push("
                  f"{json.dumps(channel)}, "
                  f"{json.dumps(payload, ensure_ascii=False)})")
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _on_device_log(self, level: str, text: str) -> None:
        self._push("log", {"level": level, "text": text, "ts": time.strftime("%H:%M:%S")})

    @staticmethod
    def _ok(data: Any = None, **extra) -> dict:
        res = {"ok": True}
        if data is not None:
            res["data"] = data
        res.update(extra)
        return res

    @staticmethod
    def _err(exc: Exception) -> dict:
        if isinstance(exc, NfcError):
            return {"ok": False, "error": str(exc), "code": exc.code}
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _guard(self, fn: Callable[[], Any]) -> dict:
        try:
            return self._ok(fn())
        except Exception as exc:                       # noqa: BLE001
            self._on_device_log("err", str(exc))
            return self._err(exc)

    # ---- 破解结果缓存（换卡即失效，下次打开软件仍能看到上一次的解密记录）
    def _restore_cache(self) -> None:
        try:
            path = crack_cache_file()
            if path.exists():
                with open(path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                self._last_card = data.get("last_card") or {}
        except Exception:
            self._last_card = {}

    def _save_cache(self) -> None:
        try:
            with open(crack_cache_file(), "w", encoding="utf-8") as fp:
                json.dump({"last_card": self._last_card}, fp, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _progress_push(self, job: str, done: int, total: int, label: str = "",
                       block: Optional[int] = None, started: Optional[float] = None) -> None:
        pct = round(done / total * 100, 1) if total else 0.0
        payload = {
            "job": job, "done": int(done), "total": int(total),
            "percent": pct, "label": label,
            "elapsed": round(time.time() - started, 2) if started else None,
        }
        if block is not None:
            payload["block"] = int(block)
        self._push("progress", payload)

    def _best_key(self) -> tuple:
        """当前可用的最佳密钥：优先破解结果，其次设置里的默认密钥。"""
        kt = int(self.settings.data.get("default_key_type", 0))
        if self._cracked_keys:
            sector = sorted(self._cracked_keys)[0]
            return self._cracked_keys[sector], kt, sector
        return self.settings.data.get("default_key", "FFFFFFFFFFFF"), kt, None

    # ---- 统一密钥解析（读/写卡同源入口）--------------------------------
    def _history_keys(self, uid: str = "") -> list:
        """本卡在读卡历史库里记录过的全部密钥（大写 12 位 hex）。"""
        uid = str(uid or (self._last_card or {}).get("uid") or "").replace(" ", "").upper()
        if not uid:
            return []
        out: list = []
        try:
            for row in self.keybox.list("history"):
                if str(row.get("uid") or "").replace(" ", "").upper() != uid:
                    continue
                for k in (row.get("keys") or []):
                    if k:
                        out.append(str(k).replace(" ", "").upper())
        except Exception:                              # noqa: BLE001
            pass
        return out

    def _key_candidates(self, key_hex: str = "") -> list:
        """按优先级汇总候选密钥：会话破解 → 本卡历史库 → 用户/公共库 → 界面输入 → 默认。

        返回 [(来源标签, 12 位 hex), ...]，已去重且保持顺序。
        """
        uid = str((self._last_card or {}).get("uid") or "")
        src: list = [("会话破解", str(k)) for k in self._cracked_keys.values()]
        src += [("读卡历史库", k) for k in self._history_keys(uid)]
        try:
            for lib in ("user", "public"):
                for row in self.keybox.list(lib):
                    k = str(row.get("key") or "")
                    if k:
                        src.append((f"密码库·{'用户' if lib == 'user' else '公共'}", k))
        except Exception:                              # noqa: BLE001
            pass
        if key_hex:
            src.append(("界面输入", str(key_hex)))
        src.append(("默认密钥",
                    str(self.settings.data.get("default_key", "FFFFFFFFFFFF"))))
        seen, uniq = set(), []
        for tag, k in src:
            c = str(k).replace(" ", "").replace(":", "").replace("-", "").upper()
            if c and c not in seen:
                seen.add(c)
                uniq.append((tag, c))
        return uniq

    def _prefill_history_key(self, uid: str = "") -> str:
        """寻卡后把本卡历史库里「真机确实能认证通过」的密钥先回填会话缓存。"""
        if not self.device.connected:
            return ""
        for text in self._history_keys(uid):
            try:
                kb = hex_to_bytes(text)
            except Exception:                          # noqa: BLE001
                continue
            if len(kb) != 6:
                continue
            for kt in (0, 1):
                try:
                    self.device.auth(0, kb, kt)
                except Exception:                      # noqa: BLE001
                    continue
                self._cracked_keys[0] = text
                self._on_device_log(
                    "ok", f"读卡历史库已有该卡密钥 {text}，直接复用（无需重新破解）")
                return text
        return ""

    @staticmethod
    def _dedup_candidates(items, exclude=None) -> list:
        """把 [(来源标签, 密钥)] 规整成 12 位大写 hex、去重并剔除已试过的密钥。"""
        skip = {str(x).replace(" ", "").upper() for x in (exclude or set())}
        seen, out = set(), []
        for tag, k in items or []:
            c = str(k).replace(" ", "").replace(":", "").replace("-", "").upper()
            if len(c) < 12:
                continue
            c = c[-12:]
            if c in seen or c in skip:
                continue
            seen.add(c)
            out.append((tag, c))
        return out

    def _resolve_key(self, key_hex: str = "", key_type: int = -1,
                     block: int = 0, prefer: str = "") -> tuple:
        """解析出一把「真机确实能认证通过」的密钥（读/写卡统一入口）。

        修复点：旧逻辑「界面输入框有值就只看界面密钥」，导致整卡读取拿默认
        FFFFFFFFFFFFFF 去认证 16 个扇区 → 每块都回 08（密钥认证失败）→ 0/64。
        现在按 会话破解 → 本卡历史库 → 密码库 → 界面输入 → 默认 的顺序逐把用
        device.auth() 实测，第一把能过的就用它，并把结果回填会话缓存；
        全都不通时明确报错，而不是静默把 64 块读成失败。

        返回 (key: bytes, key_type: int, source: str)
        """
        cands = self._key_candidates(key_hex)
        if prefer:
            pref = str(prefer).replace(" ", "").upper()
            cands.sort(key=lambda item: 0 if item[1] == pref else 1)
        pref_kt = int(key_type) if int(key_type) >= 0 else \
            int(self.settings.data.get("default_key_type", 0))
        errors: list = []
        for tag, text in cands:
            try:
                kb = hex_to_bytes(text)
            except Exception:                          # noqa: BLE001
                continue
            if len(kb) != 6:
                continue
            for kt in ([pref_kt] if int(key_type) >= 0 else [pref_kt, 1 - pref_kt]):
                try:
                    self.device.auth(int(block), kb, kt)
                except Exception as exc:                # noqa: BLE001
                    errors.append(f"{text}({'KEY B' if kt else 'KEY A'}): {exc}")
                    continue
                self._cracked_keys.setdefault(int(block) // 4, text)
                self._on_device_log(
                    "info", f"密钥就绪：{text}（{'KEY B' if kt else 'KEY A'}，"
                            f"来源：{tag}，设备认证通过）")
                return kb, kt, tag
        raise NfcError(
            "本卡没有已知密钥能通过设备认证：请先执行「破解 / 批量验证」取得该卡密钥，"
            "或在「默认密钥」里填入真实密钥后重试"
            + (f"（已试 {len(errors)} 次；最后一条：{errors[-1]}）" if errors else ""), 8)

    # ------------------------------------------------------------ 应用信息
    def app_info(self) -> dict:
        return self._ok({
            "title": APP_TITLE,
            "version": APP_VERSION,
            "error_codes": {str(k): v for k, v in ERR.items()},
            "crack_methods": CRACK_METHODS,
            "crack_chain": list(CRACK_CHAIN),
            "crack_modes": MODE_CHOICES,
            "crack_engine": self._crack_engine.status(),
            "default_key": self.settings.data.get("default_key", "FFFFFFFFFFFF"),
            "default_key_type": self.settings.data.get("default_key_type", 0),
            "last_card": self._last_card,
            "write_formats": [f["id"] for f in WRITE_FORMATS],
            "write_modes": write_mode_table(),
            "phone_nfc": {
                "cmd": "0x27",
                "modes": [m for m in PHONE_WRITE_MODES],
                "steps": phone_nfc_plan(),
                "note": ("转接卡两步法：0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡 → "
                         "0x16/0x18 写 IC 数据；与写 UID/魔术卡（0x27 改写卡片 UID）、"
                         "封卡（0x17，不可逆）区分"),
            },
            "auto_identify": {
                "cmd": "0x38", "fallback": "0x11",
                "note": "0x38 自动识别卡片（单字节）；失败或非 ID 卡时回落 0x11 寻卡",
            },
            "lock_cmds": {"ufuid_lock": "0x17", "lock_card": "0x23",
                          "check_card_no": "0x24"},
            "check_card_no_cmd": "0x24",
        })

    # ------------------------------------------------------------ 设备：连接
    def scan(self, timeout: float = 6.0) -> dict:
        def work():
            devs = self.device.scan(float(timeout))
            # 常见坑：微信小程序打开着会自动连上读卡器、独占蓝牙，桌面端就一台都扫不到。
            try:
                from .wx_token import miniprogram_open

                if miniprogram_open() and not any(d.get("likely_reader") for d in devs):
                    self._on_device_log(
                        "warn", "检测到微信小程序正开着，但没扫到读卡器："
                                "小程序很可能已占用读卡器的蓝牙。"
                                "请在小程序里点「断开连接」（或关掉小程序窗口）后再扫描。")
            except Exception:                          # noqa: BLE001
                pass
            return devs
        return self._guard(work)

    def scan_all(self, timeout: float = 6.0, ble: bool = True,
                 pcsc: bool = True, net: bool = True) -> dict:
        """三通道一起扫：蓝牙原厂读卡器 / 通用 PC/SC 读卡器 / 手机读卡器。"""
        return self._guard(lambda: self.device.scan_all(
            float(timeout), with_ble=bool(ble), with_pcsc=bool(pcsc),
            with_net=bool(net)))

    def pcsc_probe(self) -> dict:
        """检测 PC/SC 环境：服务是否启动、有没有读卡器、怎么修。"""
        return self._guard(self.device.pcsc_probe)

    def net_probe(self, timeout: float = 2.0) -> dict:
        """检测局域网里的手机读卡器。"""
        return self._guard(lambda: self.device.net_probe(float(timeout)))

    def phone_reader_info(self) -> dict:
        """给「手机当读卡器」页面的信息：本机 IP、端口、下载 APK 的位置。"""
        from .transport_net import BEACON_PORT, TCP_PORT, local_ip

        apk = ""
        try:
            from .paths import app_dir

            for cand in (app_dir() / "android" / "NFCMiniReader.apk",
                         app_dir().parent / "android" / "NFCMiniReader.apk",
                         app_dir() / "NFCMiniReader.apk"):
                if cand.exists():
                    apk = str(cand)
                    break
        except Exception:                              # noqa: BLE001
            apk = ""
        return self._ok({
            "local_ip": local_ip(),
            "beacon_port": BEACON_PORT,
            "tcp_port": TCP_PORT,
            "apk_path": apk,
            "has_apk": bool(apk),
            "steps": [
                "在手机浏览器打开本机下载地址，或直接把 APK 传到手机安装",
                "打开 App，点「启动读卡服务」（首次需授予 NFC 权限）",
                "确保手机与电脑连同一个 Wi-Fi",
                "回到本页面点「搜索手机读卡器」，选中后连接",
                "之后所有读卡 / 写卡功能都用手机的天线执行",
            ],
            "limits": [
                "手机没有 125 kHz 低频天线：读不了 ID 卡（0x26 不可用）",
                "手机做不了固件级随机数采集：破解类命令需原厂读卡器或桌面端算法",
                "手机天线小，卡片要贴准 NFC 区域并保持贴近",
            ],
        })

    def open_phone_apk_folder(self) -> dict:
        """在资源管理器里打开手机读卡器 APK 所在目录（找不到就给出提示）。"""
        def work():
            import os
            import subprocess
            import sys

            apk = ""
            info = self.phone_reader_info()
            if isinstance(info, dict):
                payload = info.get("data") if isinstance(info.get("data"), dict) else info
                apk = (payload or {}).get("apk_path") or ""
            if not apk:
                return {"ok": False, "hint": "APK 还没有打包出来。请先运行 build_android.py 生成 "
                                             "android/NFCMiniReader.apk，或用手机浏览器下载安装。"}
            folder = str(Path(apk).parent)
            try:
                if sys.platform.startswith("win"):
                    os.startfile(folder)                     # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception as exc:                          # noqa: BLE001
                return {"ok": False, "hint": f"打开目录失败：{exc}；APK 位于 {apk}"}
            return {"ok": True, "hint": f"已打开 {folder}", "apk_path": apk}
        return self._guard(work)

    def auto_connect_phone(self, timeout: float = 2.5) -> dict:
        """启动时自动连手机读卡器：先听 UDP 信标，再试 127.0.0.1:8765。

        用户不该为了连手机去敲 adb 命令，所以这里做成「能连就静默连上」：
        * 手机和电脑在同一个 Wi-Fi → 手机每秒广播的 UDP 8766 信标直接给出地址；
        * USB 调试 + adb reverse tcp:8765 → 127.0.0.1:8765 兜底；
        * 都没有就返回 ok=False，前端只记一行日志，不弹窗打扰。
        """
        def work():
            if self.device.connected:
                return {"ok": True, "address": getattr(self.device, "address", ""),
                        "device": None, "note": "已经连着设备，跳过自动连接"}
            try:
                from .transport_net import NetTransport
            except Exception:                          # noqa: BLE001
                NetTransport = None
            targets = []
            if NetTransport is not None:
                try:
                    for item in NetTransport.discover(max(1.0, float(timeout))):
                        addr = str(item.get("address") or "").strip()
                        if addr and addr not in targets:
                            targets.append(addr)
                except Exception:                      # noqa: BLE001
                    pass
            if "127.0.0.1:8765" not in targets:
                targets.append("127.0.0.1:8765")
            last = ""
            for addr in targets:
                try:
                    info = self.device.connect_net(addr, timeout=3.0)
                    self._on_device_log("ok", f"已自动连接手机读卡器 {addr}")
                    return {"ok": True, "address": addr, "device": info,
                            "note": f"已自动连接手机读卡器 {addr}"}
                except Exception as exc:               # noqa: BLE001
                    last = f"{addr}（{exc}）"
                    continue
            note = ("没有发现手机读卡器：请确认手机 App 已打开、"
                    "和电脑连同一个 Wi-Fi；或插 USB 后在电脑执行 "
                    "adb reverse tcp:8765 tcp:8765")
            if last:
                note += f"。最后错误：{last}"
            return {"ok": False, "address": "", "device": None, "note": note}
        return self._guard(work)

    def connect(self, mac: str) -> dict:
        def work():
            info = self.device.connect(mac)
            return info
        return self._guard(work)

    def auto_connect(self) -> dict:
        """开机自动扫描并连接：原厂读卡器 → 手机读卡器 → 蓝牙，找到第一个能用的就连。"""
        if self.device.connected:
            return self._ok({"connected": True, "address": self.device.transport_label,
                             "note": f"已经连接：{self.device.transport_label}", "mode": self.device.mode})

        tried = []
        # ① 原厂 PC/SC 读卡器
        try:
            from .transport_pcsc import PcscTransport
            readers = [r for r in (PcscTransport.list_readers() or []) if r]
            if readers:
                self.device.connect_pcsc(readers[0])
                return self._ok({"connected": True, "address": readers[0], "mode": "pcsc",
                                 "note": f"已自动连接通用读卡器 {readers[0]}"})
            tried.append("没发现 PC/SC 读卡器")
        except Exception as exc:                                  # noqa: BLE001
            tried.append(f"PC/SC：{exc}")

        # ② 手机读卡器（UDP 信标，兜底 127.0.0.1 的 adb reverse）
        try:
            from .transport_net import NetTransport
            found = NetTransport.discover(2.0) or []
            cands = [d.get("address") for d in found if d.get("address")] or ["127.0.0.1:8765"]
            for addr in cands[:4]:
                try:
                    self.device.connect_net(addr, timeout=3.0)
                    return self._ok({"connected": True, "address": addr, "mode": "net",
                                     "note": f"已自动连接手机读卡器 {addr}"})
                except Exception:                                 # noqa: BLE001
                    continue
            tried.append("没发现手机读卡器")
        except Exception as exc:                                  # noqa: BLE001
            tried.append(f"手机读卡器：{exc}")

        # ③ 蓝牙读卡器
        try:
            devs = self.device.scan(timeout=4.0) or []
            for d in devs:
                mac = d.get("mac") or d.get("address")
                if not mac:
                    continue
                try:
                    self.device.connect(mac)
                    return self._ok({"connected": True, "address": mac, "mode": "ble",
                                     "note": f"已自动连接蓝牙读卡器 {d.get('name') or mac}"})
                except Exception:                                 # noqa: BLE001
                    continue
            tried.append("没发现蓝牙读卡器")
        except Exception as exc:                                  # noqa: BLE001
            tried.append(f"蓝牙：{exc}")

        return self._ok({"connected": False, "note": "自动扫描没找到设备（" + "；".join(tried) + "）"})

    def connect_pcsc(self, reader: str = "") -> dict:
        """连接通用 PC/SC 读卡器（reader 为空时用第一台）。"""
        return self._guard(lambda: self.device.connect_pcsc(str(reader or "")))

    def connect_net(self, address: str, timeout: float = 6.0) -> dict:
        """连接手机读卡器，address 形如 192.168.1.5:8765。"""
        return self._guard(lambda: self.device.connect_net(
            str(address or ""), timeout=float(timeout)))

    def capabilities(self) -> dict:
        """当前通道能力矩阵（前端据此禁用点不了的功能）。"""
        return self._ok(self.device.capabilities())

    def disconnect(self) -> dict:
        def work():
            self.device.disconnect()
            self._cracked_keys = {}
            return "已断开"
        return self._guard(work)

    def status(self) -> dict:
        snap = self.device.snapshot()
        snap["cracked_keys"] = dict(self._cracked_keys)
        snap["last_card"] = self._last_card
        snap["crack_job"] = self._crack_job
        return self._ok(snap)

    def device_info(self) -> dict:
        return self._guard(self.device.device_info)

    def device_state(self) -> dict:
        return self._guard(self.device.device_state)

    # ------------------------------------------------------------ 卡片操作
    def find_card(self, auto_crack: bool = True) -> dict:
        """寻卡 + 识卡 + （可选）后台自动破解。"""
        def work():
            info = self.device.find_card()
            self._apply_sak88(info)
            self._cracked_keys = {}
            self._crack_result = None
            self._crack_job = ""
            self._last_card = {
                "uid": info.get("uid", ""),
                "card_type": info.get("card_type", ""),
                "class": info.get("class", ""),
                "class_label": info.get("class_label", ""),
                "band": info.get("band", ""),
                "sak": info.get("sak", ""),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_cache()
            # 同 UID 的历史密钥先回填：后台破解是异步的（云端 keygen + 逐把验证，
            # 数十秒级），回填后「寻卡 → 读取整卡」不必等破解完成也能用上正确密钥。
            self._prefill_history_key(info.get("uid", ""))
            self._on_device_log(
                "ok", f"寻卡成功：{info.get('class_label')} / {info.get('card_type')} / "
                      f"UID {info.get('uid')} / SAK {info.get('sak')}"
                      + (f"（{info.get('class_note')}）" if info.get("class_note") else ""))
            if auto_crack and bool(self.settings.data.get("auto_crack", True)):
                handle = self._maybe_auto_crack(info)
                if handle:
                    info["crack_job"] = handle
            return info
        return self._guard(work)

    def _apply_sak88(self, info: dict) -> None:
        """SAK88 开关（对齐小程序 sak88）：SAK=0x88 属 7 字节 UID/特殊卡，标记出来。"""
        try:
            if self.settings.data.get("sak88") and int(info.get("sak_raw") or 0) == 0x88:
                info["sak88"] = True
                info["class_note"] = (info.get("class_note", "") +
                                      " · SAK88 开关已开：按 0x88 特殊卡处理").strip()
        except Exception:
            pass

    def _maybe_auto_crack(self, info: dict) -> str:
        """IC 类卡片在后台级联破解；ID 卡没有"密钥"概念，直接跳过。"""
        if not info.get("crackable"):
            self._on_device_log("info", "该卡不是可破解的 IC（Mifare）类型，跳过密钥破解")
            return ""
        if not self.device.connected:
            return ""
        # -1 = 全卡（对齐官方：把 16 个扇区都解出来），其余为单扇区模式
        sector = int(self.settings.data.get("crack_sector", -1))
        # 非完整KEY读卡（对齐小程序 NON_FULL_KEY_READ）：先用设备采集原始密文，
        # 采集结果留在 device.last_nonces，供云端算法取用；采集失败再回落固件级联。
        if self.settings.data.get("non_full_key_read"):
            try:
                self._on_device_log("info", "非完整KEY读卡已开启：先做设备采集（0x32）再走云端")
                self.device.collect_staticnest(block=int(sector) * 4)
            except Exception as exc:                   # noqa: BLE001
                self._on_device_log("warn", f"设备采集失败（{exc}），回落固件级联")
        try:
            self._crack_job = self._start_crack_job(sector=sector)
            return self._crack_job
        except Exception as exc:                       # noqa: BLE001
            self._on_device_log("err", f"启动后台破解失败：{exc}")
            return ""

    def read_block(self, block: int, key_type: int = 0) -> dict:
        def work():
            data = self.device.read_block(int(block), int(key_type))
            return {"block": int(block), "hex": data.hex(" ").upper()}
        return self._guard(work)

    def read_block_detail(self, block: int, key_type: int = 0) -> dict:
        """读单块并回传原始帧 + 解析结果（排查异常用）。"""
        def work():
            started = time.time()
            data = self.device.read_block_verbose(int(block), int(key_type))
            return {
                "block": int(block),
                "hex": data.hex(" ").upper(),
                "raw": getattr(self.device, "_last_read_raw", ""),
                "elapsed": round(time.time() - started, 2),
            }
        return self._guard(work)

    def write_block(self, block: int, data_hex: str, key_hex: str = "",
                    key_type: int = 0, verify: bool = True) -> dict:
        # 尾块（3/7/…/63）是密钥 + 权限位：写错这张卡就废了。
        # 默认只写数据块；需要克隆整卡时把设置里的 write_skip_trailer 关掉即可。
        try:
            blk_i = int(block)
        except (TypeError, ValueError):
            blk_i = -1
        if (blk_i >= 0 and blk_i % 4 == 3
                and self.settings.data.get("write_skip_trailer", True)):
            return self._ok({"block": blk_i, "skipped": True,
                             "reason": "尾块是密钥/权限区，按设置跳过写入（防止把卡写废）"})

        def work():
            key, _kt, _src = self._resolve_key(key_hex, key_type, block=int(block))
            started = time.time()
            res = self.device.write_block(int(block), hex_to_bytes(data_hex),
                                          key, _kt, verify=bool(verify))
            res["elapsed"] = round(time.time() - started, 2)
            self._on_device_log(
                "ok", f"第 {block} 块写入成功（帧格式 {res['format']}，"
                      f"回读{'一致' if res.get('verified') else '未校验'}，"
                      f"密钥来源 {_src}，耗时 {res['elapsed']}s）")
            return res
        return self._guard(work)

    def write_block_detail(self, block: int, data_hex: str, key_hex: str = "",
                           key_type: int = 0, verify: bool = True) -> dict:
        """写单块并回传写帧 / 读帧原文，方便对着日志定位固件协议差异。"""
        def work():
            key, _kt, _src = self._resolve_key(key_hex, key_type, block=int(block))
            res = self.device.write_block_verbose(
                int(block), hex_to_bytes(data_hex), key, _kt, verify=bool(verify))
            _bytes = res["write"]
            self._on_device_log("ok", f"第 {block} 块写块诊断：格式 {_bytes['format']}、"
                                      f"回读 {_bytes.get('read_back', '—')}、密钥来源 {_src}")
            return res
        return self._guard(work)

    def dump_card(self, include_trailers: bool = True, key_hex: str = "",
                  key_type: int = -1, job: str = "dump") -> dict:
        def progress(done: int, total: int, block: int) -> None:
            self._progress_push(job, done, total, f"读取第 {block} 块", block=block)

        def work():
            # 统一密钥解析：会话破解 → 本卡历史库 → 密码库 → 界面输入 → 默认，
            # 逐把真机认证，避免旧的「界面默认密钥覆盖破解结果」导致 0/64。
            key, kt, src = self._resolve_key(key_hex, key_type, block=0)
            started = time.time()
            res = self.device.dump_card(key, kt, bool(include_trailers),
                                        sector_keys=dict(self._cracked_keys),
                                        progress=progress)
            res["elapsed"] = round(time.time() - started, 2)
            res["key_source"] = src
            res["key_used"] = key.hex().upper()
            self._on_device_log(
                "ok", f"整卡读取完成：{res['ok_count']}/{res['total']} 块，"
                      f"失败 {len(res['errors'])} 块，耗时 {res['elapsed']}s"
                      f"（密钥 {res['key_used']} · 来源 {src}）")
            # 读完卡自动破解（v3.2.0）：有读不出来的块说明还有扇区没解，
            # 立刻在后台按当前破解模式级联解开，用户不用再手点一次「破解」。
            try:
                if (self.settings.data.get("auto_crack", True)
                        and int(res.get("ok_count") or 0) > 0
                        and (res.get("errors") or [])):
                    self._on_device_log("info", "整卡读取有失败块，自动开始破解（读完卡即破解）…")
                    self._maybe_auto_crack(self._last_card or {})
            except Exception as exc:                   # noqa: BLE001
                self._on_device_log("warn", f"自动破解未能启动：{exc}")
            return res
        return self._guard(work)

    # ------------------------------------------------------------ 破解
    def crack_methods(self) -> dict:
        return self._ok({"methods": CRACK_METHODS, "chain": list(CRACK_CHAIN),
                         "engine": self._crack_engine.status()})

    def crack_engine_status(self) -> dict:
        """破解引擎状态：当前模式 / 执行链 / 本地算法可用性 / 云端配置。"""
        self._crack_engine.bind(self.settings.data)
        return self._guard(lambda: self._crack_engine.status())

    def crack_vectors(self, limit: Optional[int] = None) -> dict:
        """跑官方 mfkey32 测试向量（本地离线算法自检，界面「验证本地算法」按钮用）。"""
        return self._guard(
            lambda: self._crack_engine.local.run_vectors(
                int(limit) if limit else None)
        )

    def crack(self, method: str, sector: int = 0, key_type: int = 0,
              timeout: float = 90.0) -> dict:
        """单方式同步破解（界面上的"手动破解"按钮用）。"""
        return self._guard(
            lambda: self.device.crack(method, int(sector), int(key_type), float(timeout))
        )

    def _collect_nonces(self, nonces: Any = None) -> list:
        """汇总 mfkey32 采集数据：显式入参 > 设备采集缓存 > 设置里手工粘贴的文本。"""
        raw = nonces
        if not raw:
            raw = getattr(self.device, "last_nonces", "") or ""
        if not raw:
            raw = self.settings.data.get("crack_nonces", "")
        try:
            frames = parse_frames(raw)
        except Exception as exc:                       # noqa: BLE001
            self._on_device_log("warn", f"mfkey32 采集数据解析失败：{exc}")
            return []
        if frames:
            self._on_device_log("info", f"已载入 {len(frames)} 帧 mfkey32 采集数据")
        return frames

    def _finish_crack_ok(self, sector: int, res: dict) -> None:
        """破解命中后的统一收尾：缓存密钥、推事件、写卡夹。"""
        key = (res.get("keys") or [res.get("key", "")])[0]
        self._cracked_keys[sector] = key
        self._crack_result = res
        engine = res.get("engine") or ""
        self._push("crack", {
            "stage": "done", "ok": True, "sector": sector, "key": key,
            "keys": res.get("keys") or [key], "method": res.get("method", ""),
            "label": res.get("label", ""), "elapsed": res.get("elapsed", 0),
            "attempts": res.get("attempts", []), "engine": engine,
            "mode": res.get("mode", ""), "nonces": res.get("nonces", 0),
            "detail": res.get("detail", ""),
        })
        self._on_device_log(
            "ok", f"破解成功：扇区 {sector} 密钥 {key}"
                  f"（方式 {res.get('label') or res.get('method') or engine}，"
                  f"耗时 {res.get('elapsed', 0)}s）")
        self._save_crack_to_card(sector, key, res)

    def _finish_crack_fail(self, sector: int, res: dict) -> None:
        self._push("crack", {"stage": "done", "ok": False, "sector": sector,
                             "summary": res.get("summary", ""),
                             "attempts": res.get("attempts", []),
                             "elapsed": res.get("elapsed", 0),
                             "mode": res.get("mode", ""),
                             "nonces": res.get("nonces", 0)})
        self._on_device_log("err", f"破解未成功：{res.get('summary', '')}")

    # ---- 全卡破解（sector = -1）----------------------------------------
    def _crack_all_sectors(self, mode: str = MODE_CLOUD,
                           timeout: float = 25.0, uid: str = "",
                           progress: Optional[Callable[[int, str], None]] = None,
                           cancelled: Optional[Callable[[], bool]] = None) -> dict:
        """全卡破解（对齐官方）：候选密钥全表 → 逐扇区批量验证（命中即停）。

        之所以要这样：老实现只按 settings.crack_sector（默认 0）解 0 扇区，
        和官方"把整卡 16 个扇区都解出来"的行为不一致 —— 用户看到的就是
        "只破解了 0 扇区"。这里改为：先把会话/历史/密码库/云端 keygen 的候选
        密钥汇总成一张表，再逐扇区用 device.auth() 实测，命中即记，
        并把已经命中的密钥优先复用到后续扇区（同一张卡通常同一把密钥开全部扇区）。
        """
        def emit(pct: int, msg: str) -> None:
            if progress:
                try:
                    progress(int(pct), msg)
                except Exception:                      # noqa: BLE001
                    pass

        def stopped() -> bool:
            return bool(cancelled and cancelled())

        uid = str(uid or (self._last_card or {}).get("uid") or "").replace(" ", "").upper()
        if not uid:
            raise NfcError("未读到卡片 UID，请先把卡放到感应区并寻卡")

        total_sectors = 16
        started = time.time()
        attempts: list = []
        found: dict = {}
        base_kt = int(self.settings.data.get("default_key_type", 0))

        # 候选分两批，按"先便宜后昂贵"的顺序：本地 → 云端 keygen。
        # 默认密钥卡 / 空白卡用本地候选几秒就能解完整卡，不必先白等十几秒云端往返。
        local = self._dedup_candidates(self._key_candidates(""))
        if not local:
            raise NfcError("没有可用候选密钥：请先执行云端破解或手动添加密钥")
        self._on_device_log("info", f"全卡破解：本地候选密钥 {len(local)} 把")

        known: list = []          # 已验证可用的 (key, key_type)，优先复用到其他扇区
        # 时间预算：16 个扇区 × 500+ 候选逐把认证是"几分钟"级的重活，
        # 必须给出上限，超时就带着已解出的部分结果收工，不能无限期卡住界面。
        budget = float(self.settings.data.get("crack_all_budget", 300) or 300)
        deadline = started + max(30.0, budget)
        unsolved = set(range(total_sectors))
        kt_order = [base_kt, 1 - base_kt]
        candidates_total = len(local)
        # 批量认证：一次写出去 BATCH_SIZE 条候选，再统一收应答（实测约 1.1x 提速；
        # 固件本身仍是串行处理，省下的是主机侧往返）。漏应答即退回串行。
        BATCH_SIZE = 8
        batch_state = {"ok": bool(self.settings.data.get("crack_batch_auth", True))}
        # 设备端批量验证（0x40 灌密钥表 + 0x41 让设备自己试）：候选表够大时优先走它，
        # 固件明确拒绝或异常就整体退回主机逐把验证。
        try:
            _caps = self.device.capabilities() or {}
        except Exception:                          # noqa: BLE001
            _caps = {}
        # 手机读卡器没有 0x40/0x41（key_cache），能力矩阵里就是 False，
        # 这里必须一起看，否则会白跑一轮设备端批量验证再报「不可用」。
        device_state = {"ok": bool(self.settings.data.get("crack_device_cache", True))
                        and bool(_caps.get("key_cache", True))}
        DEVICE_MIN_KEYS = 8      # 设备端批量验证实测比主机逐把快 ~15x，8 把起就值得走

        def _try(sector: int, key_hex: str, kt: int) -> bool:
            try:
                kb = hex_to_bytes(key_hex)
            except Exception:                          # noqa: BLE001
                return False
            if len(kb) != 6:
                return False
            try:
                self.device.auth(sector * 4, kb, kt)
            except Exception:                          # noqa: BLE001
                return False
            found[sector] = key_hex
            self._cracked_keys[sector] = key_hex
            unsolved.discard(sector)
            return True

        def _sweep_known() -> None:
            """新拿到一把能用的钥匙后，立刻复扫所有还没解的扇区。"""
            for key_hex, kt in list(known):
                for s in sorted(unsolved):
                    if stopped() or time.time() > deadline:
                        return
                    if _try(s, key_hex, kt):
                        self._on_device_log(
                            "ok", f"扇区 {s} 复用已确认密钥 {key_hex}"
                                  f"（{'KEY B' if kt else 'KEY A'}）")

        def run_pass(keys: list) -> None:
            """对未解扇区做一轮验证：先复用已知密钥，再按 KEY A / KEY B 顺序扫候选表。

            候选表默认用"批量认证"发（多条请求一次写完再一起收，实测比逐把串行
            快约 10%~15%）；只要设备出现漏应答就退回逐把串行，保证不漏钥匙。
            """
            tried = {"n": 0}

            def device_search(sector: int, kt: int):
                """0x40 + 0x41：把候选表灌进设备、让设备自己试出该块密钥。

                命中返回 (key_hex, 来源标签, kt)；不可用/未命中返回 None（上层回退主机验证）。
                """
                if not device_state["ok"] or len(keys) < DEVICE_MIN_KEYS:
                    return None
                for start in range(0, len(keys), KEY_CACHE_MAX):
                    if stopped() or time.time() > deadline:
                        return None
                    chunk = keys[start:start + KEY_CACHE_MAX]
                    try:
                        res = self.device.device_batch_verify(
                            [text for _tag, text in chunk], block=sector * 4,
                            key_type=kt, timeout=30.0)
                    except Exception as exc:               # noqa: BLE001
                        self._on_device_log(
                            "warn", f"设备端批量验证不可用（{exc}），改用主机逐把验证")
                        device_state["ok"] = False
                        return None
                    if res.get("ok"):
                        key = str(res.get("key") or "").replace(" ", "").upper()
                        tag = next((t for t, txt in chunk if txt == key), "设备端批量验证")
                        self._on_device_log(
                            "ok", f"扇区 {sector}：设备端批量验证命中 {key}"
                                  f"（{'KEY B' if kt else 'KEY A'}，本批 {len(chunk)} 把）")
                        return (key, tag, kt)
                    if res.get("status") in (0x02, 0x04, 0x11):
                        # 固件明确回"地址/长度/卡片"错误 → 这套指令在当前固件不可用
                        self._on_device_log(
                            "warn", f"设备端批量验证被拒（{res.get('detail')}），"
                                    f"改用主机逐把验证")
                        device_state["ok"] = False
                        return None
                    tried["n"] += len(chunk)
                    emit(int(len(found) / total_sectors * 100),
                         f"扇区 {sector} · 设备端批量验证 {tried['n']}/{len(keys)} 把未命中")
                return None

            def scan(sector: int):
                """扫候选表，返回命中的 (key_hex, 来源标签, key_type) 或 None。"""
                for kt in kt_order:
                    got = device_search(sector, kt)     # ① 优先交给设备自己试（最快）
                    if got:
                        return got
                    idx, total = 0, len(keys)
                    while idx < total:
                        if stopped() or time.time() > deadline:
                            return None
                        # ① 批量路径：一次发一批，再统一收应答
                        if batch_state["ok"] and total - idx >= BATCH_SIZE:
                            chunk = keys[idx:idx + BATCH_SIZE]
                            kbs = []
                            for _tag, text in chunk:
                                try:
                                    kb = hex_to_bytes(text)
                                except Exception:          # noqa: BLE001
                                    kb = None
                                kbs.append(kb if (kb and len(kb) == 6) else None)
                            if all(kbs):
                                codes = self.device.auth_batch(
                                    sector * 4, kbs, kt, timeout=8.0)
                                if any(c == -1 for c in codes):
                                    batch_state["ok"] = False   # 有漏应答 -> 后面改串行
                                else:
                                    tried["n"] += len(chunk)
                                    for (tag, text), code in zip(chunk, codes):
                                        if code == 0:
                                            return text, tag, kt
                                    idx += len(chunk)
                                    if tried["n"] % (BATCH_SIZE * 5) < BATCH_SIZE:
                                        emit(int(len(found) / total_sectors * 100),
                                             f"扇区 {sector} · {'KEY B' if kt else 'KEY A'} · "
                                             f"已试 {tried['n']} 把 / 候选 {len(keys)} 把")
                                    continue
                        # ② 串行路径（回退 / 末尾不足一批）
                        tag, text = keys[idx]
                        idx += 1
                        tried["n"] += 1
                        if tried["n"] % 20 == 0:
                            # 单次认证约 0.2s，长表扫描期间也要给进度，不然像卡死
                            emit(int(len(found) / total_sectors * 100),
                                 f"扇区 {sector} · {'KEY B' if kt else 'KEY A'} · "
                                 f"已试 {tried['n']} 把 / 候选 {len(keys)} 把")
                        if _try(sector, text, kt):
                            return text, tag, kt
                return None

            for sector in sorted(unsolved):
                if stopped() or time.time() > deadline or not unsolved:
                    return
                emit(int(len(found) / total_sectors * 100),
                     f"验证扇区 {sector}（已解 {len(found)}/{total_sectors}）")
                hit = ""
                for key_hex, kt in list(known):        # ① 已知可用密钥优先
                    if _try(sector, key_hex, kt):
                        hit = key_hex
                        self._on_device_log(
                            "ok", f"扇区 {sector} 复用密钥 {key_hex}"
                                  f"（{'KEY B' if kt else 'KEY A'}）")
                        break
                if not hit:
                    got = scan(sector)                 # ② 候选全表（先 A 全表，再 B 全表）
                    if got:
                        text, tag, kt = got
                        hit = text
                        if (text, kt) not in known:
                            known.append((text, kt))
                        self._on_device_log(
                            "ok", f"扇区 {sector} 密钥 {text}"
                                  f"（{'KEY B' if kt else 'KEY A'}，来源：{tag}）")
                        attempts.append({"method": tag, "label": f"扇区 {sector}",
                                         "ok": True, "detail": text})
                if hit:
                    _sweep_known()
                else:
                    attempts.append({"method": "miss", "label": f"扇区 {sector}",
                                     "ok": False, "detail": "本轮候选密钥均未通过认证"})

        # ---- 破解轮次顺序跟随破解模式：选「云端」就先走官方 keygen ----
        # 旧实现无论哪种模式都先跑本地字典，用户选了云端却看不到云端动作，
        # 这就是「明明选的是云端破解、实际却是本地字典」的来源。
        cloud_note = ""

        def cloud_round() -> str:
            nonlocal candidates_total
            if stopped() or time.time() > deadline:
                return "已取消 / 已超时，未查询云端"
            emit(int(len(found) / total_sectors * 100), "查询官方云端密钥库（keygen）…")
            try:
                kg = cloud_keygen_lookup(uid, timeout=max(8.0, float(timeout)))
            except Exception as exc:                   # noqa: BLE001
                note = f"云端密钥库不可用（{exc}）"
                self._on_device_log("warn", note)
                return note
            cloud = self._dedup_candidates(
                [("云端密钥库", k) for k in (kg.get("keys") or [])],
                exclude={k for k, _ in known})
            if not cloud:
                note = "云端密钥库没有返回候选密钥（该 UID 未收录）"
                self._on_device_log("warn", note)
                return note
            candidates_total += len(cloud)
            note = f"云端密钥库返回 {len(cloud)} 把候选密钥"
            self._on_device_log("ok", note + f"，逐个验证剩余 {len(unsolved)} 个扇区")
            run_pass(cloud)
            return note

        if mode == MODE_CLOUD:
            cloud_note = cloud_round()
            if unsolved and not stopped() and time.time() < deadline:
                self._on_device_log("info", "云端候选未覆盖全部扇区，继续用本地候选密钥验证")
                run_pass(local)
        else:
            run_pass(local)
            if unsolved and not stopped() and time.time() < deadline:
                cloud_note = cloud_round()

        if unsolved and (stopped() or time.time() > deadline):
            self._on_device_log(
                "warn", f"全卡验证已达时间上限（{budget:.0f}s），"
                        f"剩余扇区 {sorted(unsolved)} 未解，可稍后重试")

        keys_sorted = sorted(set(found.values()))
        ok = bool(found)
        if ok:
            try:
                self.keybox.record_history(uid, keys_sorted,
                                           card_type=(self._last_card or {}).get("card_type", ""),
                                           note="全卡破解命中")
            except Exception:                          # noqa: BLE001
                pass
            if self._last_card_id:
                try:
                    self.cardbox.set_keys(
                        self._last_card_id,
                        {str(k): v for k, v in found.items()},
                        meta={"method": "all_sectors", "elapsed": round(time.time() - started, 2)})
                except Exception:                      # noqa: BLE001
                    pass
        emit(100, f"全卡验证结束：{len(found)}/{total_sectors} 个扇区命中")
        return {
            "ok": ok, "all_sectors": True, "method": "all_sectors", "label": "全卡破解",
            "sector": -1, "sector_keys": {str(k): v for k, v in sorted(found.items())},
            "keys": keys_sorted, "key": keys_sorted[0] if keys_sorted else "",
            "sectors_hit": len(found), "sectors_total": total_sectors,
            "candidates": candidates_total, "attempts": attempts,
            "elapsed": round(time.time() - started, 2), "nonces": 0, "mode": mode,
            "detail": f"全卡破解：{len(found)}/{total_sectors} 扇区命中，"
                      f"{len(keys_sorted)} 把不同密钥（候选 {candidates_total} 把）",
            "cloud_note": cloud_note,
            "summary": (f"全卡破解完成：{len(found)}/{total_sectors} 个扇区命中"
                        if ok else "全卡破解未命中任何扇区"),
        }

    def _start_crack_job(self, sector: int = 0, methods: Optional[list] = None,
                         timeout: Optional[float] = None, mode: Optional[str] = None,
                         nonces: Any = None) -> str:
        """启动破解任务：默认云端解算，失败/超时自动降级本地 mfkey32，再兜底固件级联。"""
        if not self.device.connected:
            raise NfcError("设备未连接，无法破解")
        sector = int(sector)
        kt = int(self.settings.data.get("default_key_type", 0))
        timeout = float(timeout or self.settings.data.get("crack_timeout", 25))
        firmware_chain = list(methods or CRACK_CHAIN)

        self._crack_engine.bind(self.settings.data)
        mode = self._crack_engine.mode(mode)
        engine_chain = self._crack_engine.chain_for(mode)
        frames = self._collect_nonces(nonces)

        self._push("crack", {"stage": "start", "sector": sector, "chain": engine_chain,
                             "total": len(engine_chain), "mode": mode,
                             "labels": dict(ENGINE_LABELS),
                             "engine_labels": dict(ENGINE_LABELS),
                             "nonces": len(frames),
                             "firmware_chain": firmware_chain,
                             "cloud": self._crack_engine.status(mode)["cloud"]})
        self._on_device_log(
            "info", f"破解模式：{'云端解算' if mode == MODE_CLOUD else '本地 mfkey32'}，"
                    f"执行链 {' → '.join(ENGINE_LABELS.get(x, x) for x in engine_chain)}")

        def on_attempt(method: str, ok: bool, cost: float, detail: str) -> None:
            self._push("crack", {
                "stage": "attempt", "method": method,
                "label": CRACK_LABELS.get(method, method), "ok": bool(ok),
                "elapsed": cost, "detail": detail,
            })
            if not ok:
                self._on_device_log(
                    "warn", f"{CRACK_LABELS.get(method, method)} 失败（{cost}s）：{detail}")

        def body(progress, cancelled):
            def p(i: int, n: int, msg: str) -> None:
                pct = int(round(i / n * 100)) if n else 0
                progress(pct, msg)

            def _verify_key(k: str) -> bool:
                """把云端候选密钥拿到设备上真认证一次（KEY A 不通自动试 KEY B）。"""
                try:
                    kb = hex_to_bytes(k)
                except Exception:                      # noqa: BLE001
                    return False
                if len(kb) != 6:
                    return False
                for _kt in (kt, 1 - kt):
                    try:
                        self.device.auth(int(sector) * 4, kb, _kt)
                        self._cracked_keys[int(sector)] = str(k).upper()
                        return True
                    except Exception:                  # noqa: BLE001
                        continue
                return False

            # 0) 全卡模式（sector < 0）：候选密钥全表 → 逐扇区批量验证，
            #    与官方"整卡所有扇区都解出来"的行为一致。
            if sector < 0:
                res = self._crack_all_sectors(
                    mode=mode, timeout=timeout, uid=(self._last_card or {}).get("uid", ""),
                    progress=lambda pct, msg: progress(int(pct), msg), cancelled=cancelled)
                if res.get("ok"):
                    self._crack_result = res
                    self._push("crack", {
                        "stage": "done", "ok": True, "sector": -1,
                        "key": res.get("key", ""), "keys": res.get("keys") or [],
                        "method": "all_sectors", "label": "全卡破解",
                        "elapsed": res.get("elapsed", 0), "attempts": res.get("attempts", []),
                        "sector_keys": res.get("sector_keys") or {},
                        "detail": res.get("detail", ""), "mode": mode, "nonces": 0,
                    })
                    self._on_device_log(
                        "ok", f"全卡破解成功：{res['sectors_hit']}/{res['sectors_total']} "
                              f"个扇区命中，密钥 {', '.join(res['keys'])}")
                else:
                    self._finish_crack_fail(-1, res)
                return res

            # 1) 云端解算 → 本地离线 → 自动降级（crack_engine 内部完成）
            started = time.time()
            res = self._crack_engine.solve(
                mode=mode, frames=frames, sector=sector,
                progress=lambda pct, msg: progress(int(pct), msg),
                should_stop=cancelled,
                verify=_verify_key,
                on_engine=lambda engine, ok, detail: on_attempt(
                    engine, ok, round(time.time() - started, 2), detail),
                allow_device_fallback=True)
            if res.get("ok"):
                self._finish_crack_ok(sector, res)
                return res
            if not res.get("fallback_to_device"):
                self._finish_crack_fail(sector, res)
                return res

            # 2) 固件级联兜底（老链路不变；methods/timeout 语义保持一致）
            self._on_device_log("warn", "改用固件级联破解…")
            fw = self.device.crack_chain(sector=sector, key_type=kt,
                                         methods=firmware_chain, timeout=timeout,
                                         progress=p, should_stop=cancelled,
                                         on_attempt=on_attempt)
            fw["attempts"] = list(res.get("attempts") or []) + list(fw.get("attempts") or [])
            fw["mode"] = mode
            fw["nonces"] = len(frames)
            fw["engine"] = ""
            fw["fallback_to_device"] = True
            if fw.get("ok"):
                self._finish_crack_ok(sector, fw)
            else:
                self._finish_crack_fail(sector, fw)
            return fw

        return self.device.start_job("crack", body, meta={"sector": sector})

    def _save_crack_to_card(self, sector: int, key: str, res: dict) -> None:
        """把破解结果写回卡夹里最近一次复制的那张卡（若有）。"""
        if not self._last_card_id:
            return
        try:
            with self._store_lock:
                self.cardbox.set_keys(self._last_card_id, {str(sector): key},
                                      meta={"method": res.get("method", ""),
                                            "elapsed": res.get("elapsed", 0)})
            self._on_device_log("info", f"破解密钥已写入卡夹记录（扇区 {sector}）")
        except Exception as exc:                       # noqa: BLE001
            self._on_device_log("warn", f"破解密钥写入卡夹失败：{exc}")

    def crack_start(self, sector: int = 0, methods: Optional[list] = None,
                    timeout: Optional[float] = None, mode: Optional[str] = None,
                    nonces: Any = None) -> dict:
        """手动启动后台破解，返回 job handle，前端轮询 job_status。
        默认云端解算，失败/超时自动降级本地 mfkey32；mode="local" 强制本地离线。"""
        def work():
            handle = self._start_crack_job(sector, methods, timeout, mode, nonces)
            self._crack_job = handle
            return {"handle": handle, "sector": int(sector),
                    "mode": self._crack_engine.mode(mode)}
        return self._guard(work)

    def job_status(self, handle: str) -> dict:
        return self._guard(lambda: self.device.job_status(str(handle)))

    def job_cancel(self, handle: str) -> dict:
        return self._guard(lambda: self.device.cancel_job(str(handle)))

    def detect_bug_card(self) -> dict:
        return self._guard(self.device.detect_bug_card)

    def write_uid(self) -> dict:
        return self._guard(self.device.write_uid)

    # ------------------------------------------------------------ P0 · 独立写卡流程
    # 命令语义对齐微信小程序「宜新 NFC Reader Tool」：
    #   0x0C 写卡号 / 0x16 极速 / 0x18 普通 / 0x08 带密钥类型写块 / 0x26 读 ID 卡
    #   0x24 卡号校验（单字节）/ 0x27 写 UID·生成转接卡 / 0x17 封卡 / 0x38 自动识别
    def write_modes(self) -> dict:
        """写卡档位表（fast=0x16 极速 / normal=0x18 普通 / keyed=0x08 带密钥类型）。"""
        return self._ok({"modes": write_mode_table(),
                         "phone_modes": [m for m in PHONE_WRITE_MODES]})

    def read_id_card(self) -> dict:
        """B1 · 0x26 读 ID 卡 / 低频卡，返回卡号与频率档位（125/250/375/500 kHz）。"""
        def work():
            info = self.device.read_id_card()
            # HID_ENABLE：低频卡里 HID 卡用 card_type 标识位宽（HID 26bit…37bit），
            # 开关打开时把位宽解出来给界面显示，关闭时只报卡号/频率。
            hid_on = bool(self.settings.data.get("hid_enable"))
            info["hid_enabled"] = hid_on
            if hid_on:
                try:
                    ct = int(info.get("card_type_id") or 0)
                    info["hid_bit_width"] = {1: 26, 2: 27, 3: 28, 4: 29, 5: 30,
                                             6: 31, 7: 32, 8: 33, 9: 34, 10: 35,
                                             11: 36, 12: 37}.get(ct, 0)
                    info["hid_text"] = (f"HID {info['hid_bit_width']}bit"
                                        if info.get("hid_bit_width") else "非 HID / 位宽未知")
                except Exception:
                    info["hid_text"] = "HID 解析失败"
            self._on_device_log(
                "ok", f"读 ID 卡：{info['id_hex']} · 频率 {info['freq']} · "
                      f"{info['card_type']} · 卡代码 {info['card_code']} · "
                      f"设施码 {info['facility_code']}"
                      + (f" · {info.get('hid_text')}" if hid_on else ""))
            return info
        return self._guard(work)

    def check_card_no(self, card_no: str) -> dict:
        """A3 · 写前卡号校验：读回当前卡号与待写卡号比对，不写入。"""
        return self._guard(lambda: self.device.check_card_no(card_no))

    def write_card_no(self, card_no: str, verify: bool = True) -> dict:
        """A1 · 0x0C 单独写卡号（ID 卡 / 转接卡），写后回读比对。"""
        def work():
            res = self.device.write_card_no(card_no, verify=bool(verify))
            self._on_device_log(
                "ok", f"写卡号完成：{res['card_no']}"
                      f"（{res.get('verify_text') or '未校验'}，耗时 {res['elapsed']}s）")
            return res
        return self._guard(work)

    def write_block_mode(self, block: int, data_hex: str, mode: str = "normal",
                         key_hex: str = "", key_type: int = 0,
                         verify: bool = True) -> dict:
        """A2 · 按档位写单块（fast=0x16 极速 / normal=0x18 / keyed=0x08 带密钥类型）。"""
        def work():
            mode_key, conf = resolve_write_mode(mode)
            key, kt, _src = self._resolve_key(key_hex, key_type, block=int(block))
            res = self.device.write_block_mode(
                int(block), hex_to_bytes(data_hex), mode_key, key, kt,
                verify=bool(verify))
            self._on_device_log(
                "ok", f"{conf['label']} 完成（{res['cmd']} · 第 {block} 块）："
                      f"{res.get('verify_text') or '未校验'}，密钥来源 {_src}，"
                      f"耗时 {res['elapsed']}s")
            return res
        return self._guard(work)

    def write_card(self, card_no: str = "", mode: str = "normal",
                   blocks: Optional[dict] = None, verify: bool = True,
                   check_card_no: bool = True, key_hex: str = "",
                   key_type: int = 0, job: str = "write") -> dict:
        """A1+A2+A3 · 独立写卡流程（前端「开始写卡」按钮）。

        card_no：ID 卡 / 转接卡待写卡号（0x0C）
        blocks ：IC 卡待写块数据 {"块号": "32位hex"}（0x16/0x18/0x08）
        """
        def work():
            mode_key, conf = resolve_write_mode(mode)
            key, kt, _src = self._resolve_key(key_hex, key_type, block=0)
            nonlocal card_no, check_card_no
            try:
                caps = self.device.capabilities() or {}
            except Exception:                      # noqa: BLE001
                caps = {}
            if not caps.get("read_id_card", True):
                # 手机读卡器没有 125kHz 天线：0x0C 写卡号 / 0x26 读 ID 卡都做不了，
                # 强行校验卡号只会得到「卡号校验未通过」。这里直接跳过卡号环节，
                # 只做 0x27 生成转接卡 + 写 IC 数据（小程序第二步/第三步）。
                if card_no:
                    self._on_device_log(
                        "warn", "手机读卡器读不了 125kHz ID 卡，已自动跳过「写卡号」步骤，"
                                "直接写 IC 数据")
                card_no = ""
                check_card_no = False
            started = time.time()
            self._progress_push(job, 0, 1, f"准备{conf['label']}…")
            res = self.device.write_card(
                mode_key, card_no or None, blocks or {}, verify=bool(verify),
                check_card_no=bool(check_card_no), key=key, key_type=kt,
                progress=lambda d, t, msg: self._progress_push(job, d, t, msg))
            res["elapsed"] = round(time.time() - started, 2)
            self._progress_push(job, 1, 1, "写卡流程结束")
            ok_txt = "成功" if res.get("ok") else "完成但存在未通过项"
            self._on_device_log(
                "ok" if res.get("ok") else "warn",
                f"{conf['label']} · {ok_txt}：{conf['label']} 共 {res['total']} 步，"
                f"卡类型 {res.get('class_label') or '—'}，"
                f"耗时 {res['elapsed']}s")
            return res
        return self._guard(work)

    # ------------------------------------------------------------ P1 · 手机 / 手环 NFC 写录
    # 与「写 UID 卡 / 魔术卡（0x27 改写卡片 UID）」「封卡（0x17，不可逆）」严格区分：
    # 本流程是「转接卡」两步法 —— 0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡
    # → 0x16/0x18 写 IC 数据 → 0x07 回读校验，手机 / 手环贴近即可读到该卡号。
    def phone_nfc_steps(self) -> dict:
        """手机 / 手环 NFC 写录步骤清单 + 可用档位（前端卡片渲染用）。"""
        modes = [m for m in write_mode_table() if m["id"] in PHONE_WRITE_MODES]
        return self._ok({
            "steps": phone_nfc_plan(),
            "modes": modes,
            "cmd": "0x27",
            "note": ("转接卡两步法：0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡 → "
                     "0x16/0x18 写 IC 数据；区别于写 UID/魔术卡（0x27 改写卡 UID）"
                     "与封卡（0x17，不可逆）"),
        })

    def write_phone_nfc(self, uid: str = "", card_no: str = "", blocks: Optional[dict] = None,
                        mode: str = "fast", verify: bool = True,
                        check_card_no: bool = True, key_hex: str = "",
                        key_type: int = 0, prep_only: bool = False,
                        job: str = "phone_nfc") -> dict:
        """A4 · 写录手机 / 手环 NFC（前端「写入手机 / 手环」按钮）。

        uid      ：转接卡要生成的 6 字节 UID（hex，可空 → 设备自取）
        card_no  ：要模拟的 5 字节卡号（hex / 十进制均可）
        blocks   ：IC 数据 {"块号": "32位hex"}；prep_only=True 时忽略
        prep_only：仅做到「生成转接卡」（小程序第一步）
        """
        def work():
            mode_key, conf = resolve_write_mode(mode)
            if mode_key not in PHONE_WRITE_MODES:
                raise NfcError(f"手机 / 手环 NFC 档位仅支持 "
                               f"{' / '.join(PHONE_WRITE_MODES)}，当前 {mode}", 15)
            key, kt, _src = self._resolve_key(key_hex, key_type, block=0)
            nonlocal card_no, check_card_no
            try:
                caps = self.device.capabilities() or {}
            except Exception:                      # noqa: BLE001
                caps = {}
            if not caps.get("read_id_card", True):
                # 手机读卡器没有 125kHz 天线：0x0C 写卡号 / 0x26 读 ID 卡都做不了，
                # 强行校验卡号只会得到「卡号校验未通过」。跳过卡号环节，
                # 只做 0x27 生成转接卡 + 写 IC 数据（小程序第二、三步）。
                if card_no:
                    self._on_device_log(
                        "warn", "手机读卡器读不了 125kHz ID 卡，已跳过「写卡号」步骤，"
                                "直接生成转接卡并写 IC 数据")
                card_no = ""
                check_card_no = False
            started = time.time()
            self._progress_push(job, 0, 1, "准备手机 / 手环 NFC 写录…")
            res = self.device.write_phone_nfc(
                uid=uid or None, card_no=card_no or None, blocks=blocks or {},
                mode=mode_key, verify=bool(verify), check_card_no=bool(check_card_no),
                key=key, key_type=kt, prep_only=bool(prep_only),
                progress=lambda d, t, msg: self._progress_push(job, d, t, msg))
            res["elapsed"] = round(time.time() - started, 2)
            res["mode"] = mode_key
            res["mode_label"] = conf["label"]
            self._progress_push(job, 1, 1, "手机 / 手环 NFC 写录结束")
            self._on_device_log(
                "ok" if res.get("ok") else "warn",
                f"写手机 / 手环 NFC{'（仅转接卡）' if prep_only else ''}："
                f"{conf['label']} 共 {res['total']} 步，"
                f"{(res.get('verify_text') or '未校验')}，耗时 {res['elapsed']}s")
            return res
        return self._guard(work)

    def write_uid_card(self, uid: str = "", verify: bool = True) -> dict:
        """A4 · 0x27 写 UID / 生成转接卡（前端「写 UID / 生成转接卡」按钮）。

        与「手机 / 手环写录」区分：这里是单条 0x27 指令，直接改写卡片 UID
        （魔术卡 / 转接卡专用）；uid 为空时退化为单字节 0x27，由设备自取当前卡 UID。
        """
        def work():
            res = self.device.write_uid_card(uid or None, verify=bool(verify))
            self._last_card = dict(self.device._last_card or {})
            self._save_cache()
            self._on_device_log(
                "ok" if res.get("ok") else "err",
                f"0x27 写 UID / 生成转接卡：{res.get('payload', '')} → "
                f"{'成功' if res.get('ok') else res.get('status_text')}"
                + (f"；{res.get('verify_text')}" if res.get("verify_text") else ""))
            return res
        return self._guard(work)

    # ------------------------------------------------------------ P1 · 自动识别 / 一键破解扫描
    def auto_identify(self) -> dict:
        """B · 0x38 自动识别卡片（单字节），返回卡号 / 卡类型 / 卡代码 / 设施码。"""
        def work():
            try:
                caps = self.device.capabilities() or {}
            except Exception:                      # noqa: BLE001
                caps = {}
            if not caps.get("read_id_card", True):
                # 手机通道没有 0x38 的低频识别能力：退化成 0x11 寻卡，
                # 至少把 13.56MHz 的卡型 / UID / SAK 识别出来。
                info = self.device.find_card()
                info["auto_identify_partial"] = True
                info["auto_identify_note"] = ("手机读卡器只支持 13.56 MHz 卡（0x11 寻卡）；"
                                              "125kHz ID 卡的自动识别需要原厂读卡器硬件")
                self._last_card = info
                self._on_device_log(
                    "ok", f"识别卡片：{info.get('uid')} · {info.get('class_label')}"
                          f"（手机通道仅支持 13.56MHz）")
                return info
            info = self.device.auto_identify()
            self._last_card = dict(self.device._last_card or {})
            self._last_card["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_cache()
            self._on_device_log(
                "ok", f"自动识别：卡号 {info['id_hex']} · {info['card_type']} · "
                      f"卡代码 {info['card_code']} · 设施码 {info['facility_code']}")
            return info
        return self._guard(work)

    def card_no_valid(self, card_no: str = "") -> dict:
        """0x24 卡号校验：读卡器自行比对当前卡号（不写入）。"""
        return self._guard(lambda: self.device.card_no_valid(card_no or None))

    def crack_scan(self, auto_crack: bool = True, sector: int = 0, key_type: int = 0,
                   method: str = "", job: str = "scan") -> dict:
        """一键破解扫描：0x38 自动识别（失败回落 0x11 寻卡）→ 按卡类型分派破解。

        auto_crack=False 时只做识别与建议，不启动后台破解。
        """
        def work():
            if not self.device.connected:
                raise NfcError("读卡器未连接", 3)
            started = time.time()
            self._progress_push(job, 0, 1, "自动识别卡片…")
            info = self.device.crack_scan(
                auto_crack=bool(auto_crack), sector=int(sector),
                key_type=int(key_type), method=str(method or ""),
                progress=lambda d, t, msg: self._progress_push(job, d, t, msg))
            info["elapsed"] = round(time.time() - started, 2)

            card = info.get("card") or {}
            if info.get("identified") and info.get("id_card"):
                self._last_card = dict(self.device._last_card or {})
                self._last_card["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._save_cache()
            elif card:
                self._last_card = {
                    "uid": card.get("uid", ""), "card_type": card.get("card_type", ""),
                    "class": card.get("class", ""),
                    "class_label": card.get("class_label", ""),
                    "band": card.get("band", ""), "sak": card.get("sak", ""),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._cracked_keys = {}
                self._crack_result = None
                self._crack_job = ""
                self._save_cache()

            if auto_crack and info.get("crack_recommended"):
                handle = self._maybe_auto_crack(card)
                if handle:
                    info["crack_job"] = handle
            self._progress_push(job, 1, 1, "扫描结束")
            self._on_device_log(
                "ok", f"一键破解扫描（{info['source']}）："
                      f"{card.get('class_label') or '—'} · {info['crack_reason']}"
                      + (f"（后台任务 {info['crack_job']}）" if info.get("crack_job") else ""))
            return info
        return self._guard(work)

    # ------------------------------------------------------------ P1 · 封卡 / 锁卡（不可逆）
    def lock_ufuid(self, confirm: bool = False) -> dict:
        """0x17 封卡（UFUID 类魔术卡）。⚠ 不可逆，需 confirm=True 显式确认。"""
        def work():
            if not confirm:
                raise NfcError("封卡不可逆：请确认后在界面勾选「我已知晓」再执行", 15)
            res = self.device.lock_ufuid(verify=True)
            self._on_device_log(
                "warn" if res.get("ok") else "err",
                f"封卡（0x17）：{'完成，不可逆' if res.get('ok') else res.get('err')}"
                + (f"；{res.get('verify_text')}" if res.get("verify_text") else ""))
            return res
        return self._guard(work)

    def lock_card(self, confirm: bool = False) -> dict:
        """0x23 锁卡。⚠ 不可逆，需 confirm=True 显式确认。"""
        def work():
            if not confirm:
                raise NfcError("锁卡不可逆：请确认后在界面勾选「我已知晓」再执行", 15)
            res = self.device.lock_card()
            self._on_device_log(
                "warn" if res.get("ok") else "err",
                f"锁卡（0x23）：{'完成，不可逆' if res.get('ok') else res.get('err')}")
            return res
        return self._guard(work)

    # ------------------------------------------------------------ 卡夹
    def card_list(self) -> dict:
        cards = self.cardbox.list()
        brief = [{
            "id": c["id"], "name": c["name"], "uid": c.get("uid", ""),
            "created_at": c.get("created_at", 0),
            "card_type": c.get("card_type", ""),
            "class_label": c.get("class_label", ""),
            "block_count": len([v for v in (c.get("blocks") or {}).values() if v]),
            "keys": c.get("keys") or {},
        } for c in cards]
        return self._ok(brief)

    def card_get(self, card_id: str) -> dict:
        card = self.cardbox.get(card_id)
        if not card:
            return {"ok": False, "error": "卡夹中不存在该卡片"}
        return self._ok(card)

    def card_rename(self, card_id: str, name: str) -> dict:
        ok = self.cardbox.rename(card_id, name)
        return self._ok({"renamed": ok})

    def card_remove(self, card_id: str) -> dict:
        with self._store_lock:
            removed = self.cardbox.remove(card_id)
        return self._ok({"removed": removed})

    def card_export(self, card_id: str) -> dict:
        def work():
            card = self.cardbox.get(card_id)
            if not card:
                raise NfcError("卡夹中不存在该卡片")
            target = self._ask_save(f"{card['name']}.json")
            if not target:
                return {"canceled": True}
            self.cardbox.export(card_id, Path(target))
            return {"path": target}
        return self._guard(work)

    def card_import(self) -> dict:
        def work():
            src = self._ask_open([("卡片数据", "*.json")])
            if not src:
                return {"canceled": True}
            card = self.cardbox.import_file(Path(src))
            return {"id": card["id"], "name": card["name"]}
        return self._guard(work)

    def copy_card(self, name: str = "", crack: bool = True) -> dict:
        """一键复制卡：寻卡 → 识卡 → 整卡读取 → 存入卡夹（破解同时在后台跑）。"""
        def work():
            card = self.device.find_card()
            self._cracked_keys = {}
            self._crack_result = None
            self._crack_job = ""
            self._last_card = {
                "uid": card.get("uid", ""), "card_type": card.get("card_type", ""),
                "class": card.get("class", ""), "class_label": card.get("class_label", ""),
                "band": card.get("band", ""), "sak": card.get("sak", ""),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_cache()
            if crack and bool(self.settings.data.get("auto_crack", True)):
                handle = self._maybe_auto_crack(card)
                if handle:
                    card["crack_job"] = handle

            # 后台破解是异步的，此刻 _best_key() 多半还是默认密钥 →
            # 用统一解析挑一把真能通过认证的密钥再 dump，否则又是 0/64。
            _kb, kt, _src = self._resolve_key("", -1, block=0)
            best = _kb.hex().upper()

            def progress(done: int, total: int, block: int) -> None:
                self._progress_push("copy", done, total, f"复制中 · 第 {block} 块", block=block)

            started = time.time()
            dump = self.device.dump_card(
                hex_to_bytes(best) if isinstance(best, str) else best, kt,
                include_trailers=True, sector_keys=dict(self._cracked_keys),
                progress=progress)
            blocks = {str(k): v for k, v in dump["blocks"].items() if v}
            with self._store_lock:
                saved = self.cardbox.add(
                    name or f"{card.get('class_label') or card.get('card_type', 'IC 卡')} "
                            f"{card.get('uid', '')}",
                    card.get("uid", ""), blocks, keys={},
                    note=f"{card.get('card_type', '')} / SAK {card.get('sak', '')}",
                    card_type=card.get("card_type", ""),
                    class_label=card.get("class_label", ""))
            self._last_card_id = saved["id"]
            self._on_device_log(
                "ok", f"已复制 {len(blocks)}/{dump['total']} 个数据块，存入卡夹"
                      f"（耗时 {round(time.time() - started, 2)}s）")
            return {"card": {k: saved[k] for k in ("id", "name", "uid")},
                    "blocks": len(blocks), "errors": dump["errors"],
                    "sector_notes": dump.get("sector_notes", {}),
                    "crack_job": card.get("crack_job", ""),
                    "elapsed": round(time.time() - started, 2)}
        return self._guard(work)

    def copy_card_local(self, card_id: str) -> dict:
        """不接触读卡器的"卡到卡"准备：把卡夹数据载入内存备粘贴用。"""
        card = self.cardbox.get(card_id)
        if not card:
            return {"ok": False, "error": "卡夹中不存在该卡片"}
        return self._ok({"id": card["id"], "name": card["name"],
                         "blocks": len(card.get("blocks") or {})})

    def paste_card(self, card_id: str, write_trailers: bool = True,
                   key_hex: str = "") -> dict:
        """一键粘贴写卡：把卡夹数据写入当前卡片，逐块回读校验并回报进度。"""
        def work():
            card = self.cardbox.get(card_id)
            if not card:
                raise NfcError("卡夹中不存在该卡片")
            key, kt, _src = self._resolve_key(key_hex, -1, block=0)

            blocks = sorted(((int(k), v) for k, v in (card.get("blocks") or {}).items()
                             if v), key=lambda x: x[0])
            total = len(blocks)
            written, failed, skipped, formats = 0, [], 0, {}
            started = time.time()
            for idx, (block, hexstr) in enumerate(blocks, start=1):
                if not write_trailers and block % 4 == 3:
                    skipped += 1
                    self._progress_push("paste", idx, total, f"跳过尾块 {block}", block=block,
                                        started=started)
                    continue
                self._progress_push("paste", idx - 1, total, f"写入第 {block} 块",
                                    block=block, started=started)
                try:
                    res = self.device.write_block(block, hex_to_bytes(hexstr), key, kt)
                    written += 1
                    formats[res["format"]] = formats.get(res["format"], 0) + 1
                except NfcError as exc:
                    failed.append({"block": block, "error": str(exc)})
                self._progress_push("paste", idx, total, f"已完成第 {block} 块",
                                    block=block, started=started)
            elapsed = round(time.time() - started, 2)
            if formats:
                self._on_device_log("info", f"写块帧格式命中统计：{formats}")
            self._on_device_log(
                "ok", f"写卡完成：成功 {written} 块（回读已校验）/ 失败 {len(failed)} 块 / "
                      f"跳过 {skipped} 块，耗时 {elapsed}s")
            return {"written": written, "failed": failed, "total": total,
                    "skipped": skipped, "formats": formats, "elapsed": elapsed}
        return self._guard(work)

    def format_card(self, key_hex: str = "", key_type: int = 0,
                    confirm: bool = False) -> dict:
        """格式化卡片：用已知密钥把 16 个扇区的数据块逐块写 0，密钥块写成厂商默认值。

        对齐小程序「格式化卡片」——它本质上就是逐块写（0x08），不是独立指令。
        不可逆，必须显式 confirm=True。
        """
        if not confirm:
            raise NfcError("格式化会清空卡片数据且不可恢复，请先勾选确认")
        def work():
            if not self.device.connected:
                raise NfcError("请先连接读卡器")
            kb, key_type, key_src = self._resolve_key((key_hex or "").strip(),
                                                      int(key_type), block=0)
            default_trailer = bytes.fromhex("FFFFFFFFFFFF" "FF078069" "FFFFFFFFFFFF")
            written, failed = 0, []
            total = 64
            for block in range(total):
                data = default_trailer if (block % 4 == 3) else bytes(16)
                try:
                    self.device.write_block(block, data, key=kb, key_type=key_type)
                    written += 1
                except NfcError as exc:
                    failed.append({"block": block, "error": str(exc)})
                self._progress_push("format", block + 1, total,
                                    f"格式化块 {block}", block=block)
            self._on_device_log("ok" if not failed else "warn",
                                f"格式化完成：成功 {written}/{total} 块，失败 {len(failed)}"
                                f"（密钥 {kb.hex().upper()} · 来源 {key_src}）")
            return {"written": written, "failed": failed, "total": total,
                    "key": kb.hex().upper(), "key_source": key_src}
        return self._guard(work)
    def sniff_run(self, uid: str = "", sak: int = 0x08) -> dict:
        """侦测读卡（0x34 → 0x35 → 0x36），对齐小程序「侦测读卡」页。

        需要读卡器支持侦测（官方清单 sniff=true，NFC Max 2.0 支持）。
        """
        def work():
            if not self.device.connected:
                raise NfcError("请先连接读卡器")
            u = (uid or "").strip() or (self._last_card or {}).get("uid", "")
            self._on_device_log("info", f"侦测读卡开始（UID {u or '设备自取'}）")
            res = self.device.sniff_run(u, int(sak))
            self._on_device_log("ok" if res.get("ok") else "warn",
                                f"侦测读卡结束：{res.get('raw', '')}")
            return res
        return self._guard(work)
    def collect_nonces(self, method: str = "staticnest", block: int = 0) -> dict:
        """破解采集：设备采集原始密文，供云端算法解算（对齐小程序流程）。

        目前实现 staticnest（0x32）；hardnested / pang / fm11rf08s 待补齐。
        """
        def work():
            if not self.device.connected:
                raise NfcError("请先连接读卡器")
            m = (method or "staticnest").strip().lower()
            if m in ("staticnest", "staticnest2", "0x32"):
                res = self.device.collect_staticnest(block=block)
                self._on_device_log("ok" if res.get("ok") else "warn",
                                    f"staticnest 采集：{res.get('raw', '')}")
                return res
            raise NfcError(f"暂不支持的采集方式：{method}（当前支持 staticnest）")
        return self._guard(work)
    def firmware_list(self) -> dict:
        """官方固件清单（GET /get-devices，按型号列版本与下载地址）。"""
        def work():
            from .crack_engine import _vendor_request, vendor_urls, cloud_config
            conf = cloud_config(self.settings.data)
            urls = vendor_urls(conf["endpoint"])
            payload = _vendor_request(urls["base"] + "/get-devices",
                                      {"Accept": "application/json",
                                       AUTH_HEADER: conf["token"], "User-Agent": VENDOR_UA},
                                      None, conf["timeout"], method="GET")
            items = payload.get("data") if isinstance(payload, dict) else payload
            out = []
            for d in (items or []):
                if not isinstance(d, dict):
                    continue
                out.append({"name": d.get("name"), "version": d.get("version"),
                            "firmware": d.get("firmware"), "crc": d.get("crc"),
                            "sniff": d.get("sniff"), "key_cache": d.get("key_cache")})
            return {"items": out, "count": len(out)}
        return self._guard(work)

    def firmware_match(self) -> dict:
        """按当前设备固件版本匹配对应型号（对齐官方型号表）。"""
        def work():
            info = self.device.device_info() if self.device.connected else {}
            ver = info.get("version", "")
            res = self.firmware_list()
            items = res.get("data", {}).get("items", []) if isinstance(res.get("data"), dict) else []
            hit = next((x for x in items if str(x.get("version")) == str(ver)), None)
            return {"device_version": ver, "matched": hit, "items": items}
        return self._guard(work)

    def firmware_download(self) -> dict:
        """下载匹配型号的官方固件到本地（只下载、不刷写，避免变砖）。"""
        def work():
            import urllib.request
            from .paths import data_dir
            m = self.firmware_match()
            d = m.get("data", {}).get("matched") if isinstance(m.get("data"), dict) else None
            if not d or not d.get("firmware"):
                raise NfcError("未匹配到该固件版本对应的官方固件包")
            outdir = data_dir() / "firmware"
            outdir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{d.get('name')}_v{d.get('version')}.bin")
            dst = outdir / safe
            req = urllib.request.Request(d["firmware"], headers={"User-Agent": VENDOR_UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as fp:
                fp.write(r.read())
            size = dst.stat().st_size
            self._on_device_log("ok", f"已下载官方固件 {safe}（{size} 字节），保存于 {outdir}")
            return {"name": d.get("name"), "version": d.get("version"), "path": str(dst),
                    "size": size, "url": d.get("firmware"), "crc": d.get("crc")}
        return self._guard(work)
    def card_diff(self, base_id: str = "", target_id: str = "") -> dict:
        """卡数据对比（对齐小程序 card-diff 的滚动码分析）。

        比较卡夹里两张卡（或两次 dump）的每个数据块，列出差异块与差异字节数，
        用来判断是否「滚动码卡」（同一张卡两次读取只有个别字节在变）。
        """
        def work():
            a = self.cardbox.get(base_id)
            b = self.cardbox.get(target_id)
            if not a or not b:
                cards = self.cardbox.list()
                if len(cards) < 2:
                    raise NfcError("至少需要卡夹里有 2 张卡才能对比")
                a, b = cards[0], cards[1]
            ba = a.get("blocks") or {}
            bb = b.get("blocks") or {}
            diffs = []
            all_blocks = sorted(set(list(ba.keys()) + list(bb.keys())), key=lambda x: int(x))
            same = 0
            for k in all_blocks:
                va = str(ba.get(str(k), "") or "").replace(" ", "").upper()
                vb = str(bb.get(str(k), "") or "").replace(" ", "").upper()
                if va == vb:
                    same += 1
                    continue
                pos = [i for i in range(min(len(va), len(vb))) if va[i] != vb[i]]
                diffs.append({"block": int(k), "a": va, "b": vb,
                              "diff_bytes": len(set(i // 2 for i in pos)),
                              "diff_hex": sorted({va[i:i+2] + "→" + vb[i:i+2]
                                                  for i in pos if i % 2 == 0})[:8]})
            total = len(all_blocks)
            changing = len(diffs)
            # 滚动码判定：只有很少几个块在变（通常 0-2 块），其余完全一致
            is_rolling = bool(diffs) and changing <= 2 and total >= 8
            return {
                "base": {"id": a.get("id"), "name": a.get("name"), "uid": a.get("uid")},
                "target": {"id": b.get("id"), "name": b.get("name"), "uid": b.get("uid")},
                "total_blocks": total, "same_blocks": same, "diff_blocks": changing,
                "diffs": diffs, "is_rolling_code": is_rolling,
                "verdict": ("疑似滚动码卡：两次读取仅少量块变化，直接克隆可能导致门禁失效"
                            if is_rolling else
                            "非典型滚动码卡（差异块较多或无差异）"),
            }
        return self._guard(work)
    # ------------------------------------------------------------ 密码库（公共 / 用户 / 读卡历史）
    def rescue_card(self) -> dict:
        """卡片救援：尾块被写坏的卡，用密钥库逐个扇区认证，认上的把尾块恢复成出厂值。

        恢复后的尾块 = KEY A FFFFFFFFFFFF + 权限位 FF0780 + KEY B FFFFFFFFFFFF。
        认证不上的扇区说明密钥已经被改掉，MIFARE 层面无法再救（硬件永久锁）。
        """
        def work():
            keys = []
            try:
                for lib in ("public", "user", "history"):
                    for row in self.keybox.list(lib):
                        k = (row or {}).get("key") if isinstance(row, dict) else str(row)
                        if k:
                            k = str(k).replace(" ", "").upper()
                            if len(k) == 12 and k not in keys:
                                keys.append(k)
            except Exception:                                    # noqa: BLE001
                pass
            for extra in ("FFFFFFFFFFFF", str(self.settings.data.get("default_key") or "").upper()):
                if len(extra) == 12 and extra not in keys:
                    keys.insert(0, extra)
            trailer = hex_to_bytes("FFFFFFFFFFFFFF0780FFFFFFFFFFFF")
            fixed, locked = [], []
            for sector in range(16):
                blk = sector * 4 + 3
                done = False
                for k in keys:
                    for kt in (0, 1):
                        try:
                            if not self.device.auth(blk, hex_to_bytes(k), int(kt)):
                                continue
                        except Exception:                        # noqa: BLE001
                            continue
                        try:
                            self.device.write_block(blk, trailer, hex_to_bytes(k), int(kt),
                                                    verify=False)
                            fixed.append({"sector": sector, "key": k, "key_type": int(kt)})
                            done = True
                            break
                        except Exception:                        # noqa: BLE001
                            continue
                    if done:
                        break
                if not done:
                    locked.append(sector)
            detail = (f"试了 {len(keys)} 把密钥：修复 {len(fixed)} 个扇区尾块，"
                      f"{len(locked)} 个扇区认证不上")
            self._on_device_log("ok" if fixed else "warn", "卡片救援完成：" + detail)
            return {"ok": True, "fixed": fixed, "locked": locked,
                    "tried": len(keys), "detail": detail}
        return self._guard(work)

    def keybox_list(self, lib: str = "public") -> dict:
        return self._ok({"lib": lib, "items": self.keybox.list(lib),
                         "stats": self.keybox.stats()})

    def keybox_add(self, keys: Any, lib: str = "user") -> dict:
        return self._guard(lambda: dict(self.keybox.add(keys, lib),
                                        stats=self.keybox.stats()))

    def keybox_remove(self, key_id: str = "", key: str = "", lib: str = "user") -> dict:
        return self._ok({"removed": self.keybox.remove(key_id, key, lib),
                         "stats": self.keybox.stats()})

    def keybox_clear(self, lib: str = "history") -> dict:
        return self._ok(dict(self.keybox.clear(lib), stats=self.keybox.stats()))
    def auto_crack_chain(self, sector: int = 0, key_hex: str = "", algorithm: str = "auto") -> dict:
        """自动破解链（对齐官方：优先自动识别，也可手动指定算法）。

        自动模式（auto）：寻卡拿 UID → 云端 keygen 返回候选密钥全表 → 设备批量验证（命中即停）。
        手动模式：可指定 network / history / staticnest / hardnested 等算法。
        """
        def work():
            if not self.device.connected:
                raise NfcError("请先连接读卡器")
            block = int(sector) * 4
            uid = (self._last_card or {}).get("uid", "")
            if not uid:
                card = self.device.find_card()
                uid = card.get("uid", "")
                self._last_card = {"uid": uid, "card_type": card.get("card_type", ""),
                                   "class": card.get("class", "")}
            if not uid:
                raise NfcError("未读到卡片 UID，请把卡放到感应区后重试")

            algo = (algorithm or "auto").strip().lower()

            # 全卡模式（sector < 0）：与官方一致地把 16 个扇区全部解出来
            if int(sector) < 0:
                self._on_device_log("info", "全卡破解：逐扇区验证候选密钥（16 个扇区）…")
                res = self._crack_all_sectors(
                    mode=self._crack_engine.mode(None),
                    timeout=float(self.settings.data.get("crack_timeout", 25) or 25),
                    uid=uid,
                    progress=lambda pct, msg: self._progress_push("crack", pct, 100, msg))
                res["source"] = res.get("method", "all_sectors")
                res["algorithm"] = algo
                return res

            # 手工指定密钥优先
            if key_hex:
                kb = hex_to_bytes(key_hex)[:6].ljust(6, b"\x00")
                for kt in (0, 1):
                    try:
                        self.device.auth(block, kb, kt)
                        self._cracked_keys[int(sector)] = kb.hex().upper()
                        return {"ok": True, "key": kb.hex().upper(), "key_type": kt,
                                "uid": uid, "source": "manual"}
                    except NfcError:
                        continue
                raise NfcError("手工指定的密钥未通过认证")

            # ---- pang（全加密卡）：0x22 采集 → 云端 /recovery/pang（官方最多重试 5 次）----
            if algo == "pang":
                from .crack_engine import _vendor_request, vendor_urls, cloud_config, parse_vendor_response
                conf = cloud_config(self.settings.data)
                urls = vendor_urls(conf["endpoint"])
                last = ""
                for attempt in range(1, 6):
                    col = self.device.collect_full_decode(block=block, key_type=0)
                    if not col.get("ok"):
                        last = col.get("error") or "采集失败"
                        continue
                    body = {"uid": col.get("uid_i") or 0, "nt": col.get("nt") or 0,
                            "ks_list_1": [col.get("ks_list_0", 0), col.get("ks_list_1", 0)],
                            "nr1": col.get("nr_1") or 0,
                            "ks_list_2": [0, 0], "nr2": 0,
                            "ks_list_3": [col.get("ks_list_30", 0), col.get("ks_list_37", 0)],
                            "nr3": col.get("nr_3") or 0}
                    payload = _vendor_request(urls["base"] + "/recovery/pang",
                                              {"Content-Type": "application/json",
                                               "Accept": "application/json",
                                               AUTH_HEADER: conf["token"], "User-Agent": VENDOR_UA},
                                              body, max(20.0, conf["timeout"]))
                    res = parse_vendor_response(payload)
                    keys = res.get("keys") or []
                    if keys:
                        for k in keys:
                            try:
                                kb = hex_to_bytes(k)[:6].ljust(6, b"\x00")
                                self.device.auth(block, kb, 0)
                                self._cracked_keys[int(sector)] = k.upper()
                                return {"ok": True, "key": k.upper(), "key_type": 0, "uid": uid,
                                        "source": "pang", "algorithm": "pang", "attempts": attempt}
                            except NfcError:
                                continue
                    last = res.get("detail") or "云端未返回可用密钥"
                raise NfcError(f"pang 全加密采集/解密未成功（重试 5 次）：{last}")

            # 未实现（暂缺采集细节）
            if algo in ("mfoc", "fm11rf08s"):
                raise NfcError(f"{algo} 的采集映射尚未完全接好（已知：mfoc 用 0x30、"
                               f"fm11rf08s 用 0x37）；普通 M1 卡请用自动模式")

            # ---- 官方 history 算法：先查本地读卡历史库，命中直接复用（免云端） ----
            try:
                for row in self.keybox.list("history"):
                    if str(row.get("uid") or "").upper() != str(uid).upper():
                        continue
                    for k in (row.get("keys") or []):
                        kb = hex_to_bytes(k)[:6].ljust(6, b"\x00")
                        for kt in (0, 1):
                            try:
                                self.device.auth(block, kb, kt)
                                self._cracked_keys[int(sector)] = k.upper()
                                self._on_device_log("ok", f"历史库命中：扇区 {sector} 密钥 {k.upper()}"
                                                          f"（无需云端）")
                                return {"ok": True, "key": k.upper(), "key_type": kt, "uid": uid,
                                        "source": "history", "algorithm": algo}
                            except NfcError:
                                continue
            except Exception:                              # noqa: BLE001
                pass

            # 自动 / network / history：云端 keygen 全表 → 设备批量验证（命中即停）
            self._on_device_log("info", f"云端解密：查询 keygen（UID {uid}）…")
            kg = cloud_keygen_lookup(uid, timeout=25.0)
            cand = list(kg.get("keys") or [])
            if not cand:
                raise NfcError("云端密钥库未收录该卡（无候选密钥），请改用 staticnest / hardnested")
            self._on_device_log("info", f"云端返回 {len(cand)} 把候选密钥，开始批量验证…")
            self._progress_push("crack", 0, len(cand), f"云端候选 {len(cand)} 把，验证中…")
            for kt in (0, 1):
                res = self.device.batch_verify(cand, block=block, key_type=kt)
                if res.get("hits"):
                    hit = res["hits"][0]
                    self._cracked_keys[int(sector)] = hit["key"]
                    self.keybox.record_history(uid, [hit["key"]], note="keygen 云端密钥库命中")
                    self._progress_push("crack", int(res.get("tried") or 0), len(cand),
                                        f"命中 {hit['key']}")
                    self._on_device_log("ok", f"云端解密成功：扇区 {sector} 密钥 {hit['key']}"
                                              f"（{len(cand)} 把中第 {res.get('tried')} 把命中）")
                    return {"ok": True, "key": hit["key"], "key_type": kt, "uid": uid,
                            "candidates": len(cand), "tried": res.get("tried"),
                            "source": "keygen", "algorithm": algo}
            raise NfcError(f"云端 {len(cand)} 把候选密钥全部未通过认证；"
                           f"该卡可能未收录或密钥不在库中")
        return self._guard(work)
    def keybox_batch_verify(self, keys: Any = None, lib: str = "public",
                            block: int = 0, key_type: int = 0) -> dict:
        """批量验证密钥（对齐小程序 0x40 密钥缓存 + 0x41 批量验证的功能语义）。

        keys 为空时取 lib 指定密码库的全部密钥；逐个用 0x0A 在设备端认证，
        命中的就是该扇区的可用密钥，并写入读卡历史库。
        """
        def work():
            if keys:
                raw = str(keys).replace(",", " ").replace(";", " ")
                key_list = [x for x in raw.split() if x]
            else:
                key_list = [str(r.get("key") or "") for r in self.keybox.list(lib)]
            key_list = [k for k in key_list if k]
            if not key_list:
                raise NfcError("密码库为空，没有可验证的密钥")
            self._on_device_log("info", f"批量验证：{len(key_list)} 把密钥，扇区 {block}，"
                                       f"{'KEY A' if int(key_type) == 0 else 'KEY B'}")
            res = self.device.batch_verify(key_list, block=block, key_type=key_type)
            if res.get("hits"):
                best = res["hits"][0]["key"]
                # 字典键语义是「扇区号」，这里原先误用了传入的块号，导致
                # dump 取用密钥时错位（扇区 1 的密钥被记到键 4）。
                self._cracked_keys[int(block) // 4] = best
                uid = (self._last_card or {}).get("uid", "")
                self.keybox.record_history(uid, [h["key"] for h in res["hits"]],
                                           card_type=(self._last_card or {}).get("card_type", ""),
                                           note="批量验证命中")
                self._on_device_log("ok", f"批量验证命中 {len(res['hits'])} 把，"
                                          f"扇区 {int(block) // 4}（块 {block}）密钥 {best}")
            else:
                self._on_device_log("warn", f"批量验证未命中（共试 {res.get('tried', 0)} 把）")
            return dict(res, stats=self.keybox.stats())
        return self._guard(work)
    # ------------------------------------------------------------ 设置 / 外观
    def get_settings(self) -> dict:
        return self._ok(self.settings.data)

    def save_settings(self, patch: Dict[str, Any]) -> dict:
        data = self.settings.update(patch or {})
        # 设置变更后立即同步给破解引擎（切换破解模式/token 无需重启）
        self._crack_engine.bind(self.settings.data)
        return self._ok(data)
    def wx_fetch_token(self, save: bool = True) -> dict:
        """从正在运行的微信小程序进程内存里自动提取登录 token（NICETHEME）。

        需要：微信已登录 + 「宜新 NFC Reader Tool」小程序正在运行。
        只读扫描 WeChatAppEx.exe 内存里的 UserLog，不注入、不修改目标进程。
        """
        from .wx_token import fetch_token

        def work():
            res = fetch_token()
            if not res.get("ok"):
                raise NfcError(str(res.get("error") or "未能从微信小程序获取 token"))
            if res.get("expired"):
                raise NfcError("抓到的 token 已过期，请在微信里重新打开一次该小程序再试")
            hours = max(0, int(res.get("expires_in") or 0)) // 3600
            if save:
                data = self.settings.update({"cloud_token": res["token"], "crack_mode": "cloud"})
                self._crack_engine.bind(self.settings.data)
                endpoint = data.get("cloud_endpoint")
            else:
                endpoint = self.settings.data.get("cloud_endpoint")
            self._on_device_log(
                "ok", f"已从微信小程序取得云端 token（UID {res.get('uid') or '?'}，"
                      f"约剩 {hours} 小时，已自动填入）")
            return {
                "uid": res.get("uid"),
                "expires_in": res.get("expires_in"),
                "hours": hours,
                "endpoint": endpoint,
                "token_tail": str(res.get("token", ""))[-8:],
                "saved": bool(save),
            }
        return self._guard(work)

    def cloud_token_check(self) -> dict:
        """校验云端 token 是否还可用：先本地看 JWT 过期时间，再向网关发一次轻量探测。

        返回 {"has_token", "valid", "expired", "reachable", "reason", "expires_in", ...}。
        注意区分两种情况：网络不通（reachable=False，不能据此判定 token 失效）
        与网关明确拒绝（valid=False，需要重新获取）。
        """
        from .crack_engine import vendor_token_probe
        from .wx_token import jwt_exp

        def work():
            conf = cloud_config(self.settings.data)
            token = str(conf.get("token") or "")
            base = {"endpoint": conf["endpoint"], "has_token": bool(token)}
            if not token:
                return dict(base, valid=False, expired=False, reachable=None,
                            expires_in=0, reason="尚未配置云端 token")
            now = time.time()
            exp = jwt_exp(token)
            left = int(exp - now) if exp else 0
            if exp and exp <= now:
                return dict(base, valid=False, expired=True, reachable=None,
                            expires_in=left,
                            reason=f"登录态 token 已过期（本地校验，过期 {-left // 60} 分钟）")
            probe = vendor_token_probe(token, conf["endpoint"],
                                       timeout=min(10.0, float(conf["timeout"]) + 3))
            if not probe.get("reachable"):
                return dict(base, valid=True, expired=False, reachable=False,
                            expires_in=left, detail=probe.get("detail", ""),
                            reason="网络不可达，无法确认 token（不视为失效）")
            if not probe.get("authorized"):
                return dict(base, valid=False, expired=False, reachable=True,
                            expires_in=left, detail=probe.get("detail", ""),
                            reason="云端网关拒绝了该 token，需要重新获取")
            return dict(base, valid=True, expired=False, reachable=True,
                        expires_in=left, detail=probe.get("detail", ""),
                        reason=(f"token 可用，剩余约 {left // 3600} 小时" if left > 0
                                else "token 可用"))
        return self._guard(work)

    def wx_open_miniprogram(self) -> dict:
        """打开微信并切到前台，把「怎么打开小程序」的教程一起返回给界面。

        为什么不自动点：微信 4.x 是自绘界面（UIA 只暴露 2 个节点），
        自动打开只能靠图像识别+模拟点击，维护成本高、还容易被窗口位置/DPI 搞坏。
        现在的做法是"程序打开微信 + 给出步骤引导 + 后台自动轮询抓取 token"，
        用户只需按提示点几下，抓到后自动填入。
        """
        from .wx_token import launch_wechat, focus_wechat, miniprogram_open

        def work():
            steps = []
            lw = launch_wechat()
            steps.append({"step": "launch_wechat", **lw})
            focused = focus_wechat()
            steps.append({"step": "focus_wechat", "ok": focused})
            opened = miniprogram_open()
            if opened:
                self._on_device_log("ok", "检测到小程序窗口已打开，可以直接抓取 token")
            else:
                self._on_device_log(
                    "info", "微信已就绪。请按教程在微信里打开「宜新 NFC Reader Tool」小程序，"
                            "点完「开始抓取」即可自动填入 token")
            return {"ok": bool(lw.get("ok")), "wechat_focused": focused,
                    "miniprogram_open": opened, "steps": steps,
                    "mini_steps": self._mini_steps()}
        return self._guard(work)

    @staticmethod
    def _mini_steps() -> list:
        """在微信里打开「宜新 NFC Reader Tool」的步骤（界面照这个引导用户）。"""
        return [
            {"n": 1, "title": "点开微信",
             "desc": "在任务栏/桌面点开微信；如果出现的是登录页（显示你的头像），先点绿色的「进入微信」。"},
            {"n": 2, "title": "点左侧的「小程序」图标",
             "desc": "微信左侧竖排图标里那个像罗盘的圆形图标（平时是白色，点下去会变绿）。"},
            {"n": 3, "title": "点绿色的「小程序」",
             "desc": "中间「发现」列表里会出现一行绿色的「小程序」，点它。"},
            {"n": 4, "title": "点红色的 NFC 图标",
             "desc": "右侧小程序列表里那个红色圆形、带信号波纹的图标，就是「宜新 NFC Reader Tool」。"},
            {"n": 5, "title": "开始抓取",
             "desc": "小程序打开后回到本窗口点「开始抓取 token」，程序会自动把登录态填好。"},
        ]

    def wx_miniprogram_guide(self) -> dict:
        """返回"在微信里打开小程序"的步骤教程（界面与文档共用）。"""
        from .wx_token import miniprogram_open

        return self._ok({"steps": self._mini_steps(),
                         "miniprogram_open": miniprogram_open(),
                         "appid": "wxc3dd33d9d4ae0fd6",
                         "name": "宜新 NFC Reader Tool"})

    def wx_auto_token(self, wait_seconds: float = 60.0, launch: bool = True) -> dict:
        """抓取并续期云端 token：把微信切到前台 → 轮询抓取 → 自动保存。

        小程序本身请按教程手动打开（微信 4.x 是自绘界面，自动点击不可靠、已放弃）。
        wait_seconds 内没抓到"剩余有效期够用"的 token 就报错，不会写入半成品。
        """
        from .wx_token import launch_wechat, focus_wechat, wait_for_token

        def work():
            steps = []
            if launch:
                lw = launch_wechat()
                steps.append({"step": "launch_wechat", **lw})
                steps.append({"step": "focus_wechat", "ok": focus_wechat()})
                self._on_device_log(
                    "info", "微信已就绪，正在等待「宜新 NFC Reader Tool」小程序的登录态…"
                            "（请按教程在小程序里点开一次；抓到后会自动填入）")
            res = wait_for_token(deadline_seconds=float(wait_seconds))
            if not res.get("ok"):
                raise NfcError(str(res.get("error") or "未能从微信小程序获取 token"))
            data = self.settings.update({"cloud_token": res["token"],
                                         "crack_mode": "cloud"})
            self._crack_engine.bind(self.settings.data)
            hours = max(0, int(res.get("expires_in") or 0)) // 3600
            self._on_device_log(
                "ok", f"已自动获取并填入云端 token（UID {res.get('uid') or '?'}，"
                      f"剩余约 {hours} 小时）")
            return {"uid": res.get("uid"), "expires_in": res.get("expires_in"),
                    "hours": hours, "token_tail": str(res.get("token", ""))[-8:],
                    "endpoint": data.get("cloud_endpoint"), "steps": steps,
                    "wait_seconds": float(wait_seconds)}
        return self._guard(work)

    def wx_token_status(self) -> dict:
        """探测微信小程序是否在运行、能否抓到 token（不写入设置）。"""
        from .wx_token import fetch_token

        def work():
            res = fetch_token(timeout_seconds=12.0)
            return {
                "ok": bool(res.get("ok")),
                "uid": res.get("uid"),
                "expires_in": res.get("expires_in"),
                "expired": res.get("expired"),
                "error": res.get("error"),
            }
        return self._ok(work())

    def list_wallpapers(self) -> dict:
        items = []
        try:
            files = sorted(wallpapers_dir().iterdir())
        except Exception:
            files = []
        for f in files:
            if f.suffix.lower() in IMG_SUFFIX:
                items.append({"name": f.name, "url": f"wallpapers/{quote(f.name)}"})
        return self._ok(items)

    def pick_wallpaper(self) -> dict:
        def work():
            src = self._ask_open([("图片", "*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif")])
            if not src:
                return {"canceled": True}
            p = Path(src)
            if p.suffix.lower() not in IMG_SUFFIX:
                raise NfcError("不支持的图片格式")
            dst = wallpapers_dir() / f"wall_{int(time.time())}{p.suffix.lower()}"
            shutil.copy2(p, dst)
            sync_wallpapers()          # 立刻同步到前端目录，否则界面取不到图
            self.settings.update({"wallpaper": dst.name})
            return {"name": dst.name, "url": f"wallpapers/{quote(dst.name)}"}
        return self._guard(work)

    def clear_wallpaper(self) -> dict:
        return self._ok(self.settings.update({"wallpaper": ""}))

    def device_assets(self) -> dict:
        """设备页用到的素材（已由 paths.sync_device_assets 同步到 web/assets）。"""
        base = web_assets_dir()
        items = []
        try:
            for f in sorted(base.iterdir()):
                if f.suffix.lower() in IMG_SUFFIX:
                    items.append({"name": f.name, "url": f"assets/{quote(f.name)}"})
        except Exception:
            items = []
        return self._ok(items)

    # ------------------------------------------------------------ 剪贴板
    def clipboard_write(self, text: str) -> dict:
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(text or "")
            r.update()
            r.destroy()
            return self._ok({"written": len(text or "")})
        except Exception as exc:                       # noqa: BLE001
            return self._err(exc)

    def clipboard_read(self) -> dict:
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            text = r.clipboard_get()
            r.destroy()
            return self._ok({"text": text})
        except Exception as exc:                       # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------ 窗口控制
    def win_minimize(self) -> dict:
        if self._window:
            self._window.minimize()
        return self._ok()

    def win_toggle_max(self) -> dict:
        if not self._window:
            return self._ok({"maximized": False})
        is_max = bool(getattr(self._window, "maximized", False))
        try:
            if is_max:
                self._window.restore()
            else:
                self._window.maximize()
        except Exception:
            pass
        return self._ok({"maximized": not is_max})

    def win_close(self) -> dict:
        if self._window:
            self._window.destroy()
        return self._ok()

    def win_set_opacity(self, value: float) -> dict:
        return self._ok({"value": value})

    # ------------------------------------------------------------ 文件对话框
    @staticmethod
    def _norm_filetypes(filetypes):
        """把各种写法统一成 pywebview 6.x 要求的「字符串序列」形式。

        pywebview 6.x 的 create_file_dialog 只接受字符串（或字符串序列）作为
        file_types，传入 (描述, 扩展名) 元组会直接抛 TypeError —— 表现就是
        「换背景图点了没反应」。这里统一归一化，兼容全部历史调用方：

            "图片 (*.jpg;*.png)"      -> ("图片 (*.jpg;*.png)",)
            [("图片", "*.jpg;*.png")] -> ("图片 (*.jpg;*.png)",)
            ("图片", "*.jpg;*.png")   -> ("图片 (*.jpg;*.png)",)
            ("JSON 文件 (*.json)",)   -> ("JSON 文件 (*.json)",)
        """
        if not filetypes:
            return None
        if isinstance(filetypes, str):
            return (filetypes,)

        out, loose = [], []
        for item in (filetypes if isinstance(filetypes, (list, tuple)) else [filetypes]):
            if isinstance(item, str):
                loose.append(item)
            elif isinstance(item, (list, tuple)):
                desc = str(item[0]).strip() if len(item) > 0 else ""
                ext = str(item[1]).strip() if len(item) > 1 else ""
                text = f"{desc} ({ext})" if desc and ext else (desc or ext)
                if text:
                    out.append(text)
            elif item:
                out.append(str(item))

        # 形如 ("图片", "*.jpg") 的「裸描述 + 扩展名」对
        if len(loose) == 2 and loose[1].startswith("*"):
            out.append(f"{loose[0]} ({loose[1]})")
        else:
            out.extend(loose)

        return tuple(out) or None

    def _ask_open(self, filetypes=None) -> Optional[str]:
        import webview
        kwargs = {"allow_multiple": False}
        fts = self._norm_filetypes(filetypes)
        if fts:
            kwargs["file_types"] = fts
        result = self._window.create_file_dialog(webview.OPEN_DIALOG, **kwargs)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def _ask_save(self, default_name: str) -> Optional[str]:
        import webview
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=default_name,
            file_types=self._norm_filetypes("JSON 文件 (*.json)") or ())
        if not result:
            return None
        return result if isinstance(result, str) else result[0]
