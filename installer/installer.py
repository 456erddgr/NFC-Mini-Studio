# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 一键安装程序
==============================
双击运行 → 点「一键安装」→ 装到 %LOCALAPPDATA%\\Programs\\NFCMiniStudio，
自动创建桌面 / 开始菜单快捷方式，并登记到「应用和功能」可卸载。
全程免管理员权限。
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import winreg
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

# windowed（无控制台）模式下 stdout/stderr 为 None，兜底防止第三方库 print 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_NAME = "NFC Mini Studio"
APP_VERSION = "2.2.0"
PUBLISHER = "只因君"
EXE_NAME = "NFC Mini Studio.exe"
UNINST_NAME = "卸载 NFC Mini Studio.bat"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\NFCMiniStudio"
CREATE_NO_WINDOW = 0x08000000

BG = "#0B1020"
CARD = "#151C33"
TEXT = "#EAF0FF"
MUTED = "#8A97B8"
ACCENT = "#3B82F6"
OK_COLOR = "#22C55E"


# ---------------------------------------------------------------- 路径
def payload_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "payload"
    return Path(__file__).resolve().parent / "payload"


def default_target() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / "NFCMiniStudio"


def powershell(script: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def desktop_dir() -> Path:
    p = powershell('[Environment]::GetFolderPath("Desktop")')
    return Path(p) if p else Path.home() / "Desktop"


def startmenu_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ---------------------------------------------------------------- 安装动作
def make_shortcut(lnk: Path, exe: Path, workdir: Path) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$sc = $ws.CreateShortcut('{lnk}');"
        f"$sc.TargetPath = '{exe}';"
        f"$sc.WorkingDirectory = '{workdir}';"
        f"$sc.IconLocation = '{exe},0';"
        f"$sc.Description = '{APP_NAME}';"
        "$sc.Save()"
    )
    powershell(ps)


UNINSTALL_BAT = r"""@echo off
chcp 65001 >nul
title 卸载 NFC Mini Studio
echo 正在关闭 NFC Mini Studio ...
taskkill /f /im "NFC Mini Studio.exe" >nul 2>&1
timeout /t 1 /nobreak >nul
echo 正在删除快捷方式 ...
del /f /q "%USERPROFILE%\Desktop\{app}.lnk" >nul 2>&1
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\{app}.lnk" >nul 2>&1
echo 正在清除注册表信息 ...
reg delete "{reg}" /f >nul 2>&1
echo 正在删除程序文件 ...
cd /d "%TEMP%"
start /min "" cmd /c "ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{target}" & exit"
echo 卸载完成。
"""


def write_uninstall_bat(target: Path) -> None:
    bat = target / UNINST_NAME
    bat.write_text(
        UNINSTALL_BAT.format(app=APP_NAME, reg=REG_PATH, target=str(target)),
        encoding="gbk", errors="replace")


def write_registry(target: Path, exe: Path, size_kb: int) -> None:
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH)
    try:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          str(target / UNINST_NAME))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{exe},0")
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    finally:
        winreg.CloseKey(key)


def install(target: Path, report) -> Path:
    """执行安装，report(msg, ratio) 用于回报进度。"""
    src = payload_dir()
    if not src.is_dir():
        raise RuntimeError("安装数据缺失：payload 目录不存在")

    report("准备安装目录…", 0.02)
    if target.exists():
        report("清理旧版本…", 0.05)
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    files = [p for p in src.rglob("*") if p.is_file()]
    total = max(len(files), 1)
    for i, f in enumerate(files, 1):
        dst = target / f.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        if i % 20 == 0 or i == total:
            report(f"正在复制文件 {i}/{total}", 0.05 + 0.80 * i / total)

    exe = target / EXE_NAME
    report("创建桌面快捷方式…", 0.87)
    make_shortcut(desktop_dir() / f"{APP_NAME}.lnk", exe, target)
    report("创建开始菜单快捷方式…", 0.91)
    make_shortcut(startmenu_dir() / f"{APP_NAME}.lnk", exe, target)

    report("写入卸载信息…", 0.95)
    size_kb = int(sum(os.path.getsize(target / f.relative_to(src))
                      for f in files) / 1024)
    write_uninstall_bat(target)
    try:
        write_registry(target, exe, size_kb)
    except Exception:
        pass

    report("安装完成", 1.0)
    return exe


