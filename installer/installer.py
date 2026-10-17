# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 一键安装程序
==============================
双击运行 → 点「一键安装」→ 装到 %LOCALAPPDATA%\\Programs\\NFCMiniStudio，
自动创建桌面 / 开始菜单快捷方式，并登记到「应用和功能」可卸载。
全程免管理员权限。
"""
from __future__ import annotations

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import winreg
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
except Exception:  # noqa: BLE001
    Image = ImageDraw = ImageFilter = ImageTk = None

# windowed（无控制台）模式下 stdout/stderr 为 None，兜底防止第三方库 print 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_NAME = "NFC Mini Studio"
APP_VERSION = "3.1.0"
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


def install(target: Path, report, desktop: bool = True, start_menu: bool = True) -> Path:
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
def _dpi_scale() -> float:
    """声明 DPI 感知（避免 Windows 位图拉伸导致界面发虚），并返回系统缩放比。"""
    if os.name != "nt":
        return 1.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


SCALE = _dpi_scale()
WINDOW_W = int(round(980 * SCALE))
WINDOW_H = int(round(660 * SCALE))


def _preview_asset(name: str) -> Path | None:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [base / "payload" / "_internal" / "app" / "web" / "assets" / name,
                       base / "payload" / "app" / "web" / "assets" / name]
    root = Path(__file__).resolve().parent.parent
    candidates += [root / "assets" / name, root / "app" / "web" / "assets" / name,
                   payload_dir() / "_internal" / "app" / "web" / "assets" / name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _cover_resize(path: Path, size: tuple[int, int], blur: float = 0.0) -> "Image.Image":
    """按 cover 方式精确裁成 size（保证排版对齐），可选高斯模糊。"""
    im = Image.open(path).convert("RGB")
    tw, th = size
    ratio = max(tw / im.width, th / im.height)
    im = im.resize((max(tw, int(round(im.width * ratio))),
                    max(th, int(round(im.height * ratio)))), Image.Resampling.LANCZOS)
    left = (im.width - tw) // 2
    top = max(0, (im.height - th) // 3)
    im = im.crop((left, top, left + tw, top + th))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return im


def _load_asset(name: str, size: tuple[int, int] | None = None, rounded: int = 0):
    """加载素材为 PhotoImage；给了 size 就精确裁切，rounded>0 再切圆角。"""
    if Image is None or ImageTk is None:
        return None
    path = _preview_asset(name)
    if not path:
        return None
    if size:
        size = (int(round(size[0] * SCALE)), int(round(size[1] * SCALE)))
    rounded = int(round(rounded * SCALE))
    try:
        im = _cover_resize(path, size).convert("RGBA") if size \
            else Image.open(path).convert("RGBA")
        if rounded:
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, im.width - 1, im.height - 1), radius=rounded, fill=255)
            im.putalpha(mask)
        return ImageTk.PhotoImage(im)
    except Exception:
        return None


def _glass_panel(base: "Image.Image", box, radius: int = 30, blur: float = 24.0,
                 lighten: float = 0.12, white: float = 0.09, fill: int = 232,
                 outline: tuple[int, int, int, int] = (255, 255, 255, 142)) -> None:
    """真正的液态玻璃：把背景对应区域重新模糊提亮后回贴，形成"透过玻璃看背景"的层次。"""
    from PIL import ImageEnhance
    x1, y1, x2, y2 = box
    # 玻璃板投影：让面板与背景分离
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x1 + 3, y1 + 12, x2 + 3, y2 + 18), radius=radius, fill=(0, 0, 0, 125))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
    # 透过玻璃看到的那层背景：更糊、更亮
    region = base.crop(box).convert("RGB").filter(ImageFilter.GaussianBlur(blur))
    region = Image.blend(region, Image.new("RGB", region.size, (255, 255, 255)), white)
    region = ImageEnhance.Brightness(region).enhance(1 + lighten)
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, region.width - 1, region.height - 1), radius=radius, fill=fill)
    base.paste(region, (x1, y1), mask)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle(box, radius=radius, outline=outline, width=1)
    # 玻璃厚度感：顶部两道高光 + 底部暗线
    d.line((x1 + radius, y1 + 1, x2 - radius, y1 + 1), fill=(255, 255, 255, 208), width=1)
    d.line((x1 + radius + 8, y1 + 3, x2 - radius - 8, y1 + 3), fill=(255, 255, 255, 96), width=1)
    d.line((x1 + radius, y2 - 1, x2 - radius, y2 - 1), fill=(0, 0, 0, 78), width=1)


def _make_background(width: int, height: int):
    """窗口底：模糊的软件界面 + 色彩柔光 + 多层玻璃面板。"""
    if Image is None:
        return None
    hero = _preview_asset("installer_shot_hero.png") or _preview_asset("installer_preview.png")
    if hero:
        # 底图只做轻微模糊：面板「比背景更糊」才有玻璃层次（真实背板模糊）
        base = _cover_resize(hero, (width, height), blur=13).convert("RGB")
        base = Image.blend(base, Image.new("RGB", base.size, (8, 10, 18)), 0.34)
    else:
        base = Image.new("RGB", (width, height), (10, 12, 22))

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for box, color in (
        ((-200, -180, 540, 470), (76, 126, 255, 120)),
        ((580, -240, 1200, 420), (177, 88, 255, 96)),
        ((360, 440, 1060, 940), (38, 185, 182, 78)),
    ):
        gd.ellipse(box, fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    base = Image.alpha_composite(base.convert("RGBA"), glow)

    def _b(x1, y1, x2, y2):
        return (int(round(x1 * SCALE)), int(round(y1 * SCALE)),
                int(round(x2 * SCALE)), int(round(y2 * SCALE)))

    _glass_panel(base, _b(28, 88, 452, 596), radius=int(32 * SCALE), blur=24 * SCALE,
                 lighten=0.18, white=0.15)
    _glass_panel(base, _b(468, 88, 952, 596), radius=int(32 * SCALE), blur=24 * SCALE,
                 lighten=0.18, white=0.15)
    _glass_panel(base, _b(48, 132, 432, 350), radius=int(18 * SCALE), blur=30 * SCALE,
                 lighten=0.26, white=0.22)
    _glass_panel(base, _b(488, 238, 936, 382), radius=int(20 * SCALE), blur=30 * SCALE,
                 lighten=0.24, white=0.20)
    _glass_panel(base, _b(488, 390, 936, 452), radius=int(18 * SCALE), blur=30 * SCALE,
                 lighten=0.24, white=0.20)
    return base.convert("RGB")


class InstallerUI(tk.Tk):
    WIDTH = WINDOW_W
    HEIGHT = WINDOW_H

    def __init__(self, shot_path: str | None = None) -> None:
        super().__init__()
        self.title(f"{APP_NAME} 安装程序")
        self.overrideredirect(True)
        self.resizable(False, False)
        self.configure(bg="#0a0b10")
        self.attributes("-alpha", 0.0)
        try:
            self.tk.call("tk", "scaling", 1.3334 * SCALE)
        except Exception:
            pass

        self.target = default_target()
        self.exe_path: Path | None = None
        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self.opt_desktop = True
        self.opt_startmenu = True
        self.opt_launch = False
        self._drag: tuple[int, int] | None = None
        self._images: list[object] = []
        self._shot_path = shot_path

        self._build()
        self._center()
        self._apply_round_corners()
        self.after(30, self._fade_in)
        self.after(80, self._drain)
        if shot_path:
            self.after(1500, self._auto_shot)

    # ------------------------------------------------ 窗口行为
    def _center(self) -> None:
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.WIDTH) // 2
        y = (self.winfo_screenheight() - self.HEIGHT) // 2
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _fade_in(self) -> None:
        try:
            a = float(self.attributes("-alpha"))
            a = min(0.99, a + 0.10)
            self.attributes("-alpha", a)
            if a < 0.99:
                self.after(18, self._fade_in)
        except Exception:
            self.attributes("-alpha", 1.0)

    def _apply_round_corners(self) -> None:
        if os.name != "nt":
            return
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def _auto_shot(self) -> None:
        try:
            self.attributes("-topmost", True)
            self.deiconify()
            self.lift()
            self.update_idletasks()
            self.update()
        except Exception:
            pass
        self.after(900, self._save_shot)

    def _save_shot(self) -> None:
        try:
            from PIL import ImageGrab
            self.attributes("-topmost", True)
            self.lift()
            self.focus_force()
            self.update_idletasks()
            self.update()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + self.winfo_width(), y + self.winfo_height())) \
                .save(self._shot_path)
            try:
                ImageGrab.grab().save(self._shot_path + ".full.png")
            except Exception:
                pass
            print("SHOT_OK", self._shot_path)
        except Exception as exc:  # noqa: BLE001
            print("SHOT_FAIL", exc)
        finally:
            self.after(200, self.destroy)

    @staticmethod
    def _rounded(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, splinesteps=36, **kwargs)

    def _button(self, name: str, x1, y1, x2, y2, text: str,
                fill: str, outline: str, text_fill: str = TEXT, radius: int = 14):
        self._rounded(self.cv, x1, y1, x2, y2, radius,
                      fill=fill, outline=outline, width=1, tags=(name,))
        self.cv.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=text,
                            fill=text_fill, font=("Microsoft YaHei UI", 10, "bold"),
                            tags=(name,))

    # ------------------------------------------------ 界面
    def _build(self) -> None:
        self.cv = tk.Canvas(self, width=self.WIDTH, height=self.HEIGHT, bd=0,
                            highlightthickness=0, bg="#0a0b10")
        self.cv.pack(fill="both", expand=True)

        bg = _make_background(self.WIDTH, self.HEIGHT)
        if bg is not None and ImageTk is not None:
            bg_img = ImageTk.PhotoImage(bg)
            self._images.append(bg_img)
            self.cv.create_image(0, 0, anchor="nw", image=bg_img)

        # ---- 自绘标题栏（可拖动）+ 无边框窗口控件
        self.cv.create_text(34, 30, anchor="w", text=APP_NAME, fill=TEXT,
                            font=("Microsoft YaHei UI", 18, "bold"))
        self.cv.create_text(34, 60, anchor="w",
                            text=f"蓝牙 NFC 读卡器桌面工具  ·  v{APP_VERSION}",
                            fill=MUTED, font=("Microsoft YaHei UI", 9))
        self.cv.create_text(612, 32, anchor="w", text="安装向导", fill=TEXT,
                            font=("Microsoft YaHei UI", 14, "bold"))
        self.cv.create_text(612, 58, anchor="w", text="免管理员权限 · 仅使用官方蓝牙 BLE 通信",
                            fill=MUTED, font=("Microsoft YaHei UI", 9))

        self._button("win_min", 884, 20, 914, 50, "—", "#1b2130", "#3a4355", MUTED, radius=12)
        self._button("win_close", 922, 20, 952, 50, "×", "#2a1d24", "#58313a", "#ffb8b2", radius=12)
        self.cv.tag_bind("win_min", "<Button-1>", lambda _e: self.iconify())
        self.cv.tag_bind("win_close", "<Button-1>", lambda _e: self.destroy())

        # ---- 左面板：软件介绍图片（界面截图三连）
        self.cv.create_text(54, 112, anchor="w", text="软件预览", fill=TEXT,
                            font=("Microsoft YaHei UI", 12, "bold"))
        hero = _load_asset("installer_shot_hero.png", (384, 218), rounded=16)
        if hero is not None:
            self._images.append(hero)
            self.cv.create_image(48, 132, anchor="nw", image=hero)
        else:
            self.cv.create_text(240, 240, text="界面截图将在安装包内展示", fill=MUTED,
                                font=("Microsoft YaHei UI", 11))

        for idx, (asset, label) in enumerate((
                ("installer_shot_device.png", "设备连接"),
                ("installer_shot_crack.png", "密钥破译"))):
            x = 48 + idx * 190
            shot = _load_asset(asset, (182, 102), rounded=12)
            if shot is not None:
                self._images.append(shot)
                self.cv.create_image(x, 364, anchor="nw", image=shot)
            self.cv.create_text(x + 91, 478, text=label, fill=MUTED,
                                font=("Microsoft YaHei UI", 8))

        self.cv.create_text(54, 496, anchor="w", text="软件能做什么", fill=TEXT,
                            font=("Microsoft YaHei UI", 12, "bold"))
        self.cv.create_text(54, 524, anchor="nw", width=348, justify="left",
                            text="· 一键扫描并连接 NFC Mini 2.0（BLE 蓝牙）\n"
                                 "· 寻卡 / 读卡 / 写卡 / 复制与卡夹管理\n"
                                 "· 密钥破解、进度日志与液态玻璃取色界面",
                            fill=MUTED, font=("Microsoft YaHei UI", 9))

        # ---- 右面板：安装选项
        self.cv.create_text(492, 112, anchor="w", text="安装选项", fill=TEXT,
                            font=("Microsoft YaHei UI", 12, "bold"))
        self._check_y = {"desktop": 152, "startmenu": 186, "launch": 220}
        self._check_tags = {}
        for key, label in (("desktop", "创建桌面快捷方式"),
                           ("startmenu", "创建开始菜单快捷方式"),
                           ("launch", "安装完成后立即启动")):
            tag = f"chk_{key}"
            self._check_tags[key] = tag
            self._rounded(self.cv, 500, self._check_y[key] - 11, 522, self._check_y[key] + 11,
                          5, fill="#26334e", outline="#6076a5", width=1, tags=(tag,))
            self.cv.create_text(511, self._check_y[key], text="✓", fill="#ffffff",
                                font=("Microsoft YaHei UI", 9, "bold"), tags=(tag,))
            self.cv.create_text(536, self._check_y[key], anchor="w", text=label,
                                fill=TEXT, font=("Microsoft YaHei UI", 9), tags=(tag,))
            self.cv.tag_bind(tag, "<Button-1>", lambda _e, k=key: self.toggle_option(k))

        # ---- 右面板：使用说明
        self.cv.create_text(502, 258, anchor="w", text="使用方法", fill=TEXT,
                            font=("Microsoft YaHei UI", 11, "bold"))
        self.cv.create_text(502, 282, anchor="nw", width=420, justify="left",
                            text="1  打开软件 → 顶部「扫描设备」→ 选择 NFC Mini 2.0 连接\n"
                                 "2  寻卡读写页：卡片贴近读卡器，一键寻卡后读取 / 写入 / 复制\n"
                                 "3  密钥破译页：选中卡片一键破译，进度与运行日志实时显示\n"
                                 "4  更换壁纸后界面与卡片自动取壁纸主色；卸载见「设置 → 应用」",
                            fill=MUTED, font=("Microsoft YaHei UI", 9))

        # ---- 右面板：安装位置
        self.cv.create_text(504, 421, anchor="w", text="安装到", fill=MUTED,
                            font=("Microsoft YaHei UI", 9))
        self.path_text = self.cv.create_text(552, 421, anchor="w", text=str(self.target),
                                             width=296, fill=TEXT,
                                             font=("Microsoft YaHei UI", 9))
        self._button("pick_dir", 868, 404, 928, 440, "更改", "#26334e", "#6076a5", TEXT, radius=12)
        self.cv.tag_bind("pick_dir", "<Button-1>", lambda _e: self.pick_dir())

        # ---- 右面板：进度与操作
        self.status_text = self.cv.create_text(492, 476, anchor="w",
                                               text="准备就绪，点击「立即安装」开始",
                                               fill=MUTED, font=("Microsoft YaHei UI", 9))
        self.cv.create_rectangle(492, 502, 940, 510, fill="#242b3d", outline="")
        self.cv.create_rectangle(492, 502, 492, 510, fill=ACCENT, outline="",
                                 tags=("progress_fill",))

        self._button("quit", 492, 536, 598, 578, "退出安装", "#1b2334", "#3b465e", MUTED)
        self._button("install", 610, 536, 940, 578, "立即安装", ACCENT, "#70a7ff", "#ffffff")
        self.cv.tag_bind("quit", "<Button-1>", lambda _e: self.destroy())
        self.cv.tag_bind("install", "<Button-1>", lambda _e: self.start())

        # 高 DPI：所有画布坐标与字体等比放大，保持设计比例
        if SCALE != 1.0:
            self.cv.scale("all", 0, 0, SCALE, SCALE)

        # 无边框窗口：仅标题栏区域可拖动
        self.cv.bind("<ButtonPress-1>", self._drag_start)
        self.cv.bind("<B1-Motion>", self._drag_move)

    # ------------------------------------------------ 交互
    def _draw_options(self) -> None:
        for key, tag in self._check_tags.items():
            enabled = bool(getattr(self, f"opt_{key}"))
            for item in self.cv.find_withtag(tag):
                if self.cv.type(item) == "polygon":
                    self.cv.itemconfigure(item, fill="#2f6fd8" if enabled else "#26334e")
                elif self.cv.type(item) == "text" and self.cv.itemcget(item, "text") == "✓":
                    self.cv.itemconfigure(item, fill="#ffffff" if enabled else "#4a5568")

    def toggle_option(self, key: str) -> None:
        if self.busy:
            return
        setattr(self, f"opt_{key}", not bool(getattr(self, f"opt_{key}")))
        self._draw_options()

    def _drag_start(self, event) -> None:
        if event.y <= 88 * SCALE:
            self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())
        else:
            self._drag = None

    def _drag_move(self, event) -> None:
        if self._drag:
            self.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def pick_dir(self) -> None:
        if self.busy:
            return
        chosen = filedialog.askdirectory(title="选择安装位置", initialdir=str(self.target.parent))
        if chosen:
            self.target = Path(chosen) / "NFCMiniStudio"
            self.cv.itemconfigure(self.path_text, text=str(self.target))

    def start(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.cv.itemconfigure("install", text="安装中…")
        self.cv.itemconfigure("quit", fill="#141a27", outline="#2c3447")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            exe = install(self.target, lambda m, r: self.q.put(("p", m, r)),
                          desktop=bool(self.opt_desktop),
                          start_menu=bool(self.opt_startmenu))
            self.q.put(("done", exe, 1.0))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("err", f"{type(exc).__name__}: {exc}", 0))

    def _drain(self) -> None:
        try:
            while True:
                kind, msg, ratio = self.q.get_nowait()
                if kind == "p":
                    self.cv.itemconfigure(self.status_text, text=msg, fill=MUTED)
                    self.cv.coords("progress_fill", 492 * SCALE, 502 * SCALE,
                                   (492 + 448 * max(0.0, min(1.0, ratio))) * SCALE, 510 * SCALE)
                elif kind == "done":
                    self.exe_path = Path(msg)
                    self.busy = False
                    self.cv.coords("progress_fill", 492 * SCALE, 502 * SCALE, 940 * SCALE, 510 * SCALE)
                    self.cv.itemconfigure(self.status_text, text=f"安装完成：{self.exe_path}",
                                          fill=OK_COLOR)
                    self.cv.itemconfigure("install", text="安装并启动" if self.opt_launch else "已完成")
                    self.cv.tag_unbind("install", "<Button-1>")
                    self.cv.tag_bind("install", "<Button-1>", lambda _e: self.launch())
                    if self.opt_launch:
                        self.after(350, self.launch)
                elif kind == "err":
                    self.busy = False
                    self.cv.itemconfigure(self.status_text, text=f"安装失败：{msg}", fill="#ff8a80")
                    self.cv.itemconfigure("install", text="重试安装")
                    self.cv.tag_unbind("install", "<Button-1>")
                    self.cv.tag_bind("install", "<Button-1>", lambda _e: self.start())
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

    def _opt(name: str) -> str | None:
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    if "--silent" in argv:
        # 静默安装：安装程序.exe --silent [--target D:\路径] [--launch]
        tgt = Path(_opt("--target") or default_target())
        try:
            exe_path = install(tgt, lambda m, r: None)
        except Exception:
            raise SystemExit(1)
        if "--launch" in argv:
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent),
                             creationflags=CREATE_NO_WINDOW)
        raise SystemExit(0)

    # --shot <png>：启动后自动截图自检（供构建验证用，不影响正常安装流程）
    InstallerUI(shot_path=_opt("--shot")).mainloop()
