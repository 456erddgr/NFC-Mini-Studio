# -*- coding: utf-8 -*-
"""微信小程序 token 自动提取（自动抓取 NICETHEME 登录态）。

原理
----
微信 PC 版运行小程序时，WeChatAppEx.exe 进程内存里保留着小程序自己的
UserLog（wx.getLogManager 的缓冲），其中记录了 wx.request 的请求与响应原文。
厂商小程序「宜新 NFC Reader Tool」登录后，POST /login 的响应体里就带着
userInfo.token（JWT），本模块只读扫描进程内存把它取出来，供云端 recovery 使用。

边界
----
* 只读扫描（OpenProcess + ReadProcessMemory + VirtualQueryEx），不注入、不修改内存；
* 只针对本机、当前用户自己的微信账号；
* 需要微信已登录、且小程序正在运行（token 只存在于运行期内存）。
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from typing import Dict, List, Optional

IS_WINDOWS = True
try:
    import ctypes
    import ctypes.wintypes as wt

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
except Exception:                                   # pragma: no cover - 非 Windows
    IS_WINDOWS = False
    ctypes = None                                    # type: ignore
    wt = None                                        # type: ignore
    _K32 = None                                      # type: ignore

# JWT（header.payload.signature，base64url）；内存里可能被转义，但 JWT 本身不含反斜杠
_JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
_READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
_MAX_REGION = 256 * 1024 * 1024

# 厂商小程序 appid + 关键域名，用于在多小程序间优先锁定目标
TARGET_APPID = "wxc3dd33d9d4ae0fd6"
TARGET_HOST = "nfcreader.net"


class _MBI(ctypes.Structure if IS_WINDOWS else object):      # type: ignore[misc]
    if IS_WINDOWS:
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wt.DWORD),
            ("_pad", wt.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wt.DWORD),
            ("Protect", wt.DWORD),
            ("Type", wt.DWORD),
        ]


def _jwt_payload(token: str) -> dict:
    """解出 JWT 的 payload（只用于读 uid / exp，不校验签名）。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = base64.urlsafe_b64decode(part.encode("ascii"))
        obj = json.loads(data.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _wechat_pids() -> List[int]:
    """当前用户下所有 WeChatAppEx.exe 进程 PID，按内存升序（小程序渲染进程通常较小）。"""
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process WeChatAppEx -ErrorAction SilentlyContinue | "
             "Sort-Object WorkingSet64 | Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=8,
        )
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _read_regions(pid: int) -> List[bytes]:
    """只读读出一个进程可读内存区域的内容（跳过超大区域）。"""
    chunks: List[bytes] = []
    handle = _K32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return chunks
    try:
        addr = 0
        mbi = _MBI()
        size_mbi = ctypes.sizeof(mbi)
        while _K32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), size_mbi):
            if mbi.State == MEM_COMMIT and mbi.Protect in _READABLE and mbi.RegionSize <= _MAX_REGION:
                buf = ctypes.create_string_buffer(mbi.RegionSize)
                got = ctypes.c_size_t(0)
                if _K32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf,
                                          mbi.RegionSize, ctypes.byref(got)):
                    chunks.append(buf.raw[: got.value])
            nxt = addr + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
    except Exception:
        pass
    finally:
        _K32.CloseHandle(handle)
    return chunks


def _pick_token(chunks: List[bytes]) -> Optional[dict]:
    """从内存块里挑出最像小程序登录 token 的 JWT。"""
    candidates: List[dict] = []
    for data in chunks:
        # 优先：命中 login 响应 / userInfo 的区域
        preferred = (b"login result" in data) or (b"userInfo" in data) or (TARGET_HOST.encode() in data)
        for m in _JWT_RE.finditer(data):
            token = m.group(0).decode("ascii", "replace")
            payload = _jwt_payload(token)
            uid = payload.get("uid")
            exp = payload.get("exp")
            if not isinstance(exp, int):
                continue
            score = 0
            if preferred:
                score += 4
            if isinstance(uid, int):
                score += 3
            if b"userInfo" in data[max(0, m.start() - 400): m.start()]:
                score += 3
            candidates.append({"token": token, "uid": uid, "exp": exp, "score": score})
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["score"], c["exp"]), reverse=True)
    return candidates[0]


def fetch_token(timeout_seconds: float = 20.0) -> Dict[str, object]:
    """扫描微信小程序进程，返回登录 token 及账号信息。

    返回 dict：
        成功 {"ok": True, "token", "uid", "nickname", "exp", "expires_in", "pid"}
        失败 {"ok": False, "error": "中文原因"}
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "该功能仅支持 Windows 微信 PC 版"}
    pids = _wechat_pids()
    if not pids:
        return {"ok": False, "error": "没有检测到微信小程序进程（WeChatAppEx.exe）。请先打开微信并进入「宜新 NFC Reader Tool」小程序。"}
    deadline = time.time() + max(3.0, float(timeout_seconds))
    for pid in pids:
        if time.time() > deadline:
            break
        chunks = _read_regions(pid)
        hit = _pick_token(chunks)
        if hit:
            exp = int(hit["exp"])
            now = int(time.time())
            return {
                "ok": True,
                "token": hit["token"],
                "uid": hit.get("uid"),
                "exp": exp,
                "expires_in": exp - now,
                "expired": exp <= now,
                "pid": pid,
            }
    return {"ok": False, "error": "已在微信小程序进程里扫描，但没有找到登录 token。请确认：①微信已登录；②已打开「宜新 NFC Reader Tool」小程序；③该小程序里是登录状态。"}