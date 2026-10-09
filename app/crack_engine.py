# -*- coding: utf-8 -*-
"""破解引擎（v2.2.0）：云端解算 + 本地离线 mfkey32 + 固件级联 自动降级。

设计要点
  * 默认走厂商云端解算接口（设置项 cloud_endpoint / cloud_token / cloud_timeout，
    也支持环境变量 NFCMINI_CLOUD_ENDPOINT / NFCMINI_CLOUD_TOKEN 覆盖）；
  * 设置里可切成本地离线解算（app/mfkey_local.py：proxmark3 mfkey32v2 的 numba 移植）；
  * 云端未配置 / 调用失败 / 超时 / 返回不合法 -> 自动降级到本地算法；
  * 本地算法对设备上报的 nr_enc / ar_enc 两种字节序逐一重试（端序不匹配自动换序）；
  * 云端与本地都拿不到结果时，交回 device.crack_chain 的固件级联兜底（老链路不变）。

mfkey32 的输入是同一张卡两轮认证的采集对：
    uid / nt0 / nr0_enc / ar0_enc / nt1 / nr1_enc / ar1_enc
既能手工粘成文本，也能由 transport 层把原始帧直接喂给 parse_frames()。
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .protocol import NfcError

M32 = 0xFFFFFFFF

MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
MODE_DEVICE = "device"
MODE_ORDER = (MODE_CLOUD, MODE_LOCAL, MODE_DEVICE)
ENGINE_LABELS: Dict[str, str] = {
    MODE_CLOUD: "云端解算",
    MODE_LOCAL: "本地 mfkey32",
    MODE_DEVICE: "固件级联",
}
MODE_CHOICES = [
    {"id": MODE_CLOUD, "name": "云端解算（官方 recovery 接口，默认）",
     "desc": "先读卡采集，再走厂商官方云端 recovery 接口破译；失败/超时自动降级本地 mfkey32"},
    {"id": MODE_LOCAL, "name": "本地离线解算（mfkey32）",
     "desc": "不联网，用本机 numba 加速的 mfkey32 直接恢复密钥"},
]

DEFAULT_BITS = 20        # 状态空间位数：20 = 与官方 mfkey32 一致，单帧数秒内出结果
DEFAULT_CLOUD_TIMEOUT = 8.0
CLOUD_PROTOCOL = "mfkey32"
ENV_ENDPOINT = "NFCMINI_CLOUD_ENDPOINT"
ENV_TOKEN = "NFCMINI_CLOUD_TOKEN"

# ---------------------------------------------------------------- 厂商云端真实协议（v2.2.0 修复）
# 依据「宜新 NFC Reader Tool」小程序逆向（app-service.js 模块 18）核实：
#   base   : https://nfctools.nfcreader.net/nice-json/mini-program/v1
#   端点   : POST /recovery/mfkey32v2
#   请求头 : content-type: application/json + NICETHEME: <user.token>
#   请求体 : {uid, nt1, nr1, ar1, nt2, nr2, ar2}（一次一帧，数值型十进制整数）
#   响应   : {status, message, keys | rawKeys}；status=200 成功，
#            400+"Card is compatible type" 归一化为 201（卡型不支持），
#            400+"Card is Gen3 type" 归一化为 202（Gen3 卡不支持）
# 鉴权凭据分两套，切勿混用（v2.2.0 联网实测结论）：
#   1) recovery 接口走请求头 NICETHEME: <user.token> —— 这是微信登录态 token，由 /login
#      每次登录重新签发、会过期，无法预置成固定值。故 DEFAULT_CLOUD_TOKEN 留空，由用户在
#      「破解模式」里粘贴自己的 token；未配置时云端链路立即给出中文原因并降级本地，
#      不再（像早期版本那样）拿 keygen 的固定 token 当 NICETHEME 发无效请求。
#   2) keygen 云端密钥库走请求头 Authorization: <固定 token> —— 小程序内置常量，可预置、
#      开箱即用，对应的就是 VENDOR_KEYGEN_TOKEN。
VENDOR_BASE = "https://nfctools.nfcreader.net/nice-json/mini-program/v1"
VENDOR_RECOVERY_PATH = "/recovery/mfkey32v2"
VENDOR_KEYGEN_BASE = "https://nfctoolsapp.nfcreader.net/keygen"
VENDOR_HOSTS = ("nfcreader.net",)
VENDOR_KEYGEN_TOKEN = "7a422ed2462aaabd3bda83cebe62f5602f83e8fca3cda089407db9aa5000c8d6"
VENDOR_TOKEN = VENDOR_KEYGEN_TOKEN   # 兼容旧引用：仅用于 keygen 的 Authorization
DEFAULT_CLOUD_ENDPOINT = VENDOR_BASE + VENDOR_RECOVERY_PATH
DEFAULT_CLOUD_TOKEN = ""             # 登录态 token 不可预置（原因见上文）
AUTH_HEADER = "NICETHEME"    # 厂商网关自定义鉴权头（小程序侧为 NICETHEME: <user.token>）
VENDOR_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroMessenger/8.0.0 miniProgram"
VENDOR_STATUS_OK = 200
VENDOR_STATUS_NO_CARD = 201
VENDOR_STATUS_GEN3 = 202
KEYGEN_POLL_INTERVAL = 1.2
KEYGEN_POLL_TIMEOUT = 15.0
CLOUD_FRAME_LIMIT = 4        # 单次云端解算最多提交的采集帧数（厂商接口一次一帧）
CLOUD_VERIFY_LIMIT = 0       # 单张卡拿回设备验证的候选密钥数上限；0 = 不限（全表逐把验证）

# 官方 mfkey32v2 README 测试向量：同一张卡 sector0 KEYA = A0A1A2A3A4A5 的多组采集
LOCAL_VECTORS: Tuple[Tuple[str, ...], ...] = (
    ("2a234f80", "240bd022", "ad2e1687", "57e6f7e4", "18a4bd3e", "accc1a23", "6f10e401"),
    ("2a234f80", "a5b5d569", "30e2ff62", "5e1b926d", "d2760a5c", "6157acb0", "cb0fd13d"),
    ("2a234f80", "81ee85ad", "3290942e", "f76b6ec7", "9c95e4ac", "d05dc608", "729dcbe9"),
    ("2a234f80", "b61bf034", "9c04ca8f", "787f79ce", "01e9294b", "22218ef9", "c4e54292"),
    ("2a234f80", "f793ebf3", "b783530a", "d1607737", "a7ed17fe", "50d0c65b", "4b43383c"),
)
VECTOR_EXPECT = 0xA0A1A2A3A4A5


class EngineError(NfcError):
    """引擎层错误（沿用 NfcError 的中文错误文案 / code 约定）。"""


class CloudTimeout(EngineError):
    """云端调用超时。"""


class CloudAuthError(EngineError):
    """厂商云端鉴权失败（token 无效 / 过期）——降级本地并提示用户更新 token。"""


class CloudUnsupported(EngineError):
    """厂商云端明确拒解（卡型不支持 mfkey32，如 201/202）——不重试云端，直接降级。"""


class CloudPending(EngineError):
    """官方 keygen 云端密钥库仍在计算（未完成）。"""


# ------------------------------------------------------------------ 工具
def swap32(value: int) -> int:
    """32bit 字节序翻转。"""
    v = int(value) & M32
    v = ((v >> 8) & 0x00FF00FF) | ((v & 0x00FF00FF) << 8)
    return (((v >> 16) | (v << 16)) & M32)


def _uid_int(uid: Any) -> int:
    """把 UID 文本按「十六进制」解析（本工程的 UID 一律是 0xA0A1… / A0A1… 形式）。

    不能直接用 u32()：它把纯数字串当十进制，而读卡回来的 UID 是十六进制文本，
    纯数字 UID（如 11223344）会被算成 0x00AB4130，导致云端密钥库查错卡。
    """
    if isinstance(uid, int) and not isinstance(uid, bool):
        return uid & M32
    text = str(uid).strip().replace(" ", "").replace(":", "").replace("-", "")
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) in (8, 14) and re.fullmatch(r"[0-9a-fA-F]+", text):
        return int(text, 16) & M32
    return u32(uid)


def u32(value: Any) -> int:
    """宽容地把 int / '0x..' / 'a0a1' 十进制串转成 32bit 无符号整数。"""
    if isinstance(value, bool):
        raise EngineError("非法数值：布尔值")
    if isinstance(value, int):
        return value & M32
    if isinstance(value, float):
        return int(value) & M32
    text = str(value).strip().replace(" ", "").replace(":", "").replace("-", "")
    if not text:
        raise EngineError("非法数值：空字符串")
    try:
        base = 16 if (text.lower().startswith("0x") or re.search(r"[a-fA-F]", text)) else 10
        return int(text, base) & M32
    except ValueError as exc:
        raise EngineError(f"非法数值：{value!r}") from exc


def fmt12(key: int) -> str:
    """48bit 密钥格式化（大写 12 位 hex）。"""
    return f"{int(key) & 0xFFFFFFFFFFFF:012X}"


def norm_key(value: Any) -> Optional[str]:
    """把云端返回的各种密钥写法（hex / 十进制 / 带分隔符 / 0x 前缀）统一成 12 位大写 hex。

    判定规则：带 0x 前缀或含 a-f 的一定按 hex；纯数字且恰好 12 位按密钥 hex 约定；
    其余纯数字按十进制；超过 48bit 取低 12 位 hex。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return fmt12(value)
    if isinstance(value, float):
        return fmt12(int(value))
    raw = str(value).strip()
    if not raw:
        return None
    prefixed = raw.lower().startswith("0x")
    if prefixed:
        raw = raw[2:]
    body = re.sub(r"[^0-9a-fA-F]", "", raw)
    if not body:
        return None
    if prefixed or re.search(r"[a-fA-F]", body) or len(body) == 12:
        return fmt12(int(body[:12], 16))
    try:
        number = int(body, 10)
    except ValueError:
        return None
    return fmt12(number if number <= 0xFFFFFFFFFFFF else int(f"{number:x}"[-12:], 16))


