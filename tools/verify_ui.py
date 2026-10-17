# -*- coding: utf-8 -*-
"""改完就自动验一遍：启动界面 → 机器人真点按钮 → 判定结果。

用法：
    python tools/verify_ui.py                     # 跑源码（python run.py --verify-ui）
    python tools/verify_ui.py --exe "C:\\path\\NFCMiniStudio.exe"
    python tools/verify_ui.py --python C:\\py\\python.exe

判定项（全部来自 ui_verify.json 的真实界面数据，不是"看起来应该没问题"）：
    - 自举：页面启动时 pywebview.api 是否已注入、核心接口是否齐全
    - 绑定：有没有按钮/滑杆没挂事件（点下去没反应的元凶）
    - 导航：必需导航项是否齐全、点击后是否真的切换视图
    - 扫描：点扫描后设备列表里是否真的出现了设备
    - 外观：点内置背景是否真的换掉背景、滑杆是否真的改得动 --alpha
    - 日志：有没有 err 级日志
退出码 0 = 全绿。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("APPDATA") or Path.home()) / "NFCMiniStudio"
REPORT = DATA / "ui_verify.json"
SETTINGS = DATA / "settings.json"
DEADLINE = 240          # 秒；扫描要 6s，留足余量

# 机器人会真的点"换壁纸 / 拖通透度滑杆"，这些都会落盘进 settings.json。
# 界面自己会在关窗前还原（app/main.py::_verify_cleanup），但外层脚本存在
# "读到报告就强杀进程"的路径，这里再兜一层：跑之前快照、跑完无条件还原。
APPEARANCE_KEYS = ("wallpaper", "preset", "glass", "opacity", "theme")

# 侧栏必须有的导航项（界面重排后按名称校验，比写死数量更抗改）
NAV_REQUIRED = {"设备", "寻卡读写", "卡夹", "密钥破解", "密码库",
                "外观", "设置", "常见问题"}


def snapshot_appearance() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return {k: data[k] for k in APPEARANCE_KEYS if k in data}
    except Exception:                              # noqa: BLE001
        return {}


def restore_appearance(snap: dict) -> None:
    if not snap:
        return
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        data.update(snap)
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass
    # 顺带清掉同步到前端目录的测试壁纸，别在仓库里留图
    try:
        (ROOT / "app" / "web" / "wallpapers" / "__verify_wall.png").unlink()
    except Exception:                              # noqa: BLE001
        pass


def report_ready(path: Path) -> bool:
    """报告文件已完整落盘（能解析出 steps 才算）。"""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(data.get("steps"))


def parse(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def find_report_steps(rep: dict) -> dict:
    out = {}
    for item in rep.get("steps", []):
        for k, v in item.items():
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="被测 exe 路径（打包版）")
    ap.add_argument("--python", help="python 解释器路径（源码版，默认当前解释器）")
    ap.add_argument("--keep-report", action="store_true", help="保留上一次报告")
    args = ap.parse_args()

    if REPORT.exists() and not args.keep_report:
        try:
            REPORT.unlink()
        except Exception:
            pass

    if args.exe:
        cmd = [str(args.exe), "--verify-ui"]
    else:
        cmd = [args.python or sys.executable, str(ROOT / "run.py"), "--verify-ui"]

    print("[verify] 启动：" + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    # 注意：不要用 PIPE 接 stdout —— 某些环境下 pywebview 会因此异常退出，
    # 改成落到日志文件，跑完再回读。
    log = ROOT / "tools" / "last_verify_run.log"
    snap = snapshot_appearance()
    fh = open(log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)

    t0 = time.time()
    rep = None
    exited = False
    while time.time() - t0 < DEADLINE:
        if report_ready(REPORT):
            rep = json.loads(REPORT.read_text(encoding="utf-8"))
            break
        if proc.poll() is not None:
            exited = True
            time.sleep(1.5)
            if report_ready(REPORT):
                rep = json.loads(REPORT.read_text(encoding="utf-8"))
                break
            break                                  # 真退了且没报告，别干等
        time.sleep(1.5)

    try:
        proc.wait(timeout=15)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    try:
        fh.close()
    except Exception:
        pass
    restore_appearance(snap)

    def tail(n=25):
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        if lines:
            print(f"\n--- 应用输出（末 {min(n, len(lines))} 行）---")
            for ln in lines[-n:]:
                print(ln[:300])

    if not rep:
        print("\n[FAIL] 没拿到 ui_verify.json —— 界面很可能压根没起来或中途崩了。")
        tail()
        print("       退回用 `python run.py --selftest` + `python run.py --debug` 看窗口内报错。")
        return 1

    steps = find_report_steps(rep)
    diag = parse(steps.get("final")) or {}
    boot = parse(steps.get("boot")) or diag.get("boot") or {}
    t0_types = parse(rep.get("t0_types")) or {}
    nav = parse(steps.get("nav_clicks")) or []
    scan = parse(steps.get("scan_result")) or {}
    wall_before = parse(steps.get("wall_before")) or {}
    wall_after = parse(steps.get("wall_after")) or {}
    alpha = parse(steps.get("alpha_slider")) or {}
    drawer = parse(steps.get("log_drawer")) or {}
    wall_custom = parse(steps.get("wall_custom_after")) or {}
    conn_tabs = parse(steps.get("conn_tabs")) or []

    checks: list[tuple[bool, str, str]] = []

    def ck(ok: bool, name: str, detail: str = ""):
        checks.append((bool(ok), name, detail))

    ck(isinstance(boot, dict) and boot.get("ready"),
       "页面自举成功", f"apiKeys={boot.get('apiKeys')} ready={boot.get('ready')}")
    ck(not diag.get("apiMissingCore"),
       "核心接口齐全", f"缺失={diag.get('apiMissingCore') or '无'}")
    ck(diag.get("unboundCount") == 0,
       "所有按钮/滑杆都已绑定", f"未绑定={diag.get('unbound') or '无'}")
    nav_titles = set(diag.get("navTitles") or [])
    missing_nav = sorted(NAV_REQUIRED - nav_titles)
    ck(not missing_nav, "导航项齐全",
       f"共 {diag.get('navCount')} 项，缺 {missing_nav or '无'}")
    ck(bool(nav) and all(str(x).endswith(":true") for x in nav),
       "点击导航真的切换视图", "; ".join(map(str, nav)))
    ck(scan.get("items", 0) >= 1 and "失败" not in str(scan.get("badge"))
       and bool(scan.get("first")),
       "扫描真的有设备", f"列表 {scan.get('items')} 项，角标「{scan.get('badge')}」，"
                          f"首个 {scan.get('first')}")
    ck(wall_after.get("sel") == 1 and wall_after.get("preset"),
       "内置背景可切换且持久化",
       f"选中 {wall_after.get('sel')} 个，preset={wall_after.get('preset')!r}")
    ck(wall_after.get("bg") != wall_before.get("bg"),
       "背景真的变了", f"{wall_before.get('bg')} → {wall_after.get('bg')}")
    ck(alpha.get("to") == "0.24",
       "通透度滑杆实时生效",
       f"--alpha {alpha.get('from')} → {alpha.get('to')}（已还原为 {alpha.get('restored')}）")
    ck(drawer.get("before") is False and drawer.get("open") is True
       and drawer.get("closed") is False,
       "日志抽屉开合正常", json.dumps(drawer, ensure_ascii=False))
    ck(not diag.get("errLogs"),
       "界面无错误日志", (diag.get("errLogs") or ["无"])[0] if diag.get("errLogs") else "无")
    _a = boot.get("alpha") if isinstance(boot, dict) else None
    ck(_a and float(_a) <= 0.3,
       "启动即通透（玻璃不是糊的）",
       f"启动 --alpha={_a} --blur={boot.get('blur')} 背景图={boot.get('bgImage')}")
    _b = boot.get("blur") if isinstance(boot, dict) else None
    ck(bool(_b) and float(str(_b).rstrip("px")) <= 40,
       "模糊值够小（清透折射而非磨砂）", f"启动 --blur={_b}")
    ck(wall_custom.get("hasImage") and "url(" in (wall_custom.get("url") or ""),
       "自定义壁纸能换上", f"has-image={wall_custom.get('hasImage')} "
                          f"selFile={wall_custom.get('selFile')!r}")
    ck(wall_custom.get("sel") == 1,
       "自定义壁纸选中态唯一", f"选中 {wall_custom.get('sel')} 个")

    # 三通道：每一项都必须 shown 是自己、active 恰好 1 个
    tab_ok = bool(conn_tabs)
    tab_detail = []
    for line in conn_tabs or []:
        txt = str(line)
        if ":no-btn" in txt:
            tab_ok = False
            tab_detail.append(txt)
            continue
        name, _, payload = txt.partition(":")
        try:
            obj = json.loads(payload)
        except Exception:
            tab_ok = False
            tab_detail.append(txt)
            continue
        ok = obj.get("shown") is True and obj.get("active") == 1
        tab_ok = tab_ok and ok
        tab_detail.append(f"{name}={'OK' if ok else 'FAIL'}")
    ck(tab_ok, "三通道切换（蓝牙/通用读卡器/手机读卡器）",
       " ".join(tab_detail) or "无数据")

    if args.exe:
        root = Path(args.exe).resolve().parent
        longest = max((p for p in root.rglob("*.pyd")), key=lambda p: len(str(p)),
                      default=None)
        n = len(str(longest)) if longest else 0
        ck(n and n < 250, "程序目录深度安全（蓝牙模块能加载）",
           f"最长 pyd 路径 {n} 字符：{longest.name if longest else '未找到'}")

    print("\n" + "=" * 70)
    print(f"NFC Mini Studio · 界面实机验证（{'打包版' if args.exe else '源码版'}）")
    print("=" * 70)
    print(f"注入时刻 t0：{rep.get('t0_types')}")
    print(f"自举快照   ：ready={boot.get('ready')} apiKeys={boot.get('apiKeys')} "
          f"boot.at={boot.get('at')}")
    print(f"接口快照   ：{diag.get('apiKeys')} 个（非函数：{diag.get('apiNonFn') or '无'}）")
    print(f"扫描结果   ：{scan}")
    print(f"背景切换   ：{wall_before.get('bg')} → {wall_after.get('bg')}")
    print(f"最近日志   ：{diag.get('logs')}")
    print("-" * 70)
    bad = 0
    for ok, name, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
        bad += 0 if ok else 1
    print("-" * 70)
    print(f"结果：{len(checks) - bad} 通过 / {bad} 失败")
    print(f"原始报告：{REPORT}")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
