# -*- coding: utf-8 -*-
"""
NFC 手机读卡器 APK · 一键构建脚本
================================
用法：
    python build_android.py              # 构建 release APK，输出到 android/NFCMiniReader.apk
    python build_android.py --debug      # 构建 debug APK
    python build_android.py --install    # 构建后顺手 adb install 到已连接手机
    python build_android.py --no-clean   # 保留上次构建缓存（更快）

前提：
    - 已安装 Android SDK（默认 C:\\Android\\Sdk，可用 ANDROID_HOME 覆盖）
    - 已安装 Gradle 8.7+（默认 C:\\Android\\gradle\\gradle-8.7）
    - 已安装 JDK 17（可用 JAVA_HOME 覆盖）
    - 本机 UTF-8/ASCII 路径限制：Gradle 对含中文的路径会报
      "Your project path contains non-ASCII characters"，脚本会自动把工程
      复制到 %TEMP%\\nfcmreader-build（纯 ASCII）后再构建。

为什么要在临时目录构建：
    Android Gradle Plugin 8.x 在 Windows 上遇到非 ASCII 路径会直接失败，
    而本项目位于 E:\\我的项目\\...（含中文）。脚本因此先把 android/ 源码树
    镜像到一个 ASCII 临时目录构建，再把 APK 复制回 android/NFCMiniReader.apk。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "android"
OUT_APK = SRC / "NFCMiniReader.apk"


def env_path(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def find_gradle() -> Path:
    cand = [
        env_path("GRADLE_HOME", ""),
        r"C:\Android\gradle\gradle-8.7",
        r"C:\Android\gradle\gradle-8.6",
        r"C:\gradle\gradle-8.7",
    ]
    for c in cand:
        if not c:
            continue
        p = Path(c) / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
        if p.is_file():
            return p
    # 回落到 PATH 里的 gradle
    found = shutil.which("gradle")
    if found:
        return Path(found)
    raise SystemExit(
        "找不到 Gradle。请安装 Gradle 8.7+ 并设置 GRADLE_HOME，"
        "或放到 C:\\Android\\gradle\\gradle-8.7"
    )


def find_jdk() -> Path:
    cand = [
        env_path("JAVA_HOME", ""),
        r"C:\Program Files\Microsoft\jdk-17.0.19.10-hotspot",
        r"C:\Program Files\Java\jdk-17",
        r"C:\Android\jdk-17",
    ]
    for c in cand:
        if c and (Path(c) / "bin" / "javac.exe").is_file():
            return Path(c)
    raise SystemExit("找不到 JDK 17。请设置 JAVA_HOME 指向 JDK 17。")


def find_sdk() -> Path:
    cand = [
        env_path("ANDROID_HOME", ""),
        env_path("ANDROID_SDK_ROOT", ""),
        r"C:\Android\Sdk",
    ]
    for c in cand:
        if c and (Path(c) / "platforms").is_dir():
            return Path(c)
    raise SystemExit("找不到 Android SDK。请设置 ANDROID_HOME（需含 platforms/android-34）。")


def ascii_build_root() -> Path:
    """挑一个纯 ASCII 的构建目录。

    Windows 用户名可能是中文（例如 C:\\Users\\刘子墨\\AppData\\Local\\Temp），
    而 AGP 8.x 对非 ASCII 工程路径会直接报错，所以优先用 C:\\nfcmreader-build。
    """
    cands = [Path(r"C:\nfcmreader-build"), Path(tempfile.gettempdir()) / "nfcmreader-build"]
    for c in cands:
        try:
            c = c.resolve()
            str(c).encode("ascii")
        except (OSError, UnicodeEncodeError):
            continue
        return c
    return Path(tempfile.gettempdir()) / "nfcmreader-build"


def mirror_to_ascii_tree(src: Path) -> Path:
    """把 android/ 源码树镜像到纯 ASCII 目录。"""
    tmp_root = ascii_build_root()
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    def ignore(_dir, names):
        skip = {"build", ".gradle", "local.properties"}
        return [n for n in names if n in skip]

    for item in src.iterdir():
        if item.name in {"NFCMiniReader.apk", "build", ".gradle"}:
            continue
        target = tmp_root / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore)
        else:
            shutil.copy2(item, target)
    return tmp_root


def run(cmd: list[str], cwd: Path, tag: str, env: dict | None = None) -> int:
    print(f"[{tag}] {' '.join(str(c) for c in cmd[:6])} …")
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if proc.returncode != 0:
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
    return proc.returncode


def main() -> int:
    debug = "--debug" in sys.argv
    do_install = "--install" in sys.argv
    no_clean = "--no-clean" in sys.argv

    if not SRC.is_dir():
        raise SystemExit(f"缺少 Android 工程目录：{SRC}")

    sdk = find_sdk()
    jdk = find_jdk()
    gradle = find_gradle()
    print(f"SDK    : {sdk}")
    print(f"JDK    : {jdk}")
    print(f"Gradle : {gradle}")

    build_root = mirror_to_ascii_tree(SRC)
    print(f"构建目录: {build_root}")

    # 每次写入 local.properties（指向真实 SDK）
    (build_root / "local.properties").write_text(
        "sdk.dir=" + str(sdk).replace("\\", "\\\\") + "\n", encoding="utf-8"
    )
    props = build_root / "gradle.properties"
    text = props.read_text(encoding="utf-8") if props.is_file() else ""
    if "android.overridePathCheck" not in text:
        text += "\nandroid.overridePathCheck=true\n"
        props.write_text(text, encoding="utf-8")

    env = os.environ.copy()
    env["JAVA_HOME"] = str(jdk)
    env["ANDROID_HOME"] = str(sdk)
    env["ANDROID_SDK_ROOT"] = str(sdk)

    task = "assembleDebug" if debug else "assembleRelease"
    args: list[str] = [str(gradle), task, "--console=plain"]
    if not no_clean:
        args.append("--no-daemon")

    rc = run(args, build_root, "android", env=env)
    if rc != 0:
        print("[android] 构建失败")
        return rc

    kind = "debug" if debug else "release"
    apk_src = build_root / "app" / "build" / "outputs" / "apk" / kind / f"app-{kind}.apk"
    if not apk_src.is_file():
        # 有些 AGP 版本产物名不同，兜底搜索
        found = list((build_root / "app" / "build" / "outputs" / "apk").rglob("*.apk"))
        if not found:
            print("[android] 找不到 APK 产物")
            return 1
        apk_src = found[0]

    shutil.copy2(apk_src, OUT_APK)
    size_mb = OUT_APK.stat().st_size / (1024 * 1024)
    print(f"[android] 完成：{OUT_APK}（{size_mb:.2f} MB）")

    if do_install:
        adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if adb.is_file():
            rc = run([str(adb), "install", "-r", str(OUT_APK)], ROOT, "adb")
            print("[adb] 安装完成" if rc == 0 else "[adb] 安装失败")
        else:
            print("[adb] 未找到 adb，跳过安装")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