# ---------------------------------------------------------------- 界面
class InstallerUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} 安装程序")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.target = default_target()
        self.exe_path: Path | None = None
        self.busy = False
        self.q: queue.Queue = queue.Queue()

        self._build()
        self._center(640, 480)
        self.after(80, self._drain)

    def _center(self, w: int, h: int) -> None:
        self.update_idletasks()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        # 顶部图形 + 标题
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=32, pady=(30, 8))

        cv = tk.Canvas(head, width=76, height=76, bg=BG, highlightthickness=0)
        cv.pack(side="left")
        cv.create_oval(0, 0, 76, 76, fill="#1B2545", outline="")
        for r, wd in ((16, 3), (26, 3), (36, 3)):
            cv.create_arc(10, 38 - r, 10 + 2 * r, 38 + r, start=-52, extent=104,
                          style="arc", outline="#7FB4FF", width=wd)
        cv.create_oval(12, 32, 20, 40, fill="#FFFFFF", outline="")

        box = tk.Frame(head, bg=BG)
        box.pack(side="left", padx=(14, 0))
        tk.Label(box, text=APP_NAME, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(box, text=f"蓝牙 NFC 读卡器桌面工具  ·  v{APP_VERSION}",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))

        # 安装位置卡片
        card = tk.Frame(self, bg=CARD)
        card.pack(fill="x", padx=32, pady=(18, 0))
        tk.Label(card, text="安装位置", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(14, 4))
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=16, pady=(0, 14))
        self.path_var = tk.StringVar(value=str(self.target))
        tk.Label(row, textvariable=self.path_var, bg=CARD, fg=TEXT, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", fill="x", expand=True)
        self.btn_pick = tk.Button(row, text="更改", command=self.pick_dir,
                                  bg="#22305A", fg=TEXT, relief="flat", bd=0,
                                  activebackground="#2C3E73", activeforeground=TEXT,
                                  font=("Microsoft YaHei UI", 9), padx=14, pady=4,
                                  cursor="hand2")
        self.btn_pick.pack(side="right")

        # 功能说明
        tips = tk.Frame(self, bg=BG)
        tips.pack(fill="x", padx=32, pady=(16, 0))
        for t in ("· 一键扫描连接读卡器，寻卡 / 读卡 / 写卡 / 复制 / 粘贴 / 破解全功能",
                  "· 破解双模式：默认云端解算，设置里可切本地离线 mfkey32，云端失效自动降级",
                  "· 液态玻璃界面，可更换背景壁纸",
                  "· 免管理员权限，装到当前用户目录，可随时卸载"):
            tk.Label(tips, text=t, bg=BG, fg=MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=1)

        # 进度
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", side="bottom", padx=32, pady=(0, 26))

        style = ttk.Style()
        try:
            style.theme_use("default")
        except Exception:
            pass
        style.configure("Glass.Horizontal.TProgressbar", troughcolor="#141B30",
                        background=ACCENT, bordercolor="#141B30",
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

        self.bar = ttk.Progressbar(bottom, style="Glass.Horizontal.TProgressbar",
                                   orient="horizontal", mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(0, 12))
        self.status = tk.Label(bottom, text="准备就绪，点击下方按钮开始安装",
                               bg=BG, fg=MUTED, anchor="w",
                               font=("Microsoft YaHei UI", 9))
        self.status.pack(anchor="w", pady=(0, 14))

        btns = tk.Frame(bottom, bg=BG)
        btns.pack(fill="x")
        self.btn_quit = tk.Button(btns, text="取消", command=self.destroy,
                                  bg="#1B2440", fg=MUTED, relief="flat", bd=0,
                                  activebackground="#243055", activeforeground=TEXT,
                                  font=("Microsoft YaHei UI", 10), width=10, pady=8,
                                  cursor="hand2")
        self.btn_quit.pack(side="right", padx=(10, 0))
        self.btn_launch = tk.Button(btns, text="立即启动", command=self.launch,
                                    bg="#14532D", fg="#DCFCE7", relief="flat", bd=0,
                                    activebackground="#166534", activeforeground="#DCFCE7",
                                    font=("Microsoft YaHei UI", 10), width=12, pady=8,
                                    cursor="hand2", state="disabled")
        self.btn_launch.pack(side="right", padx=(10, 0))
        self.btn_install = tk.Button(btns, text="一键安装", command=self.start,
                                     bg=ACCENT, fg="#FFFFFF", relief="flat", bd=0,
                                     activebackground="#2563EB", activeforeground="#FFFFFF",
                                     font=("Microsoft YaHei UI", 11, "bold"),
                                     width=14, pady=8, cursor="hand2")
        self.btn_install.pack(side="right")

    # ------------------------------------------------------------ 交互
    def pick_dir(self) -> None:
        if self.busy:
            return
        chosen = filedialog.askdirectory(title="选择安装位置", initialdir=str(self.target.parent))
        if chosen:
            self.target = Path(chosen) / "NFCMiniStudio"
            self.path_var.set(str(self.target))

    def start(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.btn_install.configure(state="disabled", text="安装中…")
        self.btn_pick.configure(state="disabled")
        self.btn_quit.configure(state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            exe = install(self.target, lambda m, r: self.q.put(("p", m, r)))
            self.q.put(("done", exe, 1.0))
        except Exception as exc:                        # noqa: BLE001
            self.q.put(("err", f"{type(exc).__name__}: {exc}", 0))

    def _drain(self) -> None:
        try:
            while True:
                kind, msg, ratio = self.q.get_nowait()
                if kind == "p":
                    self.status.configure(text=msg, fg=MUTED)
                    self.bar["value"] = max(0.0, min(100.0, ratio * 100))
                elif kind == "done":
                    self.exe_path = Path(msg)
                    self.bar["value"] = 100
                    self.status.configure(text=f"安装成功：{self.exe_path}", fg=OK_COLOR)
                    self.btn_install.configure(text="已安装", state="disabled")
                    self.btn_launch.configure(state="normal")
                    self.btn_quit.configure(state="normal", text="完成")
                elif kind == "err":
                    self.busy = False
                    self.status.configure(text=f"安装失败：{msg}", fg="#F87171")
                    self.btn_install.configure(state="normal", text="重试安装")
                    self.btn_pick.configure(state="normal")
                    self.btn_quit.configure(state="normal")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def launch(self) -> None:
        if not self.exe_path or not self.exe_path.exists():
            return
        try:
            subprocess.Popen([str(self.exe_path)], cwd=str(self.exe_path.parent),
                             creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        self.after(400, self.destroy)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--silent" in argv:
        # 静默安装：安装程序.exe --silent [--target D:\路径] [--launch]
        tgt = default_target()
        if "--target" in argv:
            idx = argv.index("--target")
            if idx + 1 < len(argv):
                tgt = Path(argv[idx + 1])
        try:
            exe_path = install(tgt, lambda m, r: None)
        except Exception:
            raise SystemExit(1)
        if "--launch" in argv:
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent),
                             creationflags=CREATE_NO_WINDOW)
        raise SystemExit(0)
    InstallerUI().mainloop()
