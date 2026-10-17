# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 一键构建脚本
==============================
用法：
    python build_all.py            # 全量：主程序 → payload → 安装程序 → APK
    python build_all.py app        # 只构建主程序（dist/NFC Mini Studio/）
    python build_all.py payload    # 只把主程序产物同步为安装包数据
    python build_all.py installer  # 只构建安装程序（需先有 installer/payload）
    python build_all.py android    # 只构建 Android APK（需 JDK17 + SDK34 + Gradle 8.7）
    python build_all.py --no-android  # 全量但跳过 APK

产物：
    dist/NFC Mini Studio/NFC Mini Studio.exe           绿色版主程序
    installer/dist/NFC Mini Studio 安装程序.exe        一键安装包
    android/NFCMiniReader.apk                          手机读卡器 APK
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "NFC Mini Studio"
SETUP_NAME = "NFC Mini Studio 安装程序"
ICON = ROOT / "assets" / "app.ico"
TMP = ROOT / "build_tmp"
APK_OUT = ROOT / "android" / "NFCMiniReader.apk"


def run(cmd: list[str], tag: str, cwd: Path | None = None) -> int:
    print(f"[{tag}] {' '.join(cmd[:6])} …")
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
    return proc.returncode


def build_app() -> int:
    """主程序：onedir，启动快。"""
    rc = run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--name", APP_NAME,
        "--icon", str(ICON),
        "--add-data", f"{ROOT / 'app' / 'web'};app/web",
        "--collect-all", "webview",
        "--collect-all", "bleak",
        "--collect-all", "numpy",
        "--collect-all", "numba",
        "--collect-all", "llvmlite",
        "--hidden-import", "tkinter",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(TMP / "app"),
        "--specpath", str(TMP),
        "--log-level", "WARN",
        str(ROOT / "run.py"),
    ], "app")
    print("[app] 完成" if rc == 0 else "[app] 失败")
    return rc


def sync_payload() -> int:
    """把主程序产物同步为安装包数据。"""
    src = ROOT / "dist" / APP_NAME
    dst = ROOT / "installer" / "payload"
    if not src.is_dir():
        print("[payload] 缺少 dist 产物，请先执行 app 构建")
        return 1
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    print(f"[payload] 已同步 {sum(1 for _ in dst.rglob('*') if _.is_file())} 个文件")
    return 0


def build_installer() -> int:
    """安装程序：onefile，内嵌 payload。"""
    payload = ROOT / "installer" / "payload"
    if not payload.is_dir():
        print("[installer] 缺少 payload，请先执行 app 构建与同步")
        return 1
    rc = run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", SETUP_NAME,
        "--icon", str(ICON),
        "--add-data", f"{payload};payload",
        "--distpath", str(ROOT / "installer" / "dist"),
        "--workpath", str(TMP / "installer"),
        "--specpath", str(TMP),
        "--log-level", "WARN",
        str(ROOT / "installer" / "installer.py"),
    ], "installer")
    print("[installer] 完成" if rc == 0 else "[installer] 失败")
    return rc


def build_android() -> int:
    """Android APK：调用 build_android.py（内部自动镜像到 ASCII 路径）。"""
    script = ROOT / "build_android.py"
    if not script.is_file():
        print("[android] 缺少 build_android.py")
        return 1
    rc = run([sys.executable, str(script)], "android")
    if rc == 0 and APK_OUT.is_file():
        size = APK_OUT.stat().st_size / 1024 / 1024
        print(f"[android] 完成  →  {APK_OUT.name}  ({size:.2f} MB)")
    else:
        print("[android] 失败")
    return rc


def _has_flag(flag: str) -> bool:
    return flag in sys.argv[1:]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    step = args[0] if args else ("all" if not _has_flag("--no-android") else "desktop")

    if step in ("all", "desktop", "app"):
        if build_app() != 0:
            return 1
    if step in ("all", "desktop", "app", "payload"):
        if sync_payload() != 0:
            return 1
    if step in ("all", "desktop", "installer"):
        if build_installer() != 0:
            return 1
    if step in ("all", "android") and not _has_flag("--no-android"):
        if build_android() != 0:
            return 1

    print("\n全部完成")
    print(f"  绿色版      dist/{APP_NAME}/{APP_NAME}.exe")
    print(f"  安装程序    installer/dist/{SETUP_NAME}.exe")
    if APK_OUT.is_file():
        print(f"  手机 APK    android/{APK_OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