# ------------------------------------------------------------------ 采集帧
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "uid": ("uid", "uidhex", "carduid", "uid4", "uidbytes"),
    "nt0": ("nt0", "nt0hex", "nonce0", "nt00"),
    "nr0_enc": ("nr0enc", "nr0", "nr0encoded", "nr0e"),
    "ar0_enc": ("ar0enc", "ar0", "ar0encoded", "ar0e"),
    "nt1": ("nt1", "nt1hex", "nonce1", "nt01"),
    "nr1_enc": ("nr1enc", "nr1", "nr1encoded", "nr1e"),
    "ar1_enc": ("ar1enc", "ar1", "ar1encoded", "ar1e"),
}
_ORDER_FIELDS = ("uid", "nt0", "nr0_enc", "ar0_enc", "nt1", "nr1_enc", "ar1_enc")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]{1,8}|[0-9a-fA-F]{2,8}")


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _lookup(mapping: Dict[str, Any], field: str) -> Any:
    for alias in _ALIASES[field]:
        if alias in mapping:
            return mapping[alias]
    return None


@dataclass(frozen=True)
class NonceFrame:
    """一轮 mfkey32 采集对（同一张卡两段认证的 7 个 32bit 字段）。"""

    uid: int
    nt0: int
    nr0_enc: int
    ar0_enc: int
    nt1: int
    nr1_enc: int
    ar1_enc: int

    # -- 序列化 ------------------------------------------------------
    def as_dict(self) -> Dict[str, str]:
        return {name: f"{getattr(self, name) & M32:08X}" for name in _ORDER_FIELDS}

    def hex_line(self) -> str:
        return " ".join(f"{getattr(self, name) & M32:08x}" for name in _ORDER_FIELDS)

    def swap(self, do_nr: bool, do_ar: bool) -> "NonceFrame":
        if not (do_nr or do_ar):
            return self
        return NonceFrame(
            self.uid, self.nt0,
            swap32(self.nr0_enc) if do_nr else self.nr0_enc,
            swap32(self.ar0_enc) if do_ar else self.ar0_enc,
            self.nt1,
            swap32(self.nr1_enc) if do_nr else self.nr1_enc,
            swap32(self.ar1_enc) if do_ar else self.ar1_enc,
        )

    # -- 构造 --------------------------------------------------------
    @classmethod
    def from_mapping(cls, mapping: Dict[str, Any], fallback_uid: Optional[int] = None) -> "NonceFrame":
        norm = {_norm_name(k): v for k, v in mapping.items()}
        values: Dict[str, int] = {}
        missing: List[str] = []
        for field in _ORDER_FIELDS:
            raw = _lookup(norm, field)
            if raw in (None, ""):
                if field == "uid" and fallback_uid is not None:
                    values[field] = int(fallback_uid) & M32
                    continue
                missing.append(field)
                continue
            values[field] = u32(raw)
        if missing:
            raise EngineError("采集数据缺少字段：" + "、".join(missing))
        return cls(**values)

    @classmethod
    def from_sequence(cls, items: Sequence[Any]) -> "NonceFrame":
        if len(items) == 7:
            return cls(*[u32(v) for v in items])
        if len(items) == 6:
            # 6 字段写法：nt0 nr0 ar0 nt1 nr1 ar1（uid 由上一组继承，这里不强求）
            a, b, c, d, e, f = [u32(v) for v in items]
            return cls(0, a, b, c, d, e, f)
        raise EngineError(f"字段数量不对：需要 7 个（uid+两轮采集），收到 {len(items)} 个")


def order_variants(frame: NonceFrame) -> List[Tuple[str, NonceFrame]]:
    """返回需要尝试的 (端序说明, 帧) 列表，原序优先。

    设备 BLE 上报的 nr_enc / ar_enc 在不同固件版本里字节序可能相反（proxmark 侧按 32bit
    小端解释），端序不匹配时 mfkey32 会直接 0 候选，所以两种序都试。
    """
    plans = (("原序", False, False),
             ("nr_enc 反序", True, False),
             ("ar_enc 反序", False, True),
             ("nr_enc/ar_enc 双反序", True, True))
    out: List[Tuple[str, NonceFrame]] = []
    seen: set = set()
    for label, do_nr, do_ar in plans:
        item = frame.swap(do_nr, do_ar)
        if item in seen:
            continue
        seen.add(item)
        out.append((label, item))
    return out


