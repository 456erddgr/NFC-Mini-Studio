# -*- coding: utf-8 -*-
"""
NFC 手机端 APK · 一键构建脚本
=============================
工程里有**两个** App，共用一套核心代码：

    android/
      shared/java/net/nfcreader/core/   ← 共享核心（协议、NFC 引擎、TCP 服务、卡片操作、玻璃 UI）
      app/                              ← NFC 手机读卡器（把手机当电脑的读卡器）
      studio/                           ← NFC 完整工具箱（脱离电脑，手机自己读/写/破解/卡夹）

用法：
    python build_android.py              # 两个 App 都构建，输出 android/NFCMiniReader.apk + android/NFCMiniStudio.apk
    python build_android.py --module app   # 只构建读卡器
    python build_android.py --module studio # 只构建完整工具箱
    python build_android.py --debug        # 构建 debug APK
    python build_android.py --install      # 构建后顺手 adb install -r 到已连接手机
    python build_android.py --no-clean     # 保留上次构建缓存（更快，源码仍会重新同步）

前提：
    - 已安装 Android SDK（默认 C:\\Android\\Sdk，可用 ANDROID_HOME 覆盖）
    - 已安装 Gradle 8.7+（默认 C:\\Android\\gradle\\gradle-8.7，可用 GRADLE_HOME 覆盖）
    - 已安装 JDK 17（可用 JAVA_HOME 覆盖）
    - 本机 UTF-8/ASCII 路径限制：AGP 8.x 对含中文的工程路径会直接报
      "Your project path contains non-ASCII characters"，脚本会自动把工程
      复制到 C:\\nfcmreader-build（纯 ASCII）后再构建。

为什么要在 ASCII 目录构建：
    本项目位于 E:\\我的项目\\...（含中文），AGP 8.x 在 Windows 上遇到非 ASCII
    路径会直接失败。脚本因此先把 android/ 源码树镜像到 ASCII 目录构建，
    再把产物复制回 android/ 下的两个 APK。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "android"

# 模块 -> 输出 APK 文件名
MODULES = {
    "app": ("NFC 手机读卡器", SRC / "NFCMiniReader.apk"),
    "studio": ("NFC 完整工具箱", SRC / "NFCMiniStudio.apk"),
}


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
        r"C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot",
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
    """挑一个纯 ASCII 的构建目录（用户名可能是中文）。"""
    for c in (Path(r"C:\nfcmreader-build"), Path(tempfile.gettempdir()) / "nfcmreader-build"):
        try:
            c = c.resolve()
            str(c).encode("ascii")
        except (OSError, UnicodeEncodeError):
            continue
        return c
    return Path(tempfile.gettempdir()) / "nfcmreader-build"


def mirror_to_ascii_tree(src: Path, keep_build: bool) -> Path:
    """把 android/ 源码树镜像到纯 ASCII 目录。

    keep_build=True 时保留上一次的 build/ 与 .gradle/（增量构建，十几秒出包），
    但源码每次都会重新同步，避免改了代码还跑旧字节码。
    """
    tmp_root = ascii_build_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    if not keep_build:
        shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.mkdir(parents=True, exist_ok=True)

    def ignore(_dir, names):
        skip = {"build", ".gradle", "local.properties", "dist"}
        return [n for n in names if n in skip or n.endswith(".apk")]

    for item in src.iterdir():
        if item.name in {".gradle", "build", "dist"} or item.name.endswith(".apk"):
            continue
        target = tmp_root / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(item, target, ignore=ignore)
        else:
            shutil.copy2(item, target)
    return tmp_root


def run(cmd: list[str], cwd: Path, tag: str, env: dict | None = None, tail: int = 60) -> int:
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
    out = (proc.stdout or "") + (proc.stderr or "")
    log = ROOT / "android_build.log"
    log.write_text(out, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print("\n".join(out.splitlines()[-tail:]))
        print(f"[{tag}] 失败，完整日志：{log}")
    return proc.returncode


def main() -> int:
    debug = "--debug" in sys.argv
    do_install = "--install" in sys.argv
    no_clean = "--no-clean" in sys.argv
    only = None
    for i, a in enumerate(sys.argv):
        if a == "--module" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
    targets = [only] if only else list(MODULES)
    for t in targets:
        if t not in MODULES:
            raise SystemExit(f"未知模块：{t}（可选 app / studio）")

    if not SRC.is_dir():
        raise SystemExit(f"缺少 Android 工程目录：{SRC}")

    sdk, jdk, gradle = find_sdk(), find_jdk(), find_gradle()
    print(f"SDK    : {sdk}")
    print(f"JDK    : {jdk}")
    print(f"Gradle : {gradle}")

    build_root = mirror_to_ascii_tree(SRC, keep_build=no_clean)
    print(f"构建目录: {build_root}")

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
    # 让 javac/gradle 用英文报错、UTF-8 输出，日志才不会变成乱码
    env["JAVA_TOOL_OPTIONS"] = (
        "-Duser.language=en -Duser.country=US -Dfile.encoding=UTF-8 -Dclient.encoding.override=UTF-8"
    )

    kind = "debug" if debug else "release"
    tasks = [f":{t}:assemble{'Debug' if debug else 'Release'}" for t in targets]
    args: list[str] = [str(gradle)] + tasks + ["--console=plain"]
    if not no_clean:
        args.append("--no-daemon")

    rc = run(args, build_root, "android", env=env, tail=120)
    if rc != 0:
        print("[android] 构建失败")
        return rc

    ok = 0
    for mod in targets:
        label, out_apk = MODULES[mod]
        apk_dir = build_root / mod / "build" / "outputs" / "apk" / kind
        apk_src = apk_dir / f"{mod}-{kind}.apk"
        if not apk_src.is_file():
            found = list(apk_dir.rglob("*.apk")) if apk_dir.is_dir() else []
            if not found:
                print(f"[{mod}] 找不到 APK 产物")
                continue
            apk_src = found[0]
        shutil.copy2(apk_src, out_apk)
        size_mb = out_apk.stat().st_size / (1024 * 1024)
        print(f"[{mod}] {label} → {out_apk}（{size_mb:.2f} MB）")
        ok += 1

    if do_install:
        adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if adb.is_file():
            for mod in targets:
                _, out_apk = MODULES[mod]
                if out_apk.is_file():
                    rc = run([str(adb), "install", "-r", str(out_apk)], ROOT, "adb")
                    print(f"[adb] {mod} 安装{'完成' if rc == 0 else '失败'}")
        else:
            print("[adb] 未找到 adb，跳过安装")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
