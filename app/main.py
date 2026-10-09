# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 程序入口
==========================
Python(bleak) 后端 + WebView2 前端（pywebview）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import webview

# 允许 `python -m app.main` 与打包后直接运行两种方式
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.api import Api
    from app.paths import (APP_TITLE, data_dir, sync_device_assets, sync_wallpapers,
                           wallpapers_dir, web_dir, web_wallpapers_dir)
else:
    from .api import Api
    from .paths import (APP_TITLE, data_dir, sync_device_assets, sync_wallpapers,
                        wallpapers_dir, web_dir, web_wallpapers_dir)

# 自检专用的假壁纸（1x1 纯色 PNG）：验证「自定义壁纸」链路，跑完就删
VERIFY_WALLPAPER = "__verify_wall.png"
_VERIFY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01"
    b"\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
_verify_snapshot = None


def _verify_prepare(api) -> None:
    """自检前：备份观感设置，塞一张测试壁纸进用户壁纸目录并同步到前端目录。"""
    global _verify_snapshot
    _verify_snapshot = dict(api.settings.data)
    try:
        wallpapers_dir().mkdir(parents=True, exist_ok=True)
        (wallpapers_dir() / VERIFY_WALLPAPER).write_bytes(_VERIFY_PNG)
        sync_wallpapers()
    except Exception as exc:                       # noqa: BLE001
        print(f"[verify-ui] 准备测试壁纸失败：{exc}", flush=True)


def _verify_cleanup(api) -> None:
    """自检后：删掉测试壁纸，把设置恢复成自检前的样子。"""
    try:
        (wallpapers_dir() / VERIFY_WALLPAPER).unlink()
    except Exception:
        pass
    # 同步到前端目录的那份也要删：否则每次跑 verify_ui 都会在仓库里留一张图
    try:
        (web_wallpapers_dir() / VERIFY_WALLPAPER).unlink()
    except Exception:
        pass
    if _verify_snapshot:
        try:
            api.settings.update({k: v for k, v in _verify_snapshot.items()
                                 if k in ("wallpaper", "preset", "glass", "opacity", "theme")})
            print("[verify-ui] 已还原自检前的观感设置", flush=True)
        except Exception as exc:                   # noqa: BLE001
            print(f"[verify-ui] 还原设置失败：{exc}", flush=True)


