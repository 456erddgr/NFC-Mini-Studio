# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 一键构建脚本
==============================
用法：
    python build_all.py            # 全量：主程序 → payload → 安装程序
    python build_all.py app        # 只构建主程序（dist/NFC Mini Studio/）
    python build_all.py installer  # 只构建安装程序（需先有 installer/payload）
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


def run(cmd: list[str], tag: str) -> int:
    print(f"[{tag}] {' '.join(cmd[:6])} …")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
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


def main() -> int:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("all", "app"):
        if build_app() != 0:
            return 1
    if step in ("all", "app", "payload"):
        if sync_payload() != 0:
            return 1
    if step in ("all", "installer"):
        if build_installer() != 0:
            return 1
    print("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