# ------------------------------------------------------------ 采集数据解析
def parse_frames(payload: Any) -> List[NonceFrame]:
    """把各种形态的采集数据统一解析成 NonceFrame 列表。

    支持：dict / dict 列表（键名大小写与 _enc 后缀可省略）/ JSON 字符串 / 纯文本
    （`nt0=.. nr0_enc=..` 键值对，或每行 7 段 hex）。
    """
    if payload is None:
        return []
    if isinstance(payload, NonceFrame):
        return [payload]
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        if text[:1] in "[{":
            try:
                return parse_frames(json.loads(text))
            except ValueError:
                pass
        return _parse_text(text)
    if isinstance(payload, dict):
        for key in ("frames", "items", "data", "list", "nonces", "records"):
            if key in payload and isinstance(payload[key], (list, dict, str)):
                inner = parse_frames(payload[key])
                if inner:
                    return inner
        return [NonceFrame.from_mapping(payload)]
    if isinstance(payload, (list, tuple)):
        frames: List[NonceFrame] = []
        last_uid: Optional[int] = None
        for item in payload:
            if isinstance(item, dict):
                frame = NonceFrame.from_mapping(item, fallback_uid=last_uid)
            elif isinstance(item, (list, tuple)):
                frame = NonceFrame.from_sequence(item)
            elif isinstance(item, (int, str)) and not isinstance(item, bool):
                tokens = _split_tokens(str(item))
                if len(tokens) % 7 != 0 or not tokens:
                    raise EngineError("hex 序列长度必须是 7 的倍数（uid+两轮采集）")
                for i in range(0, len(tokens), 7):
                    frames.append(NonceFrame.from_sequence(tokens[i:i + 7]))
                last_uid = frames[-1].uid
                continue
            else:
                raise EngineError(f"不支持的采集数据类型：{type(item).__name__}")
            frames.append(frame)
            last_uid = frame.uid
        return frames
    raise EngineError(f"不支持的采集数据类型：{type(payload).__name__}")


def _strip_comment(line: str) -> str:
    for mark in ("#", "//"):
        idx = line.find(mark)
        if idx >= 0:
            line = line[:idx]
    return line.strip()


def _split_tokens(line: str) -> List[str]:
    return [t for t in re.split(r"[\s,;|]+", line.strip()) if t]


def _parse_text(text: str) -> List[NonceFrame]:
    frames: List[NonceFrame] = []
    pending: List[str] = []
    last_uid: Optional[int] = None

    def flush_hex(tokens: List[str]) -> None:
        nonlocal last_uid
        if not tokens:
            return
        if len(tokens) % 7:
            raise EngineError("hex 序列长度必须是 7 的倍数（uid+两轮采集）")
        for i in range(0, len(tokens), 7):
            chunk = tokens[i:i + 7]
            if last_uid is not None and len(chunk) == 7 and chunk[0] == "":
                chunk = [f"{last_uid:08x}"] + chunk[1:]
            frame = NonceFrame.from_sequence(chunk)
            frames.append(frame)
            last_uid = frame.uid

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        if "=" in line or ":" in line and not re.fullmatch(r"[0-9a-fA-F:\s]+", line):
            mapping: Dict[str, Any] = {}
            bare: List[str] = []
            for token in _split_tokens(line.replace("=", " = ")):
                if token == "=":
                    continue
                if "=" in token:
                    key, _, val = token.partition("=")
                    mapping[key] = val
                else:
                    bare.append(token)
            if bare:
                # `uid 2a234f80` 这类空格分隔的键值对
                for i in range(0, len(bare) - 1, 2):
                    if _norm_name(bare[i]) in _ALIASES or True:
                        mapping.setdefault(bare[i], bare[i + 1])
            if mapping:
                frame = NonceFrame.from_mapping(mapping, fallback_uid=last_uid)
                frames.append(frame)
                last_uid = frame.uid
                continue
        tokens = _HEX_RE.findall(line)
        if tokens:
            pending.extend(tokens)
    if pending:
        flush_hex(pending)
    if not frames:
        raise EngineError("没有解析到任何采集数据（需要 uid+nt0+nr0_enc+ar0_enc+nt1+nr1_enc+ar1_enc）")
    return frames