def _verify_ui(window, api) -> None:
    """--verify-ui：开窗 → 真点按钮 → 读界面状态 → 落盘报告 → 自动关窗。

    用途是「改完之后自动检查一遍」：不需要人盯屏幕，用机器人真的去点，
    证明按钮能点动、后端真被调到、扫描真能出设备、壁纸/玻璃真会变。
    结果落盘到 %APPDATA%\\NFCMiniStudio\\ui_verify.json，再由 tools/verify_ui.py 判定。
    """
    import json
    import time

    def ev(js: str):
        try:
            return window.evaluate_js(js)
        except Exception as exc:                   # noqa: BLE001
            return f"__ERR__ {exc}"

    def diag():
        return ev("JSON.stringify(window.__diag ? window.__diag() : null)")

    report: dict = {"title": APP_TITLE, "steps": []}

    def step(name: str, value):
        report["steps"].append({name: value})
        print(f"[verify-ui] {name}: {value}", flush=True)

    # 0) 窗口刚建好就问一次：看注入有没有延迟（按钮失灵的老毛病就出在这段空窗期）
    report["t0_types"] = ev(
        "JSON.stringify({pv: typeof window.pywebview, api: typeof (window.pywebview &&"
        " window.pywebview.api), boot: window.__boot || null})")

    time.sleep(5)                                  # 给页面足够时间自举
    step("boot", diag())

    # 1) 导航：逐个点，必须真的切视图
    step("nav_clicks", ev(
        "JSON.stringify(['device','card','box','crack','keybox','appearance','settings','help'].map(v => {"
        " const n = document.querySelector('[data-view=\"'+v+'\"]');"
        " if (!n) return v + ':no-nav'; n.click();"
        " const el = document.getElementById('view-' + v);"
        " return v + ':' + (el && el.classList.contains('is-active')); }))"))

    # 2) 扫描：点按钮 → 等 → 列表里必须真有设备
    step("click_scan", ev(
        "(() => { const b = document.getElementById('btnScan');"
        " if (!b) return 'no-button'; b.click(); return 'clicked'; })()"))
    time.sleep(18)                                 # 扫描 6s + 后端余量
    step("scan_result", ev(
        "JSON.stringify({badge: (document.getElementById('scanBadge')||{}).textContent,"
        " items: document.querySelectorAll('#devList > li').length,"
        " first: (document.querySelector('#devList > li b')||{}).textContent || ''})"))

    # 3) 内置背景：点一个「跟当前不一样」的预设 → 背景必须变、选中态必须唯一切换
    step("wall_before", ev(
        "JSON.stringify({bg: document.getElementById('bg').style.backgroundImage,"
        " sel: document.querySelectorAll('#wallGrid .sel').length,"
        " cur: window.__diag ? window.__diag().preset : '',"
        " cells: document.querySelectorAll('#wallGrid > *').length})"))
    step("click_preset", ev(
        "(() => { const cells = Array.prototype.slice.call("
        "document.querySelectorAll('#wallGrid > *'));"
        " const cur = window.__diag ? window.__diag().preset : '';"
        " const t = cells.filter(c => (c.dataset.preset || '') !== cur)[0];"
        " if (!t) return 'no-other-preset(' + cur + ')';"
        " t.click(); return 'clicked:' + t.dataset.preset; })()"))
    time.sleep(1.5)
    step("wall_after", ev(
        "JSON.stringify({bg: document.getElementById('bg').style.backgroundImage,"
        " sel: document.querySelectorAll('#wallGrid .sel').length,"
        " preset: window.__diag ? window.__diag().preset : null})"))

    # 4) 通透度滑杆：拖动 → --alpha 必须跟着变（证明外观不是死的），测完还原原值
    step("alpha_slider", ev(
        "(() => { const r = document.getElementById('rngAlpha');"
        " if (!r) return 'no-slider';"
        " const cs = () => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--alpha').trim();"
        " const old = cs();"
        " r.value = 24; r.dispatchEvent(new Event('input', {bubbles: true}));"
        " const now = cs();"
        " const back = Math.round(parseFloat(old) * 100) || 13;"
        " r.value = back; r.dispatchEvent(new Event('input', {bubbles: true}));"
        " return JSON.stringify({from: old, to: now, restored: cs(), slider: r.value}); })()"))

    # 5) 日志抽屉：开关必须真的动
    step("log_drawer", ev(
        "(() => { const b = document.getElementById('btnLogDrawer') || document.getElementById('btnLog');"
        " const d = document.querySelector('.log-drawer');"
        " if (!b || !d) return 'no-drawer';"
        " const before = d.classList.contains('open'); b.click();"
        " const mid = d.classList.contains('open'); b.click();"
        " return JSON.stringify({before, open: mid, closed: d.classList.contains('open')}); })()"))

    # 6) 自定义壁纸：点一张真实图片 → 背景必须变成 url(...) 且只选中它
    step("wall_custom", ev(
        "(() => { const it = document.querySelector('#wallGrid .wall-item[data-file]');"
        " if (!it) return 'no-custom-cell';"
        " it.click(); return 'clicked:' + it.dataset.file; })()"))
    time.sleep(2.0)
    step("wall_custom_after", ev(
        "JSON.stringify({hasImage: document.getElementById('bg').classList.contains('has-image'),"
        " url: (document.getElementById('bg').style.backgroundImage || '').slice(0, 140),"
        " sel: document.querySelectorAll('#wallGrid .sel').length,"
        " selFile: (document.querySelector('#wallGrid .wall-item.sel') || {}).dataset"
        " ? document.querySelector('#wallGrid .wall-item.sel').dataset.file || '' : ''})"))

    step("final", diag())

    try:
        out = data_dir() / "ui_verify.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verify-ui] 报告：{out}", flush=True)
    except Exception as exc:                       # noqa: BLE001
        print(f"[verify-ui] 写报告失败：{exc}", flush=True)

    # 必须在关窗之前还原观感设置：tools/verify_ui.py 读到报告后就可能在
    # 超时窗口结束后强杀本进程，如果把它留到 main() 的收尾，用户的自定义
    # 壁纸 / 预设 / 滑杆值就会被永久改掉（真实踩过）。
    try:
        _verify_cleanup(api)
        print("[verify-ui] 已还原自检前的观感设置", flush=True)
    except Exception as exc:                       # noqa: BLE001
        print(f"[verify-ui] 还原设置失败：{exc}", flush=True)

    try:
        window.destroy()
    except Exception:
        pass


def _check_path_budget() -> None:
    """打包版路径体检。

    坑：bleak 用的 WinRT 扩展模块文件名超长（_winrt_windows_devices_bluetooth_*.pyd），
    一旦程序被放到很深的目录，LoadLibrary 会报「文件名或扩展名太长」，
    表面症状就是「蓝牙一个设备都扫不出来」。这里提前量一下并留痕。
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    longest = 0
    try:
        longest = max((len(str(p)) for p in base.rglob("*.pyd")), default=0)
    except Exception:
        pass
    if longest > 240 or len(str(base)) > 180:
        try:
            (data_dir() / "startup_warn.txt").write_text(
                f"程序目录过深，蓝牙模块可能加载失败。\n"
                f"当前根目录（{len(str(base))} 字符）：{base}\n"
                f"最长模块路径：{longest} 字符\n"
                f"建议把软件移动到更短的路径（例如 D:\\NFCMiniStudio）后重试。\n",
                encoding="utf-8")
        except Exception:
            pass
        print(f"[warn] 路径过长：root={len(str(base))} pyd={longest}，蓝牙可能加载失败",
              flush=True)


def main() -> int:
    sync_wallpapers()
    sync_device_assets()          # 设备页 3D 渲染图必须落在 app/web/assets/ 下
    _check_path_budget()

    api = Api()
    verify = "--verify-ui" in sys.argv
    if verify:
        _verify_prepare(api)
    index = web_dir() / "index.html"
    if not index.exists():
        raise SystemExit(f"前端资源缺失: {index}")

    window = webview.create_window(
        APP_TITLE,
        url=index.as_uri(),
        js_api=api,
        width=1280,
        height=820,
        min_size=(1080, 700),
        frameless=True,
        easy_drag=False,
        resizable=True,
        background_color="#080B14",
        text_select=True,
    )
    api._window = window

    storage = data_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)

    webview.start(
        func=(_verify_ui if "--verify-ui" in sys.argv else None),
        args=[window, api],
        debug=bool(sys.argv[1:2] and sys.argv[1] == "--debug"),
        private_mode=False,
        storage_path=str(storage),
    )
    if verify:
        _verify_cleanup(api)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
