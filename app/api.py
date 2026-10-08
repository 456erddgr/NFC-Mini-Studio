# -*- coding: utf-8 -*-
"""暴露给前端 HTML 的 JS API（pywebview js_api）。

v2.0.0 变更：
  * 新增 USB（串口）连接通道：scan_ports / probe_serial / connect_usb；
  * 寻卡自动识卡（IC / ID / UID / 7 字节 UID）并自动在**后台**发起破解级联；
  * 读卡、写卡、复制、粘贴、破解全部推送带百分比的实时进度事件；
  * 写卡走后端自适应帧格式 + 回读校验，并把实际命中的格式回报给界面。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from .crack_engine import (ENGINE_LABELS, MODE_CHOICES, MODE_CLOUD, MODE_DEVICE,
                           MODE_LOCAL, CrackEngine, parse_frames)
from .device import CRACK_CHAIN, CRACK_LABELS, NfcDevice
from .paths import (APP_TITLE, APP_VERSION, sync_wallpapers, wallpapers_dir,
                    web_assets_dir, crack_cache_file)
from .protocol import (DEFAULT_KEY, ERR, PHONE_WRITE_MODES, NfcError, WRITE_FORMATS,
                       hex_to_bytes, phone_nfc_plan, resolve_write_mode,
                       write_mode_table)
from .store import CardBox, Settings
from .transport import WIRED_CANDIDATES

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
            "baud_choices": [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600],
            "wired_candidates": [{"vid": v, "pid": p, "label": l}
                                 for v, p, l in WIRED_CANDIDATES],
            "wired_ep_size": 64,
        })

    # ------------------------------------------------------------ 设备：连接
    def scan(self, timeout: float = 6.0) -> dict:
        return self._guard(lambda: self.device.scan(float(timeout)))

    def scan_ports(self) -> dict:
        return self._guard(self.device.list_ports)

    def probe_serial(self, port: str, baud: int = 115200) -> dict:
        return self._guard(lambda: self.device.probe_serial(str(port), int(baud)))

    def connect(self, mac: str) -> dict:
        def work():
            info = self.device.connect(mac)
            self.settings.update({"last_mac": mac, "connection_mode": "ble"})
            return info
        return self._guard(work)

    def connect_usb(self, port: str, baud: int = 115200) -> dict:
        def work():
            info = self.device.connect_serial(str(port), int(baud))
            self.settings.update({"last_port": str(port), "baud": int(baud),
                                  "connection_mode": "usb"})
            return info
        return self._guard(work)

    # ---- USB 有线（libusb 厂商自定义类 bulk 通道，v2.1 新增）----
    def wired_devices(self) -> dict:
        """枚举可用的 USB 有线读卡器（与串口枚举并列，互不影响）。"""
        return self._guard(self.device.list_wired)

    def probe_wired(self, vid: int = 0, pid: int = 0, serial: str = "") -> dict:
        return self._guard(lambda: self.device.probe_wired(int(vid or 0), int(pid or 0),
                                                           str(serial or "")))

    def connect_wired(self, vid: int = 0, pid: int = 0, serial: str = "") -> dict:
        def work():
            info = self.device.connect_wired(int(vid or 0), int(pid or 0), str(serial or ""))
            self.settings.update({"connection_mode": "wired", "wired_vid": int(vid or 0),
                                  "wired_pid": int(pid or 0), "wired_serial": str(serial or "")})
            return info
        return self._guard(work)

    def wired_send_frame(self, frame_hex: str, wait: float = 1.5) -> dict:
        return self._guard(lambda: self.device.wired_send_raw(str(frame_hex), float(wait)))

    def wired_log(self, limit: int = 120, clear: bool = False) -> dict:
        def work():
            items = self.device.wired_raw_log(int(limit), bool(clear))
            snap = self.device.snapshot().get("wired", {})
            return {"items": items, "state": snap}
        return self._guard(work)

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

    def _maybe_auto_crack(self, info: dict) -> str:
        """IC 类卡片在后台级联破解；ID 卡没有"密钥"概念，直接跳过。"""
        if not info.get("crackable"):
            self._on_device_log("info", "该卡不是可破解的 IC（Mifare）类型，跳过密钥破解")
            return ""
        if not self.device.connected:
            return ""
        try:
            self._crack_job = self._start_crack_job(
                sector=int(self.settings.data.get("crack_sector", 0)))
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
        def work():
            if key_hex:
                key = key_hex
            else:
                key, _kt, _s = self._best_key()
            started = time.time()
            res = self.device.write_block(int(block), hex_to_bytes(data_hex),
                                          hex_to_bytes(key) if isinstance(key, str) else key,
                                          int(key_type), verify=bool(verify))
            res["elapsed"] = round(time.time() - started, 2)
            self._on_device_log(
                "ok", f"第 {block} 块写入成功（帧格式 {res['format']}，"
                      f"回读{'一致' if res.get('verified') else '未校验'}，"
                      f"耗时 {res['elapsed']}s）")
            return res
        return self._guard(work)

    def write_block_detail(self, block: int, data_hex: str, key_hex: str = "",
                           key_type: int = 0, verify: bool = True) -> dict:
        """写单块并回传写帧 / 读帧原文，方便对着日志定位固件协议差异。"""
        def work():
            if key_hex:
                key = key_hex
            else:
                key, _kt, _s = self._best_key()
            res = self.device.write_block_verbose(
                int(block), hex_to_bytes(data_hex),
                hex_to_bytes(key) if isinstance(key, str) else key,
                int(key_type), verify=bool(verify))
            _bytes = res["write"]
            self._on_device_log("ok", f"第 {block} 块写块诊断：格式 {_bytes['format']}、"
                                      f"回读 {_bytes.get('read_back', '—')}")
            return res
        return self._guard(work)

    def dump_card(self, include_trailers: bool = True, key_hex: str = "",
                  key_type: int = -1, job: str = "dump") -> dict:
        def progress(done: int, total: int, block: int) -> None:
            self._progress_push(job, done, total, f"读取第 {block} 块", block=block)

        def work():
            if key_hex:
                key = hex_to_bytes(key_hex)
            else:
                best, _kt, _s = self._best_key()
                key = hex_to_bytes(best) if isinstance(best, str) else best
            kt = int(key_type) if int(key_type) >= 0 else \
                int(self.settings.data.get("default_key_type", 0))
            started = time.time()
            res = self.device.dump_card(key, kt, bool(include_trailers),
                                        sector_keys=dict(self._cracked_keys),
                                        progress=progress)
            res["elapsed"] = round(time.time() - started, 2)
            self._on_device_log(
                "ok", f"整卡读取完成：{res['ok_count']}/{res['total']} 块，"
                      f"失败 {len(res['errors'])} 块，耗时 {res['elapsed']}s")
            return res
        return self._guard(work)

    # ------------------------------------------------------------ 破解
    def crack_methods(self) -> dict:
        return self._ok({"methods": CRACK_METHODS, "chain": list(CRACK_CHAIN),
                         "engine": self._crack_engine.status()})

    def crack_engine_status(self) -> dict:
        """破解引擎状态（v2.2.0）：当前模式 / 执行链 / 本地算法可用性 / 云端配置。"""
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

            # 1) 云端解算 → 本地离线 → 自动降级（crack_engine 内部完成）
            started = time.time()
            res = self._crack_engine.solve(
                mode=mode, frames=frames, sector=sector,
                progress=lambda pct, msg: progress(int(pct), msg),
                should_stop=cancelled,
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
            self._on_device_log(
                "ok", f"读 ID 卡：{info['id_hex']} · 频率 {info['freq']} · "
                      f"{info['card_type']} · 卡代码 {info['card_code']} · "
                      f"设施码 {info['facility_code']}")
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
            if key_hex:
                key = hex_to_bytes(key_hex)
            else:
                best, _kt, _s = self._best_key()
                key = hex_to_bytes(best) if isinstance(best, str) else best
            res = self.device.write_block_mode(
                int(block), hex_to_bytes(data_hex), mode_key, key, int(key_type),
                verify=bool(verify))
            self._on_device_log(
                "ok", f"{conf['label']} 完成（{res['cmd']} · 第 {block} 块）："
                      f"{res.get('verify_text') or '未校验'}，耗时 {res['elapsed']}s")
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
            if key_hex:
                key = hex_to_bytes(key_hex)
            else:
                best, _kt, _s = self._best_key()
                key = hex_to_bytes(best) if isinstance(best, str) else best
            kt = int(key_type) if int(key_type) >= 0 else \
                int(self.settings.data.get("default_key_type", 0))
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
            if key_hex:
                key = hex_to_bytes(key_hex)
            else:
                best, _kt, _s = self._best_key()
                key = hex_to_bytes(best) if isinstance(best, str) else best
            kt = int(key_type) if int(key_type) >= 0 else \
                int(self.settings.data.get("default_key_type", 0))
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

            best, kt, _sector = self._best_key()

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
            if key_hex:
                key = hex_to_bytes(key_hex)
            else:
                best, _kt, _s = self._best_key()
                key = hex_to_bytes(best) if isinstance(best, str) else best
            kt = int(self.settings.data.get("default_key_type", 0))

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

    # ------------------------------------------------------------ 设置 / 外观
    def get_settings(self) -> dict:
        return self._ok(self.settings.data)

    def save_settings(self, patch: Dict[str, Any]) -> dict:
        data = self.settings.update(patch or {})
        # 设置变更后立即同步给破解引擎（切换破解模式/token 无需重启）
        self._crack_engine.bind(self.settings.data)
        return self._ok(data)

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