# ---------------------------------------------------------------- 本地算法
class LocalEngine:
    """本地离线 mfkey32（app/mfkey_local.py 的懒加载封装）。"""

    def __init__(self, bits: int = DEFAULT_BITS) -> None:
        self.bits = int(bits)
        self._mod: Any = None
        self._error = ""
        self._loaded = False
        self._lock = threading.RLock()
        self._buffers: Dict[int, Tuple[Any, Any, Any]] = {}

    # -- 可用性 ------------------------------------------------------
    def load(self) -> Any:
        with self._lock:
            if self._loaded:
                return self._mod
            self._loaded = True
            try:
                from . import mfkey_local as mod
                self._mod = mod
                self._error = ""
            except Exception as exc:                     # noqa: BLE001
                self._mod = None
                self._error = f"{type(exc).__name__}: {exc}"
            return self._mod

    @property
    def available(self) -> bool:
        return self.load() is not None

    @property
    def error(self) -> str:
        self.load()
        return self._error

    def _arrays(self) -> Tuple[Any, Any, Any]:
        mod = self.load()
        key = id(mod)
        if key not in self._buffers:
            np = mod.np
            self._buffers[key] = (
                np.empty((int(mod.OUT_CAP), 2), dtype=np.int64),
                np.zeros(1, dtype=np.int64),
                np.zeros(8, dtype=np.int64),
            )
        return self._buffers[key]

    # -- 单帧恢复 ----------------------------------------------------
    def recover(self, frame: NonceFrame,
                should_stop: Optional[Callable[[], bool]] = None,
                on_try: Optional[Callable[[str], None]] = None) -> dict:
        """对一帧采集数据做恢复，自动尝试两种字节序。"""
        mod = self.load()
        if mod is None:
            raise EngineError(f"本地算法不可用：{self._error or '模块导入失败'}")
        out, cnt, dbg = self._arrays()
        started = time.time()
        tried: List[dict] = []
        for label, item in order_variants(frame):
            if should_stop and should_stop():
                return {"ok": False, "key": "", "candidates": 0, "order": "",
                        "elapsed": round(time.time() - started, 2), "tried": tried,
                        "detail": "用户取消"}
            if on_try:
                try:
                    on_try(label)
                except Exception:                        # noqa: BLE001
                    pass
            t0 = time.time()
            dbg[:] = 0
            key, cand = mod.recover_key(item.uid, item.nt0, item.nr0_enc, item.ar0_enc,
                                        item.nt1, item.nr1_enc, item.ar1_enc,
                                        out, cnt, int(self.bits), dbg)
            cost = round(time.time() - t0, 2)
            hit = int(key) >= 0
            tried.append({"order": label, "ok": hit, "candidates": int(cand),
                          "elapsed": cost})
            if hit:
                return {"ok": True, "key": fmt12(int(key)), "key_int": int(key),
                        "candidates": int(cand), "order": label,
                        "elapsed": round(time.time() - started, 2), "tried": tried,
                        "detail": ""}
        return {"ok": False, "key": "", "candidates": 0, "order": "",
                "elapsed": round(time.time() - started, 2), "tried": tried,
                "detail": "本地算法未命中（候选数为 0 通常表示采集数据或端序不匹配）"}

    def recover_any(self, frames: Iterable[NonceFrame],
                    should_stop: Optional[Callable[[], bool]] = None,
                    on_try: Optional[Callable[[str], None]] = None) -> dict:
        """多帧依次尝试，任一帧命中即返回。"""
        rows: List[dict] = []
        started = time.time()
        for idx, frame in enumerate(frames):
            if should_stop and should_stop():
                break
            if on_try:
                try:
                    on_try(f"第 {idx + 1} 帧")
                except Exception:                            # noqa: BLE001
                    pass
            res = self.recover(frame, should_stop=should_stop)
            res["frame"] = idx + 1
            rows.append(res)
            if res.get("ok"):
                res["attempts"] = rows
                return res
        return {"ok": False, "key": "", "candidates": 0, "order": "", "attempts": rows,
                "elapsed": round(time.time() - started, 2),
                "detail": "本地算法未命中任何采集帧"}

    # -- 官方向量自检 ------------------------------------------------
    def run_vectors(self, limit: Optional[int] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> dict:
        """跑官方 mfkey32v2 测试向量（期望 KEYA=A0A1A2A3A4A5）。"""
        mod = self.load()
        if mod is None:
            return {"ok": False, "hits": 0, "total": 0,
                    "error": self._error or "本地算法不可用", "rows": []}
        vectors = list(LOCAL_VECTORS)
        if limit:
            vectors = vectors[:int(limit)]
        started = time.time()
        rows: List[dict] = []
        hits = 0
        for idx, vec in enumerate(vectors):
            if should_stop and should_stop():
                break
            frame = NonceFrame.from_sequence([int(v, 16) for v in vec])
            res = self.recover(frame, should_stop=should_stop)
            hit = bool(res.get("ok")) and res.get("key_int") == VECTOR_EXPECT
            hits += 1 if hit else 0
            rows.append({"index": idx + 1, "hex": " ".join(vec), "ok": hit,
                         "key": res.get("key", ""), "order": res.get("order", ""),
                         "candidates": res.get("candidates", 0),
                         "elapsed": res.get("elapsed", 0)})
        return {"ok": hits == len(rows) and bool(rows), "hits": hits,
                "total": len(rows), "expect": fmt12(VECTOR_EXPECT),
                "elapsed": round(time.time() - started, 2), "rows": rows,
                "error": "" if hits == len(rows) and rows else self._error}


# ---------------------------------------------------------------- 云端解算
def cloud_config(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读取云端配置（环境变量优先，方便打包后现场改）。

    地址为空时回落到厂商官方默认 recovery 地址，保证"默认优先走官方云端"开箱即用；
    token 不做兜底（recovery 要的是登录态 token，预置无效值只会白拿 401），留空时由云端
    链路给出明确的中文降级原因，提示用户去粘贴自己的 token。
    """
    data = dict(settings or {})
    endpoint = (os.environ.get(ENV_ENDPOINT) or str(data.get("cloud_endpoint", "") or "")).strip()
    token = (os.environ.get(ENV_TOKEN) or str(data.get("cloud_token", "") or "")).strip()
    if not endpoint:
        endpoint = DEFAULT_CLOUD_ENDPOINT
    try:
        timeout = float(os.environ.get("NFCMINI_CLOUD_TIMEOUT") or data.get("cloud_timeout") or DEFAULT_CLOUD_TIMEOUT)
    except Exception:                                    # noqa: BLE001
        timeout = DEFAULT_CLOUD_TIMEOUT
    return {"endpoint": endpoint, "token": token,
            "timeout": max(1.0, min(timeout, 120.0))}


def vendor_urls(endpoint: str = "") -> Dict[str, str]:
    """把设置里的地址规整成厂商三件套：recovery / base / keygen。

    允许三种填法：
      * 完整 recovery 地址（默认预置值）        -> 原样使用；
      * 厂商 base 地址（.../mini-program/v1）   -> 自动补 /recovery/mfkey32v2；
      * 自定义第三方接口                        -> 原样当 recovery 端点用，不做拼接。
    """
    ep = str(endpoint or "").strip()
    if ep and not re.match(r"^https?://", ep, re.I):
        ep = VENDOR_BASE.rstrip("/") + "/" + ep.lstrip("/")
    ep = ep or DEFAULT_CLOUD_ENDPOINT
    lowered = ep.lower()
    idx = lowered.find("/recovery/")
    if idx >= 0:
        return {"recovery": ep, "base": ep[:idx], "keygen": VENDOR_KEYGEN_BASE}
    host = (urlparse(ep).hostname or "").lower()
    tail = ep.rstrip("/")
    if any(h in host for h in VENDOR_HOSTS) and (
            tail.lower().endswith("/v1") or "mini-program" in tail.lower()
            or tail.lower().endswith(VENDOR_RECOVERY_PATH.rstrip("/"))):
        if tail.lower().endswith(VENDOR_RECOVERY_PATH.rstrip("/")):
            return {"recovery": tail, "base": tail[: -len(VENDOR_RECOVERY_PATH)],
                    "keygen": VENDOR_KEYGEN_BASE}
        return {"recovery": tail + VENDOR_RECOVERY_PATH, "base": tail,
                "keygen": VENDOR_KEYGEN_BASE}
    return {"recovery": ep, "base": ep, "keygen": VENDOR_KEYGEN_BASE}


def _frame_u32(frame: NonceFrame) -> Dict[str, int]:
    """厂商 recovery/mfkey32v2 的真实字段映射（小程序 q() 原型，逐字节核对）：

        厂商字段          本工程采集帧            含义
        uid              frame.uid               卡 UID（4 字节）
        nt1/nr1/ar1      nt0/nr0_enc/ar0_enc     第一轮认证采样
        nt2/nr2/ar2      nt1/nr1_enc/ar1_enc     第二轮认证采样
    """
    return {
        "uid": frame.uid & M32,
        "nt1": frame.nt0 & M32,
        "nr1": frame.nr0_enc & M32,
        "ar1": frame.ar0_enc & M32,
        "nt2": frame.nt1 & M32,
        "nr2": frame.nr1_enc & M32,
        "ar2": frame.ar1_enc & M32,
    }


def vendor_frame_body(frame: NonceFrame, byte_arrays: bool = False) -> Dict[str, Any]:
    """构造请求体：默认数值型（厂商真实协议）；byte_arrays=True 给出字节数组格式兜底。"""
    body = _frame_u32(frame)
    if not byte_arrays:
        return dict(body)
    return {k: ["0x%02x" % b for b in int(v).to_bytes(4, "big")] for k, v in body.items()}


def _bytes_to_key(value: Any) -> Optional[str]:
    """[0x2a,0x23,0x4f,0x80,0x57,0xe6] / [42,35,...] -> '2A234F8057E6'。"""
    if not isinstance(value, (list, tuple)):
        return norm_key(value)
    nums: List[int] = []
    for item in value:
        try:
            nums.append(u32(item) & 0xFF)
        except EngineError:
            return None
    if len(nums) < 6:
        return None
    return "".join("%02X" % b for b in nums[:6])


def parse_vendor_keys(payload: Dict[str, Any]) -> List[str]:
    """解析厂商响应中的密钥：rawKeys（字节数组，双层/单层）与 keys（0x 数组 / hex 串）。"""
    out: List[str] = []

    def push(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if not value:
                return
            if all(isinstance(x, (list, tuple)) for x in value):
                for item in value:
                    key = _bytes_to_key(item)
                    if key:
                        out.append(key)
                return
            if all(isinstance(x, str) for x in value):
                for item in value:                       # ["0x2a23..","0x.."] 多把密钥
                    if re.fullmatch(r"(0x)?[0-9a-fA-F]{12,14}", item.strip()):
                        key = norm_key(item)
                        if key:
                            out.append(key)
                return
            key = _bytes_to_key(value)                   # [42,35,79,128,87,230] 单把密钥
            if key:
                out.append(key)
            return
        if isinstance(value, str) and not re.fullmatch(r"(0x)?[0-9a-fA-F]{12,14}", value.strip()):
            return
        key = norm_key(value)
        if key:
            out.append(key)

    for field in ("rawKeys", "rawkeys", "keys", "key"):
        if payload.get(field):
            push(payload[field])
            if out:
                break
    seen, uniq = set(), []
    for key in out:
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    return uniq


def parse_vendor_response(payload: Any) -> dict:
    """按厂商网关真实响应协议解析：{status, message, keys | rawKeys}。"""
    if not isinstance(payload, dict):
        return {"ok": False, "key": "", "keys": [], "detail": "云端返回格式异常", "raw": payload}
    message = str(payload.get("message") or "").strip()
    try:
        status = int(payload.get("status"))
    except (TypeError, ValueError):
        status = 0
    if status == VENDOR_STATUS_OK:
        keys = parse_vendor_keys(payload)
        if keys:
            return {"ok": True, "key": keys[0], "keys": keys, "status": status,
                    "detail": f"云端 status=200，返回 {len(keys)} 把候选密钥", "raw": payload}
        return {"ok": False, "key": "", "keys": [], "status": status,
                "detail": message or "云端 status=200 但未返回密钥", "raw": payload}
    if status == VENDOR_STATUS_NO_CARD:
        return {"ok": False, "key": "", "keys": [], "status": status, "unsupported": True,
                "detail": "厂商云端拒解：Card is compatible type（该卡型不支持 mfkey32）",
                "raw": payload}
    if status == VENDOR_STATUS_GEN3:
        return {"ok": False, "key": "", "keys": [], "status": status, "unsupported": True,
                "detail": "厂商云端拒解：Card is Gen3 type（Gen3 卡不支持 mfkey32）",
                "raw": payload}
    if status in (401, 403) or "token" in message.lower():
        return {"ok": False, "key": "", "keys": [], "status": status, "auth": True,
                "detail": f"厂商云端鉴权失败：{message or 'token 无效'}", "raw": payload}
    return {"ok": False, "key": "", "keys": [], "status": status,
            "detail": message or f"云端返回 status={status or '未知'}", "raw": payload}


def _vendor_request(url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]],
                    timeout: float, method: str = "POST") -> Any:
    """按厂商网关要求发请求（JSON 请求体 + 自定义鉴权头），返回解析后的 JSON。"""
    data = None
    if method.upper() == "POST":
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:                                # noqa: BLE001
            pass
        if text.strip():
            try:
                return json.loads(text)
            except ValueError:
                pass
        raise EngineError(f"云端请求失败：HTTP {exc.code} {text[:200]}".strip()) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise CloudTimeout(f"云端请求超时（{timeout}s）") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise CloudTimeout(f"云端请求超时（{timeout}s）") from exc
        raise EngineError(f"云端不可达：{exc.reason or exc}") from exc
    except CloudTimeout:
        raise
    except Exception as exc:                             # noqa: BLE001
        raise EngineError(f"云端请求异常：{type(exc).__name__}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError as exc:
        raise EngineError("云端返回不是合法 JSON") from exc


def _pick_key(keys: Sequence[str], verify: Optional[Callable[[str], bool]] = None) -> str:
    """从候选密钥里挑一把可用密钥：有 verify 回调就交给设备认证确认，否则取第一把。

    注意：旧实现硬性只验证前 12 把（CLOUD_VERIFY_LIMIT=12），而官方 keygen 对
    一张普通卡会返回 500+ 把候选密钥 —— 真正能开门的那把常常排在 12 名之外，
    表现就是"明明抓到 500 多把，却怎么都解不出来"。现在默认全表逐把验证
    （单次设备认证 ~10ms，541 把约 6~8 秒），并把进度回报给上层。
    """
    items = list(keys)
    if CLOUD_VERIFY_LIMIT and CLOUD_VERIFY_LIMIT > 0:
        items = items[:CLOUD_VERIFY_LIMIT]
    for key in items:
        if not verify:
            return str(key)
        try:
            if verify(str(key)):
                return str(key)
        except Exception:                                # noqa: BLE001
            continue
    return ""


def _keys_from_anything(node: Any, depth: int = 0) -> List[str]:
    """宽松兜底：从任意响应结构里挖出密钥（供 keygen 等非标准响应使用）。"""
    if depth > 5 or node is None or isinstance(node, bool):
        return []
    if isinstance(node, dict):
        got = parse_vendor_keys(node)
        if got:
            return got
        out: List[str] = []
        for value in node.values():
            out.extend(_keys_from_anything(value, depth + 1))
        return out
    if isinstance(node, (list, tuple)):
        if node and all(isinstance(x, (list, tuple)) for x in node):
            out = []
            for item in node:
                key = _bytes_to_key(item)
                if key:
                    out.append(key)
            if out:
                return out
        if len(node) >= 6 and all(isinstance(x, (int, float, str)) for x in node):
            key = _bytes_to_key(node)
            return [key] if key else []
        out = []
        for item in node:
            out.extend(_keys_from_anything(item, depth + 1))
        return out
    if isinstance(node, str) and not re.fullmatch(r"(0x)?[0-9a-fA-F]{12,14}", node.strip()):
        return []
    key = norm_key(node)
    return [key] if key else []


def cloud_keygen_lookup(uid: Any, base: str = VENDOR_KEYGEN_BASE, token: str = "",
                        timeout: float = DEFAULT_CLOUD_TIMEOUT,
                        should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """官方云端密钥库（nfctoolsapp.nfcreader.net/keygen）：按 UID 提交并轮询取全卡密钥。

    小程序实现：POST /submit {uid} -> {errcode, payload: <id>}；
                GET  /query?id=<id> -> {errcode, payload: {status, result:[...]}}。
    未收录/仍在计算时抛 CloudPending，由上层降级本地解算。
    """
    if uid in (None, ""):
        raise EngineError("缺少 UID，无法查询官方云端密钥库")
    uid_int = _uid_int(uid)          # UID 是十六进制文本，不可按十进制解析
    root = str(base or VENDOR_KEYGEN_BASE).rstrip("/")
    # keygen 用独立固定凭据（Authorization），与 recovery 的 NICETHEME 登录态 token 无关
    tok = str(token or "").strip() or VENDOR_KEYGEN_TOKEN
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": tok, "User-Agent": VENDOR_UA}
    # 厂商 keygen 对 uid 有强类型校验（实测传整型返回 422 "Input should be a valid string"），
    # 必须传 8 位十六进制字符串，与小程序的 uid 文本格式一致
    submit = _vendor_request(root + "/submit", headers, {"uid": f"{uid_int:08X}"}, timeout)
    if not isinstance(submit, dict):
        raise EngineError(f"官方云端密钥库返回格式异常：{submit!r}")
    if int(submit.get("errcode") or 0) != 0:
        raise EngineError(f"官方云端密钥库提交失败：{submit.get('errmsg') or submit.get('message') or submit}")
    job_id = submit.get("payload")
    if not job_id:
        raise EngineError("官方云端密钥库未返回任务号")
    deadline = time.time() + max(KEYGEN_POLL_TIMEOUT, float(timeout))
    while True:
        if should_stop and should_stop():
            raise EngineError("用户取消")
        if time.time() > deadline:
            raise CloudPending("官方云端密钥库仍在计算（可稍后重试或改用本地 mfkey32）")
        time.sleep(KEYGEN_POLL_INTERVAL)
        query = _vendor_request(f"{root}/query?id={job_id}", headers, None, timeout, method="GET")
        if not isinstance(query, dict):
            continue
        if int(query.get("errcode") or 0) != 0:
            raise EngineError(f"官方云端密钥库查询失败：{query.get('errmsg') or query.get('message') or query}")
        payload = query.get("payload")
        status = ""
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").lower()
        if status in ("completed", "success", "done", "finished"):
            # 官方 keygen 的 payload.result 是"该卡候选密钥全表"（实测单卡可达 500+ 把），
            # 必须整表返回交给设备批量验证，不能只取第一把。
            raw_keys = payload.get("result") if isinstance(payload, dict) else None
            keys: List[str] = []
            if isinstance(raw_keys, list):
                for item in raw_keys:
                    k = norm_key(item)
                    if k:
                        keys.append(k)
            if not keys:
                keys = _keys_from_anything(payload)
            if keys:
                return {"ok": True, "keys": keys, "key": keys[0], "uid": f"{uid_int:08X}",
                        "url": f"{root}/query?id={job_id}",
                        "detail": f"官方云端密钥库命中 {len(keys)} 把候选密钥"}
            raise CloudPending("官方云端密钥库已完成但未返回密钥")
        if status in ("failed", "fail", "error"):
            raise CloudPending(f"官方云端密钥库未收录该卡：{payload.get('result') or payload}")
        keys = _keys_from_anything(payload)
        if keys:
            return {"ok": True, "keys": keys, "key": keys[0], "uid": f"{uid_int:08X}",
                    "url": f"{root}/query?id={job_id}",
                    "detail": f"官方云端密钥库命中 {len(keys)} 把密钥"}


def cloud_solve(frames: Sequence[NonceFrame], endpoint: str, token: str = "",
                timeout: float = DEFAULT_CLOUD_TIMEOUT,
                should_stop: Optional[Callable[[], bool]] = None,
                verify: Optional[Callable[[str], bool]] = None,
                uid: Any = None,
                on_note: Optional[Callable[[str], None]] = None,
                max_frames: int = CLOUD_FRAME_LIMIT) -> dict:
    """调用官方云端解算（默认首选链路），返回 {ok, key, keys, detail, ...}。

    协议严格照厂商小程序实现：POST <base>/recovery/mfkey32v2，请求头 NICETHEME: <token>，
    一次提交一帧 uid/nt1/nr1/ar1/nt2/nr2/ar2。
    ``verify`` 为可选回调（拿回设备做真实认证校验），返回 True 表示该候选密钥可用。
    无采集帧但有 UID 时，先查官方云端密钥库（keygen）。
    """
    urls = vendor_urls(endpoint)
    # recovery 的 NICETHEME 只能用登录态 token：不兜底、不用 keygen 固定凭据冒充
    tok = str(token or "").strip()
    notes: List[str] = []

    def note(text: str) -> None:
        notes.append(text)
        if on_note:
            try:
                on_note(text)
            except Exception:                            # noqa: BLE001
                pass

    frame_list = list(frames or [])
    if should_stop and should_stop():
        raise EngineError("用户取消")

    if not frame_list and uid not in (None, ""):
        try:
            kg = cloud_keygen_lookup(uid, token=tok, timeout=timeout, should_stop=should_stop)
        except CloudTimeout:
            note("官方云端密钥库超时")
        except CloudAuthError:
            raise
        except EngineError as exc:
            note(f"官方云端密钥库未命中：{exc}")
        else:
            cands = list(kg.get("keys") or [])
            note(f"官方云端密钥库命中 {len(cands)} 把候选密钥，逐把交设备认证…")
            # 逐把验证必须可取消、有进度：固件单次认证约 0.2s，500+ 把要一两分钟，
            # 期间没有任何回显会让用户以为程序卡死（只能强杀进程）。
            chosen = ""
            for idx, cand in enumerate(cands, start=1):
                if should_stop and should_stop():
                    raise EngineError("用户取消")
                if not verify:
                    chosen = str(cand)
                    break
                try:
                    if verify(str(cand)):
                        chosen = str(cand)
                        break
                except Exception:                        # noqa: BLE001
                    continue
                if idx % 20 == 0 or idx == len(cands):
                    note(f"已验证 {idx}/{len(cands)} 把候选密钥…")
            if chosen:
                return {"ok": True, "key": chosen, "keys": kg.get("keys") or [chosen],
                        "detail": f"官方云端密钥库命中：{kg.get('detail', '')}",
                        "endpoint": kg.get("url", ""), "source": "keygen",
                        "cloud": {"source": "keygen", "keys": kg.get("keys") or [],
                                  "endpoint": kg.get("url", ""), "frame": 0},
                        "notes": notes}
            note("官方云端密钥库返回的密钥未通过设备认证")
    if not frame_list:
        raise EngineError("没有可用的 mfkey32 采集数据")
    if not tok:
        raise CloudAuthError(
            "云端 recovery 接口需要小程序登录态 token（请求头 NICETHEME），当前未配置；"
            "请在「破解模式」卡片里粘贴自己的 token 后重试，本次已降级本地 mfkey32")

    problems: List[str] = []
    for idx, frame in enumerate(frame_list[: max(1, int(max_frames))]):
        if should_stop and should_stop():
            raise EngineError("用户取消")
        for label, byte_arrays in (("数值体", False), ("字节数组体", True)):
            try:
                payload = _vendor_request(
                    urls["recovery"],
                    {"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json", AUTH_HEADER: tok, "User-Agent": VENDOR_UA},
                    vendor_frame_body(frame, byte_arrays), timeout)
            except CloudTimeout:
                raise
            except EngineError as exc:
                if byte_arrays:
                    problems.append(f"第 {idx + 1} 帧：{exc}")
                continue
            res = parse_vendor_response(payload)
            status = res.get("status") or 0
            if res.get("auth"):
                raise CloudAuthError(f"{res['detail']}（请在设置里更新云端 token）")
            if res.get("unsupported"):
                raise CloudUnsupported(str(res["detail"]))
            if res.get("ok"):
                keys = res["keys"]
                chosen = _pick_key(keys, verify)
                if chosen:
                    return {"ok": True, "key": chosen, "keys": keys,
                            "detail": f"官方云端解算命中：{res.get('detail', '')}",
                            "endpoint": urls["recovery"], "frame": idx + 1,
                            "source": "recovery",
                            "cloud": {"source": "recovery", "keys": keys,
                                      "endpoint": urls["recovery"], "frame": idx + 1,
                                      "status": status},
                            "notes": notes}
                note(f"第 {idx + 1} 帧云端解出 {len(keys)} 把密钥，但设备认证均未通过")
                break
            if not byte_arrays and status in (400, 422):
                continue                                 # 可能是请求体形态不符 → 换字节数组体重试
            problems.append(f"第 {idx + 1} 帧：{res.get('detail')}")
            break
    detail = "；".join(problems[-3:]) or "云端未返回可用密钥"
    raise EngineError(f"云端解算未成功：{detail}")


_RESP_KEY_FIELDS = ("key", "keys", "keya", "key_a", "keyb", "key_b", "sector_key",
                    "candidates", "candidate", "value", "hex", "data_key")
_RESP_CONTAINERS = ("data", "result", "results", "payload", "body", "list",
                    "items", "sectors", "records", "content")


def _resp_flag(payload: Any) -> Optional[bool]:
    if not isinstance(payload, dict):
        return None
    for field in ("ok", "success", "valid", "hit", "matched"):
        if field in payload:
            value = payload[field]
            if value is False:
                return False
            if isinstance(value, str) and value.strip().lower() in ("false", "fail", "failed", "no", "0"):
                return False
            return True
    for field in ("status", "code"):
        if field in payload:
            text = str(payload[field]).strip().lower()
            if text in ("200", "0", "ok", "success", "true"):
                return True
            if re.fullmatch(r"\d+", text):
                return False
    return None


def _resp_find_key(node: Any, depth: int = 0) -> Optional[str]:
    if depth > 4 or node is None or isinstance(node, bool):
        return None
    if isinstance(node, (int, float, str)):
        return norm_key(node)
    if isinstance(node, dict):
        lowered = {str(k).lower(): v for k, v in node.items()}
        for field in _RESP_KEY_FIELDS:
            if field in lowered:
                got = _resp_find_key(lowered[field], depth + 1)
                if got:
                    return got
        for field in _RESP_CONTAINERS:
            if field in lowered:
                got = _resp_find_key(lowered[field], depth + 1)
                if got:
                    return got
        return None
    if isinstance(node, (list, tuple)):
        for item in list(node)[:8]:
            got = _resp_find_key(item, depth + 1)
            if got:
                return got
    return None


def parse_cloud_response(payload: Any) -> dict:
    """宽容解析云端响应。

    支持形态：{ok,key} / {success:1,data:{key}} / {keys:[..]} /
    {data:[{sector,key}]} / {result:"0xA0A1..."} / ["a0a1a2a3a4a5"] / 嵌套容器。
    """
    key = _resp_find_key(payload)
    flag = _resp_flag(payload)
    if not key:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("msg") or payload.get("message")
                         or payload.get("error") or payload.get("detail") or "")
        if flag is False and not detail:
            detail = "云端返回失败状态"
        return {"ok": False, "key": "", "keys": [], "detail": detail or "响应里没有密钥字段",
                "raw": payload}
    return {"ok": True, "key": key, "keys": [key], "detail": "",
            "ok_flag": flag, "raw": payload}


# ------------------------------------------------------------------ 门面
class CrackEngine:
    """破解门面：按模式给出 云端 -> 本地 -> 固件级联 的执行链并逐步降级。"""

    def __init__(self, settings: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str, str], None]] = None) -> None:
        self.local = LocalEngine()
        self._settings = settings or {}
        self._log = log

    # -- 配置 --------------------------------------------------------
    def bind(self, settings: Optional[Dict[str, Any]]) -> None:
        self._settings = dict(settings or {})

    @property
    def settings(self) -> Dict[str, Any]:
        return self._settings

    def mode(self, override: Optional[str] = None) -> str:
        raw = override or os.environ.get("NFCMINI_CRACK_MODE") or self._settings.get("crack_mode") or MODE_CLOUD
        mode = str(raw).strip().lower()
        if mode in ("local", "offline", "本地"):
            return MODE_LOCAL
        return MODE_CLOUD

    def chain_for(self, mode: Optional[str] = None) -> List[str]:
        """当前模式下的引擎执行顺序（最后一定会挂上固件级联兜底）。"""
        mode = self.mode(mode)
        if mode == MODE_LOCAL:
            return [MODE_LOCAL, MODE_DEVICE]
        return [MODE_CLOUD, MODE_LOCAL, MODE_DEVICE]

    def status(self, override: Optional[str] = None) -> dict:
        conf = cloud_config(self._settings)
        urls = vendor_urls(conf["endpoint"])
        return {
            "mode": self.mode(override),
            "modes": MODE_CHOICES,
            "chain": self.chain_for(override),
            "labels": ENGINE_LABELS,
            "local": {"available": self.local.available, "error": self.local.error,
                      "algorithm": "mfkey32v2（proxmark3 移植 · numba 加速）",
                      "bits": self.local.bits, "vectors": len(LOCAL_VECTORS)},
            "cloud": {"endpoint": conf["endpoint"], "has_token": bool(conf["token"]),
                      "login_token": bool(conf["token"]),
                      "needs_login_token": not conf["token"],
                      "timeout": conf["timeout"],
                      "recovery": urls["recovery"],
                      "auth_header": AUTH_HEADER,
                      "keygen": VENDOR_KEYGEN_BASE,
                      "keygen_builtin_token": bool(VENDOR_KEYGEN_TOKEN),
                      "default_endpoint": DEFAULT_CLOUD_ENDPOINT,
                      "default_token": DEFAULT_CLOUD_TOKEN,
                      "using_default": conf["endpoint"] == DEFAULT_CLOUD_ENDPOINT},
        }

    # -- 解算 --------------------------------------------------------
    def solve(self, mode: Optional[str] = None,
              frames: Optional[Sequence[NonceFrame]] = None, sector: int = 0,
              progress: Optional[Callable[[int, str], None]] = None,
              should_stop: Optional[Callable[[], bool]] = None,
              on_engine: Optional[Callable[[str, bool, str], None]] = None,
              allow_device_fallback: bool = True,
              uid: Any = None,
              verify: Optional[Callable[[str], bool]] = None) -> dict:
        """执行双模式解算；返回与 device.crack_chain 同构的结果 + engine 字段。

        ``uid``：卡 UID（云端首次解算前的官方密钥库查询用）；
        ``verify``：候选密钥的设备认证回调（由 api 层注入），保证云端返回的密钥真的能用。
        """
        mode = self.mode(mode)
        frames = list(frames or [])
        conf = cloud_config(self._settings)
        started = time.time()
        attempts: List[dict] = []

        def emit(pct: int, msg: str) -> None:
            if progress:
                try:
                    progress(int(pct), msg)
                except Exception:                        # noqa: BLE001
                    pass

        def report(engine: str, ok: bool, detail: str) -> None:
            if on_engine:
                try:
                    on_engine(engine, ok, detail)
                except Exception:                        # noqa: BLE001
                    pass

        def stopped() -> bool:
            return bool(should_stop and should_stop())

        def ok_result(engine: str, key: str, detail: str, extra: dict) -> dict:
            row = {"method": engine, "label": ENGINE_LABELS.get(engine, engine),
                   "ok": True, "elapsed": round(time.time() - started, 2), "keys": [key],
                   "detail": detail}
            attempts.append(row)
            return {"ok": True, "method": engine, "label": row["label"], "keys": [key],
                    "key": key, "sector": int(sector), "engine": engine,
                    "attempts": attempts, "elapsed": row["elapsed"],
                    "detail": detail, "fallback_to_device": False,
                    "nonces": len(frames), "mode": mode, **extra}

        def fail(engine: str, detail: str, elapsed: float = 0.0) -> None:
            attempts.append({"method": engine, "label": ENGINE_LABELS.get(engine, engine),
                             "ok": False, "elapsed": round(elapsed, 2), "detail": detail})
            report(engine, False, detail)

        # ---- 1) 云端（默认首选：官方 recovery 接口，无采集帧时先查官方密钥库）----
        if mode == MODE_CLOUD:
            if stopped():
                return self._cancelled(sector, mode, attempts, frames)
            urls = vendor_urls(conf["endpoint"])
            if not conf["endpoint"]:
                msg = "未配置云端解算接口，自动降级本地离线算法"
                self._log_line("warn", msg)
                fail(MODE_CLOUD, "未配置云端解算接口", 0.0)
            elif not frames and uid in (None, ""):
                msg = "没有 mfkey32 采集数据，跳过云端解算"
                self._log_line("warn", msg)
                fail(MODE_CLOUD, "缺少 mfkey32 采集数据（nt/nr_enc/ar_enc）", 0.0)
            else:
                emit(6, "云端解算中（官方 recovery 接口）…")
                self._log_line("info", f"云端解算：官方接口 {urls['recovery']}，"
                                       f"鉴权头 {AUTH_HEADER}，{len(frames)} 帧采集数据"
                                       + (f"，UID {uid}" if uid not in (None, "") else ""))
                report(MODE_CLOUD, True, "请求中")
                t0 = time.time()
                try:
                    res = cloud_solve(frames, conf["endpoint"], conf["token"], conf["timeout"],
                                      should_stop=should_stop, verify=verify, uid=uid,
                                      on_note=lambda text: self._log_line("info", f"云端：{text}"))
                    self._log_line("ok", f"云端解算命中密钥 {res['key']}（{round(time.time() - t0, 2)}s）")
                    return ok_result(MODE_CLOUD, res["key"],
                                     res.get("detail") or f"云端命中（候选 {len(res.get('keys') or [])}）",
                                     {"cloud": res.get("cloud") or {},
                                      "cloud_source": res.get("source", ""),
                                      "cloud_keys": res.get("keys") or []})
                except Exception as exc:                 # noqa: BLE001
                    detail = str(exc)
                    self._log_line("warn", f"{detail}，自动降级本地离线算法")
                    fail(MODE_CLOUD, detail, time.time() - t0)
                    report(MODE_CLOUD, False, detail)

        # ---- 2) 本地 ----
        if stopped():
            return self._cancelled(sector, mode, attempts, frames)
        if not self.local.available:
            detail = f"本地算法不可用：{self.local.error or '模块导入失败'}"
            self._log_line("warn", detail)
            fail(MODE_LOCAL, detail, 0.0)
        elif not frames:
            detail = "缺少 mfkey32 采集数据（需要两轮认证的 nt/nr_enc/ar_enc）"
            fail(MODE_LOCAL, detail, 0.0)
        else:
            self._log_line("info", f"本地 mfkey32：{len(frames)} 帧采集数据，"
                                   f"bits={self.local.bits}，端序自动重试")
            report(MODE_LOCAL, True, "解算中")
            total = max(1, len(frames))
            counter = {"i": 0}

            def on_try(label: str) -> None:
                counter["i"] += 1
                pct = 12 + int(min(1.0, counter["i"] / (total * 4)) * 78)
                emit(pct, f"本地解算中（第 {min(total, (counter['i'] - 1) // 4 + 1)} 帧 · {label}）")

            t0 = time.time()
            try:
                res = self.local.recover_any(frames, should_stop=should_stop, on_try=on_try)
            except Exception as exc:                     # noqa: BLE001
                res = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            if res.get("ok"):
                detail = f"本地命中（{res.get('order') or '原序'}，候选 {res.get('candidates', 0)}）"
                self._log_line("ok", f"本地解算命中密钥 {res['key']}（{round(time.time() - t0, 2)}s）")
                return ok_result(MODE_LOCAL, res["key"], detail, {"order": res.get("order", ""),
                                                                  "tried": res.get("tried") or res.get("attempts")})
            detail = res.get("detail") or "本地算法未命中"
            self._log_line("warn", f"本地解算未命中：{detail}")
            fail(MODE_LOCAL, detail, time.time() - t0)
            report(MODE_LOCAL, False, detail)

        # ---- 3) 固件级联兜底（交回 device.crack_chain）----
        summary = "云端/本地解算均未命中，改用固件级联破解" if mode == MODE_CLOUD \
            else "本地解算未命中，改用固件级联破解"
        if not allow_device_fallback:
            summary = "云端/本地解算均未命中"
        emit(92, summary)
        return {"ok": False, "method": "", "label": "", "keys": [], "sector": int(sector),
                "attempts": attempts, "elapsed": round(time.time() - started, 2),
                "summary": summary, "mode": mode, "engine": "", "nonces": len(frames),
                "fallback_to_device": bool(allow_device_fallback and not stopped())}

    @staticmethod
    def _cancelled(sector: int, mode: str, attempts: List[dict],
                   frames: Sequence[NonceFrame]) -> dict:
        attempts.append({"method": "", "label": "已取消", "ok": False, "elapsed": 0.0,
                         "detail": "用户取消"})
        return {"ok": False, "method": "", "label": "", "keys": [], "sector": int(sector),
                "attempts": attempts, "elapsed": 0.0, "summary": "用户取消破解",
                "mode": mode, "engine": "", "nonces": len(frames),
                "fallback_to_device": False}

    def _log_line(self, level: str, text: str) -> None:
        if self._log:
            try:
                self._log(level, text)
            except Exception:                            # noqa: BLE001
                pass
