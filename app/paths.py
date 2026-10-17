# -*- coding: utf-8 -*-
"""路径解析：兼容源码运行与 PyInstaller 打包运行。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "NFCMiniStudio"
APP_TITLE = "NFC Mini Studio"
APP_VERSION = "3.2.9"

# 仅作参考记录：项目当前位于 E:\我的项目\NFC读卡工具NFCMiniStudio
# （2026-10-09 从 E:\NFCMiniStudio 整理而来，2026-10-11 改为此中文名）。
# 运行期一律用 project_root() 动态解析，不依赖这个常量，目录再搬家也不会失效。
DEFAULT_PROJECT_DIR = r"E:\我的项目\NFC读卡工具NFCMiniStudio"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """源码模式返回项目根，打包模式返回解包目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def web_dir() -> Path:
    return project_root() / "app" / "web"


def web_assets_dir() -> Path:
    """前端可直接用相对路径访问的素材目录（设备渲染图等）。"""
    d = web_dir() / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """用户数据目录 %APPDATA%\\NFCMiniStudio"""
    base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def wallpapers_dir() -> Path:
    d = data_dir() / "wallpapers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_file() -> Path:
    return data_dir() / "settings.json"


def cards_file() -> Path:
    return data_dir() / "cards.json"


def crack_cache_file() -> Path:
    """破解结果缓存：按卡片 UID 记住已恢复的密钥，二次插入免重复破解。"""
    return data_dir() / "crack_cache.json"


IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
# 设备素材除位图外还允许矢量图（device_front_view.svg 等）：体积小、任意缩放都清晰
DEVICE_SUFFIX = IMG_SUFFIX | {".svg"}


def web_wallpapers_dir() -> Path:
    """前端页面可直接用相对路径访问的壁纸目录。"""
    d = web_dir() / "wallpapers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sync_wallpapers() -> int:
    """把用户壁纸同步进前端目录，返回本次复制的文件数。

    启动时和每次更换壁纸后都要调用：否则新选的图片只落在 %APPDATA%，
    前端按 `wallpapers/xxx` 相对路径取图会 404，表现就是"换了壁纸没反应"。
    """
    import shutil

    src_dir = wallpapers_dir()
    dst_dir = web_wallpapers_dir()
    copied = 0
    for f in src_dir.iterdir():
        if f.suffix.lower() not in IMG_SUFFIX:
            continue
        target = dst_dir / f.name
        try:
            if not target.exists() or target.stat().st_mtime < f.stat().st_mtime:
                shutil.copy2(f, target)
                copied += 1
        except Exception:
            pass
    return copied


def sync_device_assets() -> int:
    """把项目 assets/ 里的设备素材同步到前端目录，返回复制数量。

    设备页要展示读卡器外观，素材以相对路径 `assets/xxx` 被前端引用；
    所以必须落在 app/web/assets/ 下面（打包时 app/web 整体进包）。
    """
    import shutil

    src_dir = project_root() / "assets"
    dst_dir = web_assets_dir()
    copied = 0
    if not src_dir.exists():
        return 0
    for f in src_dir.iterdir():
        # 用 DEVICE_SUFFIX（含 .svg）：设备渲染图是矢量的 device_front_view.svg，
        # 若按位图后缀过滤会漏掉它，前端 `assets/device_front_view.svg` 直接 404 → 正面变纯白块
        if f.suffix.lower() not in DEVICE_SUFFIX:
            continue
        target = dst_dir / f.name
        try:
            if not target.exists() or target.stat().st_mtime < f.stat().st_mtime:
                shutil.copy2(f, target)
                copied += 1
        except Exception:
            pass
    return copied
