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
import os
import re
import subprocess
import time
from pathlib import Path
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


def jwt_exp(token: str) -> int:
    """取 JWT 的 exp（秒级时间戳）；拿不到返回 0。"""
    payload = _jwt_payload(token)
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, int) else 0


# ---------------------------------------------------------------- 拉起微信
WECHAT_CANDIDATES = (
    r"C:\Program Files\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
)


def find_wechat_exe() -> str:
    """找微信 PC 版主程序：先查注册表 App Paths，再查常见安装路径。"""
    if not IS_WINDOWS:
        return ""
    for exe in ("WeChat.exe", "Weixin.exe"):
        try:
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                path = str(winreg.QueryValue(key, None) or "")
            if path and Path(path).exists():
                return path
        except Exception:                            # noqa: BLE001
            pass
    for path in WECHAT_CANDIDATES:
        if Path(path).exists():
            return path
    # 最后兜底：扫常见安装根目录
    for base in (r"C:\Program Files\Tencent", r"C:\Program Files (x86)\Tencent",
                 r"D:\Program Files\Tencent", r"E:\Program Files\Tencent"):
        b = Path(base)
        if not b.exists():
            continue
        for name in ("WeChat.exe", "Weixin.exe"):
            hit = next(b.rglob(name), None)
            if hit:
                return str(hit)
    return ""


def launch_wechat() -> Dict[str, object]:
    """启动微信 PC 版（已在运行则直接返回）。"""
    if not IS_WINDOWS:
        return {"ok": False, "error": "该功能仅支持 Windows 微信 PC 版"}
    for name in ("WeChat", "Weixin"):
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe"],
                                 capture_output=True, text=True, timeout=10)
            if f"{name}.exe" in (out.stdout or ""):
                return {"ok": True, "already_running": True, "exe": name}
        except Exception:                            # noqa: BLE001
            pass
    exe = find_wechat_exe()
    if not exe:
        return {"ok": False, "error": "没有找到微信主程序，请确认已安装微信 PC 版"}
    try:
        subprocess.Popen([exe], close_fds=True)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": f"启动微信失败：{exc}", "exe": exe}
    return {"ok": True, "exe": exe}


def open_miniprogram(appid: str = TARGET_APPID,
                     path: str = "pages/index/index") -> Dict[str, object]:
    """直接拉起目标小程序（微信桌面端的 weixin:// 协议）。

    关键教训（2026-10-09 实测）：**不能**用 `cmd /c start "<uri>"`——
    URI 里的 `&` 会被 cmd 当成命令分隔符，URL 被截断后落到默认浏览器上，
    看起来"打开了"其实根本没进微信。这里改用 Windows 原生 ShellExecute
    （os.startfile），由系统把 URI 交给注册好的协议处理器
    （本机 `weixin://` → `Weixin.exe "%1"`）。
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "该功能仅支持 Windows 微信 PC 版"}
    uri = f"weixin://dl/business/?appid={appid}&path={path}"
    used = ""
    exe = find_wechat_exe()
    # ① 首选：把 URI 当作参数直接交给微信主程序 —— 全程不经过浏览器/系统协议处理器。
    #    （实测把 URI 交给 ShellExecute 时，遇到微信不认的路径会被默认浏览器接走。）
    if exe:
        try:
            subprocess.Popen([exe, uri], close_fds=True)
            used = f"direct:{exe}"
        except Exception:                            # noqa: BLE001
            used = ""
    # ② 退路：系统协议处理器（个别微信版本注册得比较规矩）
    if not used:
        try:
            os.startfile(uri)                        # noqa: S606 - 交给系统协议处理器
            used = "shellexecute"
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": f"拉起小程序失败：{exc}", "uri": uri}
    time.sleep(2.5)
    focused = focus_wechat()
    opened = miniprogram_open()
    return {"ok": True, "uri": uri, "via": used, "wechat_focused": focused,
            "miniprogram_open": opened,
            "hint": ("小程序已在微信中打开" if opened else
                     "微信已就绪，但没能自动打开小程序：请在微信左侧「小程序」面板里点开"
                     "「宜新 NFC Reader Tool」（只需点这一次），抓到登录态后会自动填入")}


def focus_wechat() -> bool:
    """把微信主窗口真正切到前台（并校验），方便用户按教程点开小程序。

    坑：Windows 会拒绝后台进程直接抢前台，SetForegroundWindow 静默失败。
    这里用「ALT 键技巧」解除前台锁定，并用 GetForegroundWindow 校验，
    必要时先发一次微信默认的显示热键 Ctrl+Alt+W 把收进托盘的窗口叫出来。
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32

        def _hwnd() -> int:
            for title in ("微信", "WeChat"):
                h = user32.FindWindowW(None, title)
                if h:
                    return int(h)
            return 0

        hwnd = _hwnd()
        if not hwnd:                                # 可能收进托盘了：按默认热键叫出来
            user32.keybd_event(0x11, 0, 0, 0)       # Ctrl down
            user32.keybd_event(0x12, 0, 0, 0)       # Alt down
            user32.keybd_event(0x57, 0, 0, 0)       # W down
            time.sleep(0.05)
            user32.keybd_event(0x57, 0, 2, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(1.2)
            hwnd = _hwnd()
        if not hwnd:
            return False
        for _ in range(3):
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.keybd_event(0x12, 0, 0, 0)        # ALT down（解除前台锁定）
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.45)
            if int(user32.GetForegroundWindow()) == hwnd:
                return True
        return False
    except Exception:                                # noqa: BLE001
        pass
    return False


def miniprogram_open() -> bool:
    """判断目标小程序窗口是否已经开着（WeChatAppEx 的窗口标题）。"""
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process WeChatAppEx -ErrorAction SilentlyContinue | "
             "Where-Object { $_.MainWindowTitle } | Select-Object -ExpandProperty "
             "MainWindowTitle) -join ' | '"],
            capture_output=True, text=True, timeout=8)
        titles = (out.stdout or "").lower()
        return ("nfc" in titles) or ("宜新" in titles)
    except Exception:                                # noqa: BLE001
        return False


def wait_for_token(deadline_seconds: float = 45.0, interval: float = 3.0,
                   min_exp_left: int = 300) -> Dict[str, object]:
    """轮询抓取 token：等小程序把登录态写进内存，拿到"还没过期"的那把就返回。"""
    deadline = time.time() + max(5.0, float(deadline_seconds))
    last: Dict[str, object] = {"ok": False, "error": "等待超时，未抓到可用的 token"}
    while time.time() < deadline:
        res = fetch_token(timeout_seconds=12.0)
        if res.get("ok") and int(res.get("expires_in") or 0) > min_exp_left:
            return res
        last = res if not res.get("ok") else {
            "ok": False,
            "error": f"抓到的 token 剩余有效期只有 {int(res.get('expires_in') or 0)} 秒，继续等待刷新",
        }
        time.sleep(max(1.0, float(interval)))
    return last


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
