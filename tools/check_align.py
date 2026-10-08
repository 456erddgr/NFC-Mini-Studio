# -*- coding: utf-8 -*-
"""静态一致性自检：前端调用的接口 / DOM / 绑定 / 设置项 是否与后端和 HTML 对齐。

用法：
    python tools/check_align.py

这个脚本不需要开窗口，纯静态分析，专门用来在"改完代码还没跑界面"时先抓低级错：
    1. app.js 里 api('xxx') 调用的方法，api.py 的 Api 类里是否真的有；
    2. app.js 里 $('#id') / getElementById('id') 用到的 id，index.html 里是否存在；
    3. index.html 里的每个 <button id> 是否都在 app.js 里绑定了事件（漏绑 = 点了没反应）；
    4. 导航 data-view="x" 是否有对应的 #view-x；
    5. 前端 save_settings 写入的键，store.DEFAULT_SETTINGS 里是否都登记了
       （没登记会被静默丢弃，典型症状：点内置背景没反应）。
退出码 0 = 全部通过，1 = 有问题。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "web" / "js" / "app.js"
INDEX = ROOT / "app" / "web" / "index.html"
API_PY = ROOT / "app" / "api.py"
STORE_PY = ROOT / "app" / "store.py"

# 按需绑定（弹窗按钮在 openModal 时才挂），不算漏绑
BIND_EXEMPT = {"modalOk", "modalCancel"}


def api_methods() -> set:
    tree = ast.parse(API_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Api":
            return {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return set()


def default_settings_keys() -> set:
    tree = ast.parse(STORE_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):      # DEFAULT_SETTINGS: Dict[str, Any] = {...}
            target = node.target
        if target is not None and getattr(target, "id", "") == "DEFAULT_SETTINGS":
            return {k.value for k in node.value.keys}
    return set()


def main() -> int:
    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")
    problems: list[str] = []

    # 1) 接口对齐
    api = api_methods()
    called = set(re.findall(r"\bapi\(\s*['\"]([a-zA-Z_][\w]*)['\"]", js))
    missing_api = sorted(called - api)
    if missing_api:
        problems.append("前端调用了后端不存在的方法: " + ", ".join(missing_api))

    unused_api = sorted(m for m in api - called if not m.startswith("_"))

    # 2) DOM id 对齐
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r'\$\(\s*[\'"]#([\w-]+)[\'"]', js))
    js_ids |= set(re.findall(r'getElementById\(\s*[\'"]([\w-]+)[\'"]', js))
    missing_dom = sorted(js_ids - html_ids)
    if missing_dom:
        problems.append("JS 引用了 HTML 里不存在的 id: " + ", ".join(missing_dom))

    # 3) 按钮绑定
    btn_ids = re.findall(r'<button[^>]*\bid="([^"]+)"', html)
    bound = set(re.findall(r'\$\(\s*[\'"]#([\w-]+)[\'"]\s*\)\s*\.\s*on(?:click|input|change)', js))
    unbound = [b for b in btn_ids if b not in bound and b not in BIND_EXEMPT]
    if unbound:
        problems.append("HTML 里的按钮没绑定任何事件（点了不会有反应）: " + ", ".join(unbound))

    # 滑杆
    for rid in re.findall(r'<input[^>]*type="range"[^>]*\bid="([^"]+)"', html):
        if rid not in bound:
            problems.append(f"滑杆 #{rid} 没绑定 oninput")

    # 4) 导航视图
    views = set(re.findall(r'data-view="([^"]+)"', html))
    for v in sorted(views):
        if f'id="view-{v}"' not in html:
            problems.append(f'导航 data-view="{v}" 找不到对应的 #view-{v}')

    # 5) save_settings 键登记
    keys = default_settings_keys()
    sent: set = set()
    for m in re.finditer(r"api\(\s*['\"]save_settings['\"]\s*,\s*\{([^}]*)\}", js):
        sent |= set(re.findall(r"([a-zA-Z_]\w*)\s*:", m.group(1)))
    rogue = sorted(sent - keys)
    if rogue:
        problems.append("前端写入的设置项没在 DEFAULT_SETTINGS 登记，会被后端静默丢弃: " + ", ".join(rogue))

    # 6) 静态资源存在
    for m in re.finditer(r'(?:src|href)="((?!https?:|data:|#)[^"]+)"', html):
        if not (INDEX.parent / m.group(1)).exists():
            problems.append("HTML 引用资源缺失: " + m.group(1))

    print("=" * 68)
    print("NFC Mini Studio · 静态一致性自检")
    print("=" * 68)
    print(f"后端 Api 方法 {len(api)} 个；前端调用 {len(called)} 个；HTML id {len(html_ids)} 个；"
          f"按钮 {len(btn_ids)} 个")
    if unused_api:
        print(f"[提示] 前端未调用（可能是给外部/预留）: {', '.join(unused_api)}")
    if problems:
        print("\n[FAIL] 发现 %d 处问题：" % len(problems))
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print("\n静态自检未通过，先修上面的问题再跑界面。")
        return 1
    print("\n[PASS] 接口 / DOM / 绑定 / 设置项 / 资源 全部对齐。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
