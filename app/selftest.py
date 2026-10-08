# -*- coding: utf-8 -*-
"""自检脚本：不开窗、纯命令行把主要链路跑一遍。

用法（源码目录内）：
    python -m app.selftest          # 或 python run.py --selftest

覆盖：
  1. 依赖与运行环境
  2. 资源路径解析（源码 / 打包两种模式）
  3. 前端静态契约：按钮 id 是否都在 DOM 里、前端调的 api 方法后端是否都有
  4. 后端接口联调（headless 调 Api，不弹窗）
  5. 壁纸链路：选图 -> 同步到前端目录 -> 前端能取到
  6. CSS 变量一致性（--alpha 必须是小数）
  7. 蓝牙扫描实测
  8. 破解链路：本地引擎加载 / 官方向量 5/5 / 双模式设置项 / 降级链 / 无凭据自动降级
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
_results: list[tuple[str, str, str]] = []
_lines: list[str] = []


def _emit(line: str) -> None:
    _lines.append(line)
    print(line, flush=True)


def record(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))
    icon = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "WARN": "[WARN]", "INFO": "[ .. ]"}[level]
    line = f"{icon} {name}"
    if detail:
        line += f" — {detail}"
    _emit(line)


def check(name: str, fn):
    """跑一个检查函数：返回值当作 detail，抛异常即 FAIL。"""
    try:
        detail = fn()
        record(PASS, name, detail or "")
        return True
    except AssertionError as exc:
        record(FAIL, name, str(exc))
        return False
    except Exception as exc:                       # noqa: BLE001
        record(FAIL, name, f"{type(exc).__name__}: {exc}")
        return False


# ------------------------------------------------------------ 1. 依赖
def t_deps() -> str:
    import bleak
    import webview

    try:
        import winrt  # noqa: F401
        winrt_ok = "winrt 就绪"
    except Exception as exc:                       # noqa: BLE001
        winrt_ok = f"winrt 缺失({exc})"
    return (f"pywebview {getattr(webview, '__version__', '?')} / "
            f"bleak {getattr(bleak, '__version__', '?')} / {winrt_ok}")


# ------------------------------------------------------------ 2. 路径
def t_paths() -> str:
    from . import paths

    web = paths.web_dir()
    for rel in ("index.html", "css/style.css", "js/app.js"):
        p = web / rel
        assert p.exists(), f"缺少前端资源 {p}"
    assert paths.data_dir().exists(), f"用户数据目录不可用 {paths.data_dir()}"
    return f"root={paths.project_root().name} frozen={paths.is_frozen()}"


# ------------------------------------------------------------ 3. 前端静态契约
_ID_RE = re.compile(r"\$\('#([A-Za-z0-9_-]+)'\)")
_API_RE = re.compile(r"""api\(\s*['"]([A-Za-z0-9_]+)['"]""")
_DYN_ID_RE = re.compile(r"""id=["']([A-Za-z0-9_-]+)["']""")


def _read_front() -> tuple[str, str]:
    from . import paths

    web = paths.web_dir()
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "js" / "app.js").read_text(encoding="utf-8")
    return html, js


def t_button_ids() -> str:
    html, js = _read_front()
    dom_ids = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", html))
    dyn_ids = set(_DYN_ID_RE.findall(js))
    want = set(_ID_RE.findall(js))
    missing = sorted(want - dom_ids - dyn_ids)
    assert not missing, f"前端引用了 DOM 里不存在的 id：{missing}（按钮会点了没反应）"
    return f"{len(want)} 个 id 全部命中（静态 {len(dom_ids)} / 动态 {len(dyn_ids)}）"


def t_api_methods() -> str:
    from .api import Api

    _, js = _read_front()
    called = {m for m in _API_RE.findall(js) if m != "api"}
    have = {n for n in dir(Api) if not n.startswith("_")}
    missing = sorted(called - have)
    assert not missing, f"前端调用了后端不存在的方法：{missing}"
    return f"{len(called)} 个接口全部存在"


def t_css_alpha() -> str:
    from . import paths

    css = (paths.web_dir() / "css" / "style.css").read_text(encoding="utf-8")
    m = re.search(r"--alpha:\s*([0-9.]+)\s*;", css)
    assert m, "style.css 未定义 --alpha"
    val = float(m.group(1))
    assert val <= 1, f"--alpha 初值 {val} 不是小数，JS 会把它当百分比导致玻璃发白/全透"
    assert "calc(var(--alpha) / 100" not in css, "--alpha 语义应统一为小数，不该再除 100"
    assert "backdrop-filter" in css, "缺少 backdrop-filter，玻璃效果不会生效"
    return f"--alpha={val}，blur/backdrop-filter 就绪"


def t_button_bind() -> str:
    """按钮有 id 不等于能点 —— 必须真的绑了事件，否则点下去毫无反应。"""
    html, js = _read_front()
    btn_ids = re.findall(r"""<button[^>]*\bid=["']([A-Za-z0-9_-]+)["']""", html)
    bound = set(re.findall(
        r"""\$\('#([A-Za-z0-9_-]+)'\)\s*\.\s*on(?:click|input|change)""", js))
    exempt = {"modalOk", "modalCancel"}            # 弹窗按钮按需绑定
    dead = [b for b in btn_ids if b not in bound and b not in exempt]
    assert not dead, f"这些按钮没绑定任何事件，点了不会有反应：{dead}"
    ranges = re.findall(r"""<input[^>]*type="range"[^>]*\bid="([A-Za-z0-9_-]+)["']""", html)
    dead_rng = [r for r in ranges if r not in bound]
    assert not dead_rng, f"这些滑杆没绑定 oninput：{dead_rng}"
    return f"{len(btn_ids)} 个按钮 + {len(ranges)} 个滑杆全部已绑定事件"


def t_nav_views() -> str:
    html, _ = _read_front()
    views = sorted(set(re.findall(r"""data-view=["']([A-Za-z0-9_-]+)["']""", html)))
    missing = [v for v in views if f'id="view-{v}"' not in html]
    assert not missing, f"导航项找不到对应视图：#view-{missing}"
    return f"{len(views)} 个导航视图（{'/'.join(views)}）齐全"


def t_settings_keys() -> str:
    """前端 save_settings 写的键必须在 DEFAULT_SETTINGS 里登记，否则被静默丢弃。"""
    from .store import DEFAULT_SETTINGS

    _, js = _read_front()
    sent: set = set()
    for m in re.finditer(r"""api\(\s*['"]save_settings['"]\s*,\s*\{([^}]*)\}""", js):
        sent |= set(re.findall(r"([A-Za-z_]\w*)\s*:", m.group(1)))
    rogue = sorted(sent - set(DEFAULT_SETTINGS))
    assert not rogue, f"前端写入的设置项未登记，会被后端丢掉（界面点了没反应）：{rogue}"
    return f"写入的 {len(sent)} 个设置键全部已登记"


# ------------------------------------------------------------ 4. 后端接口联调
def _api():
    from .api import Api

    return Api()


def t_backend_roundtrip() -> str:
    api = _api()
    info = api.app_info()
    assert info.get("ok"), f"app_info 失败：{info}"
    st = api.get_settings()
    assert st.get("ok"), f"get_settings 失败：{st}"
    walls = api.list_wallpapers()
    assert walls.get("ok"), f"list_wallpapers 失败：{walls}"
    # 设置往返
    before = st["data"].get("opacity")
    api.save_settings({"opacity": 20})
    assert api.get_settings()["data"]["opacity"] == 20, "save_settings 未落盘"
    api.save_settings({"opacity": before})
    return (f"版本 {info['data']['version']} / 设置项 {len(st['data'])} 项 / "
            f"壁纸 {len(walls['data'])} 张")


def t_wallpaper_pipeline() -> str:
    """造一张临时图，验证 选图->同步->前端可取 的完整链路。"""
    from . import paths

    src = paths.wallpapers_dir() / "__selftest__.png"
    dst = paths.web_wallpapers_dir() / "__selftest__.png"
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082")
    try:
        src.write_bytes(png)
        copied = paths.sync_wallpapers()
        assert dst.exists(), "sync_wallpapers 没把图片同步到前端目录（换了壁纸界面会取不到图）"
        items = _api().list_wallpapers()["data"]
        names = {i["name"] for i in items}
        assert "__selftest__.png" in names, "list_wallpapers 漏掉了刚同步的图片"
        url = next(i["url"] for i in items if i["name"] == "__selftest__.png")
        assert not url.startswith("/"), f"壁纸 url 不该以 / 开头（file:// 下会跑到盘根）：{url}"
        return f"同步 {copied} 张，url={url}"
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except Exception:
                pass


def t_settings_migrate() -> str:
    """老配置(file 里 opacity=26)应被一次性迁移成新默认。"""
    from . import paths
    from .store import DEFAULT_SETTINGS, UI_REV, Settings

    f = paths.settings_file()
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    try:
        f.write_text(json.dumps({"opacity": 26, "glass": 70}), encoding="utf-8")
        s = Settings()
        assert s.data.get("opacity") == DEFAULT_SETTINGS["opacity"], \
            f"老配置的 opacity 没被迁移（仍是 {s.data.get('opacity')}）"
        assert int(s.data.get("_ui_rev", 0)) == UI_REV, "_ui_rev 没写回"
        return f"26 -> {s.data['opacity']}，_ui_rev={UI_REV}"
    finally:
        try:
            if old:
                f.write_text(old, encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except Exception:
            pass


def t_cloud_defaults() -> str:
    """v2.2.0 修复：老配置里云端地址/token 为空，启动时必须被补成官方默认值。

    这是"选了云端却一直走本地 mfkey32"的根因：endpoint 为空 → crack_engine
    判定"未配置云端解算接口"→ 直接降级本地。这里连"用户自己填过就不覆盖"一起验。
    """
    from . import paths
    from .crack_engine import DEFAULT_CLOUD_ENDPOINT, DEFAULT_CLOUD_TOKEN
    from .store import CLOUD_REV, Settings

    f = paths.settings_file()
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    try:
        # 场景 A：老配置（v2.2.0 之前）——云端三项为空
        f.write_text(json.dumps({"crack_mode": "cloud", "cloud_endpoint": "",
                                 "cloud_token": ""}, ensure_ascii=False), encoding="utf-8")
        a = Settings().data
        assert a.get("cloud_endpoint") == DEFAULT_CLOUD_ENDPOINT, \
            f"老配置的空端点没被补成官方默认值：{a.get('cloud_endpoint')!r}"
        # token 是登录态、不可预置：迁移只把键补成空占位，绝不塞入无效固定值
        assert a.get("cloud_token") == DEFAULT_CLOUD_TOKEN == "", \
            f"空 token 不该被填入无效固定值：{a.get('cloud_token')!r}"
        assert int(a.get("_cloud_rev", 0)) == CLOUD_REV, "_cloud_rev 没写回，会每次启动重复迁移"

        # 场景 B：用户自己填过地址/token —— 迁移不得覆盖
        f.write_text(json.dumps({"crack_mode": "local", "cloud_endpoint": "https://my.host/x",
                                 "cloud_token": "my-token"}, ensure_ascii=False), encoding="utf-8")
        b = Settings().data
        assert b.get("cloud_endpoint") == "https://my.host/x", \
            f"用户自定义端点在迁移中被覆盖了：{b.get('cloud_endpoint')!r}"
        assert b.get("cloud_token") == "my-token", "用户自定义 token 在迁移中被覆盖了"
        assert b.get("crack_mode") == "local", f"用户的 local 模式被改掉了：{b.get('crack_mode')!r}"
        return f"空值补默认（_cloud_rev={CLOUD_REV}），自定义值不被覆盖"
    finally:
        try:
            if old:
                f.write_text(old, encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except Exception:
            pass


# ------------------------------------------------------------ 5. 破解链路（云端 / 本地双模式）
@contextlib.contextmanager
def _env_guard(**overrides):
    """临时改写破解相关环境变量（值传 None 表示删除），退出时逐项还原。

    云端的地址/token/超时是"环境变量优先于设置项"的，这里先固定住，
    才能让下面的降级检查跟本机现有环境无关。
    """
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_crack_local_load() -> str:
    """本地 mfkey32 引擎能不能加载：加载不了就只剩云端一条路。"""
    from .crack_engine import LocalEngine

    started = time.time()
    eng = LocalEngine()
    mod = eng.load()
    cost = time.time() - started
    assert mod is not None, f"本地引擎加载失败：{eng.error or '模块导入失败'}"
    assert eng.available, f"本地引擎不可用：{eng.error or '未知原因'}"
    assert not eng.error, f"本地引擎加载报错：{eng.error}"
    return f"本地 mfkey32 就绪（bits={eng.bits}，加载 {cost:.2f}s）"


def t_crack_vectors() -> str:
    """官方向量自检：mfkey32v2 README 的 5 组采集必须都还原出 A0A1A2A3A4A5。"""
    from .crack_engine import VECTOR_EXPECT, LocalEngine, fmt12

    res = LocalEngine().run_vectors()
    total = int(res.get("total", 0))
    hits = int(res.get("hits", 0))
    assert total == 5, f"官方向量数量不对：{total}（应为 5）"
    assert res.get("expect") == fmt12(VECTOR_EXPECT) == "A0A1A2A3A4A5", \
        f"向量期望密钥不对：{res.get('expect')}"
    miss = [r.get("index") for r in res.get("rows", []) if not r.get("ok")]
    assert hits == total, (f"官方向量只命中 {hits}/{total}，期望密钥 {fmt12(VECTOR_EXPECT)}"
                           f"（未命中：{miss or res.get('error') or '未知原因'}）")
    return f"官方向量 {hits}/{total} 命中（expect={res['expect']}，耗时 {res.get('elapsed')}s）"


def t_crack_settings() -> str:
    """双模式设置项必须已登记，且默认值预置成官方云端（否则前端切换/开箱即用都不成立）。"""
    from .crack_engine import DEFAULT_CLOUD_ENDPOINT, DEFAULT_CLOUD_TOKEN
    from .store import DEFAULT_SETTINGS, Settings

    need = {"crack_mode": "cloud", "cloud_endpoint": DEFAULT_CLOUD_ENDPOINT,
            "cloud_token": DEFAULT_CLOUD_TOKEN, "cloud_timeout": 8}
    missing = sorted(k for k in need if k not in DEFAULT_SETTINGS)
    assert not missing, f"设置项未登记，前端改了会被后端丢掉（切换破解模式没反应）：{missing}"
    assert DEFAULT_SETTINGS["crack_mode"] == "cloud", \
        f"crack_mode 默认值应为 cloud（厂商云端解算），实际 {DEFAULT_SETTINGS['crack_mode']!r}"
    assert DEFAULT_SETTINGS["cloud_endpoint"] == DEFAULT_CLOUD_ENDPOINT, \
        f"cloud_endpoint 默认值应为官方 recovery 地址：{DEFAULT_SETTINGS['cloud_endpoint']!r}"
    assert DEFAULT_SETTINGS["cloud_token"] == DEFAULT_CLOUD_TOKEN == "", \
        "cloud_token 默认值应为空（recovery 要登录态 token，不可预置）"
    timeout = DEFAULT_SETTINGS["cloud_timeout"]
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0, \
        f"cloud_timeout 应该是正数秒：{timeout!r}"
    live = Settings().data
    absent = sorted(k for k in need if k not in live)
    assert not absent, f"实际读到的设置里缺少这些键（老配置合并失败）：{absent}"
    return (f"4 项已登记（crack_mode 默认 {DEFAULT_SETTINGS['crack_mode']}"
            f" / 超时 {timeout}s / 端点预置官方默认、token 待用户填登录态）")


def t_cloud_credentials() -> str:
    """云端两套凭据必须分开（v2.2.0 联网实测回归点）。

    早期版本把 keygen 的固定 token 当成 NICETHEME 预置给 recovery，实测网关直接回
    401 "token 无效"，导致"选了云端却永远静默降级本地"。这里用桩函数离线验证：
    ① recovery 无登录态 token 时不出网、抛 CloudAuthError 并带中文原因；
    ② keygen 用 Authorization 内置固定凭据，且 uid 必须是十六进制字符串（整数会被 422 拒）。
    """
    from . import crack_engine as ce

    assert ce.DEFAULT_CLOUD_TOKEN == "", "recovery 的登录态 token 不允许有预置值"
    assert ce.VENDOR_KEYGEN_TOKEN and ce.VENDOR_KEYGEN_TOKEN != ce.DEFAULT_CLOUD_TOKEN, \
        "keygen 固定凭据必须与 recovery 登录态 token 分开"
    conf = ce.cloud_config({})
    assert conf["endpoint"] == ce.DEFAULT_CLOUD_ENDPOINT, f"默认端点异常：{conf['endpoint']!r}"
    assert conf["token"] == "", f"未配置时 token 应保持空：{conf['token']!r}"

    real = ce._vendor_request
    # ① keygen：抓请求头/请求体，用桩短路在提交阶段（不出网）
    seen: dict = {}

    def fake(url, headers, body, timeout, method="POST"):
        seen.update(url=url, headers=dict(headers), body=body)
        return {"errcode": 1, "errmsg": "stub-stop"}

    def submit_for(uid_text: str) -> dict:
        ce._vendor_request = fake
        try:
            try:
                ce.cloud_keygen_lookup(uid_text)
            except ce.EngineError as exc:
                assert "stub-stop" in str(exc), f"桩未生效：{exc}"
            else:
                raise AssertionError("keygen 桩未生效，测试无效")
        finally:
            ce._vendor_request = real
        return dict(seen)

    hit = submit_for("A0A1A2A3")
    assert hit["headers"].get("Authorization") == ce.VENDOR_KEYGEN_TOKEN, \
        f"keygen 必须用内置固定凭据：{hit['headers'].get('Authorization')!r}"
    assert "NICETHEME" not in hit["headers"], "keygen 不该带登录态头"
    assert hit["body"] == {"uid": "A0A1A2A3"}, \
        f"keygen 的 uid 必须是十六进制字符串（整数会被厂商 422 拒）：{hit['body']!r}"

    # 纯数字 UID 的歧义回归：'11223344' 是十六进制，绝不能按十进制算成 0x00AB4130
    digits = submit_for("11223344")
    assert digits["body"] == {"uid": "11223344"}, \
        f"纯数字 UID 被按十进制解析了（云端会查错卡）：{digits['body']!r}"

    # ② recovery：未配 token 时必须零请求 + 明确中文原因
    frames = [ce.NonceFrame.from_sequence([int(v, 16) for v in ce.LOCAL_VECTORS[0]])]
    calls: list = []
    ce._vendor_request = lambda *a, **k: calls.append(a) or {}
    try:
        try:
            ce.cloud_solve(frames, ce.DEFAULT_CLOUD_ENDPOINT, "", 5)
        except ce.CloudAuthError as exc:
            assert "token" in str(exc), f"降级原因应点明 token：{exc}"
        else:
            raise AssertionError("未配 token 时 cloud_solve 应抛 CloudAuthError")
    finally:
        ce._vendor_request = real
    assert not calls, "未配 token 却仍然向云端发起了请求"
    return "recovery 需登录态 token（缺则零请求+中文原因降级）；keygen 走内置固定凭据且 uid 传字符串"


def t_cloud_store_migration() -> str:
    """老配置云端迁移（v2.2.0 修复回归点）：补齐官方端点的同时，必须清掉旧版脏 token。

    现场配置里曾出现 cloud_token = keygen 固定凭据、_cloud_rev = 1 的历史状态；
    若迁移只看版本号不看值，该无效 token 会被永久保留 -> 云端每次 401 静默降级本地。
    这里直接对 store 的迁移函数做三组离线断言（不触碰真实用户配置）。
    """
    import json as _json
    import tempfile
    from pathlib import Path as _P

    from .store import (CLOUD_REV, DEFAULT_CLOUD_ENDPOINT, DEFAULT_SETTINGS,
                        LEGACY_AUTO_TOKENS, JsonStore, Settings)

    with tempfile.TemporaryDirectory() as td:
        def migrated(raw: dict) -> dict:
            p = _P(td) / "settings.json"
            p.write_text(_json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            st = JsonStore(p, DEFAULT_SETTINGS)
            Settings._migrate_cloud(st)
            return st.data

        # ① 冷配置：旧版自动塞入的固定 token + _cloud_rev=1 -> 清空并补端点
        cold = migrated({
            "crack_mode": "cloud", "cloud_endpoint": "",
            "cloud_token": LEGACY_AUTO_TOKENS[0], "_cloud_rev": 1,
        })
        assert cold["cloud_token"] == "", f"旧版脏 token 未清空：{cold['cloud_token']!r}"
        assert cold["cloud_endpoint"] == DEFAULT_CLOUD_ENDPOINT, "官方端点未补齐"
        assert int(cold["_cloud_rev"]) == CLOUD_REV, "迁移版本号未推进"

        # ①b 现场真实现状：_cloud_rev 已经是 CLOUD_REV、token 却仍是脏值
        #     （只查版本号的迁移会直接 return，脏 token 永久残留 -> 云端 401 静默降级）
        stamped = migrated({
            "crack_mode": "cloud", "cloud_endpoint": DEFAULT_CLOUD_ENDPOINT,
            "cloud_token": LEGACY_AUTO_TOKENS[0], "_cloud_rev": CLOUD_REV,
        })
        assert stamped["cloud_token"] == "", \
            f"已打标记的历史配置里脏 token 未清空：{stamped['cloud_token']!r}"

        # ② 用户自己填过的 token 绝不能被迁移动到
        mine = migrated({"crack_mode": "cloud", "cloud_token": "my-own-login-token",
                         "_cloud_rev": 1})
        assert mine["cloud_token"] == "my-own-login-token", \
            f"用户自填 token 被覆盖：{mine['cloud_token']!r}"

        # ③ 全新配置：默认云端端点 + 空 token + crack_mode=cloud
        fresh = migrated({})
        assert fresh["crack_mode"] == "cloud", f"默认破解模式应为 cloud：{fresh['crack_mode']!r}"
        assert fresh["cloud_endpoint"] == DEFAULT_CLOUD_ENDPOINT, "默认端点不是官方恢复接口"
        assert fresh["cloud_token"] == "", f"默认 token 应为空（用户填登录态）：{fresh['cloud_token']!r}"
    return "老配置补官方端点、清旧版脏 token（用户自填 token 保留）、新配置默认 cloud"


def t_crack_chain() -> str:
    """降级链：云端模式 cloud→local→device，本地模式 local→device。"""
    from .crack_engine import MODE_CLOUD, MODE_DEVICE, MODE_LOCAL, CrackEngine

    with _env_guard(NFCMINI_CRACK_MODE=None):
        cloud = CrackEngine({"crack_mode": MODE_CLOUD}).chain_for(MODE_CLOUD)
        local = CrackEngine({"crack_mode": MODE_LOCAL}).chain_for(MODE_LOCAL)
        by_cloud_setting = CrackEngine({"crack_mode": MODE_CLOUD}).chain_for()
        by_local_setting = CrackEngine({"crack_mode": MODE_LOCAL}).chain_for()
    assert cloud == [MODE_CLOUD, MODE_LOCAL, MODE_DEVICE], f"云端链路不对：{cloud}"
    assert local == [MODE_LOCAL, MODE_DEVICE], f"本地链路不对：{local}"
    assert by_cloud_setting == cloud, f"crack_mode=cloud 时的链路不对：{by_cloud_setting}"
    assert by_local_setting == local, f"crack_mode=local 时的链路不对：{by_local_setting}"
    return "cloud→local→device / local→device（设置项切换即时生效）"


def t_crack_fallback() -> str:
    """无 token + 端点不可达时，solve 必须自动降级到本地并解出官方密钥。"""
    from .crack_engine import LOCAL_VECTORS, VECTOR_EXPECT, CrackEngine, NonceFrame, fmt12

    frames = [NonceFrame.from_sequence([int(v, 16) for v in LOCAL_VECTORS[0]])]
    dead = "http://127.0.0.1:9/nfc/crack"          # 本机没人监听，必然失败
    with _env_guard(NFCMINI_CLOUD_ENDPOINT=dead, NFCMINI_CLOUD_TOKEN="dummy-token-for-unreachable-test",
                    NFCMINI_CLOUD_TIMEOUT="2", NFCMINI_CRACK_MODE=None):
        eng = CrackEngine({"crack_mode": "cloud", "cloud_endpoint": dead,
                           "cloud_token": "dummy-token-for-unreachable-test", "cloud_timeout": 2})
        res = eng.solve(frames=frames, allow_device_fallback=False)
    attempts = res.get("attempts", [])
    cloud_row = next((a for a in attempts if a.get("method") == "cloud"), None)
    assert cloud_row is not None, f"solve 没有尝试云端，链路不对：{attempts}"
    assert not cloud_row.get("ok"), "云端居然成功了，这条检查没验证到降级（请换一个不可达端点）"
    reason = res.get("summary") or (attempts[-1].get("detail") if attempts else "")
    assert res.get("ok"), f"云端失败后没有降级到本地解出密钥：{reason}"
    assert res.get("engine") == "local", f"应由本地引擎命中，实际 {res.get('engine')!r}"
    assert res.get("key") == fmt12(VECTOR_EXPECT), f"降级后解出的密钥不对：{res.get('key')}"
    assert res.get("fallback_to_device") is False, "已禁止固件级联兜底，不该回退 device"
    return (f"云端失败（{cloud_row.get('detail')}）→ 本地命中 {res['key']}"
            f"（整体 {res.get('elapsed')}s）")


# ------------------------------------------------------------ 6. 蓝牙
def t_ble() -> str:
    from .device import NfcDevice

    dev = NfcDevice()
    try:
        found = dev.scan(timeout=6.0)
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass
    hint = ""
    if not found:
        hint = ("（蓝牙适配器未开启、或附近无广播设备时会为 0；"
                "若系统设置里蓝牙已开却始终为 0，需要单独查 WinRT/驱动）")
        record(WARN, "蓝牙扫描实测", "0 个设备" + hint)
        return ""
    friendly = [d.get("name") or d.get("address") or "?" for d in found[:5]]
    names = "、".join(str(x) for x in friendly)
    liked = [d.get("name") for d in found if d.get("likely_reader")]
    extra = f"，疑似读卡器：{'、'.join(str(x) for x in liked)}" if liked else ""
    record(PASS, "蓝牙扫描实测", f"{len(found)} 个设备：{names}{extra}")
    return ""


# ------------------------------------------------------------ 主入口
def run() -> int:
    _emit("=" * 62)
    _emit("NFC Mini Studio · 自检")
    _emit("=" * 62)

    check("依赖导入", t_deps)
    check("资源路径解析", t_paths)
    check("按钮 id 对齐", t_button_ids)
    check("按钮/滑杆事件绑定", t_button_bind)
    check("导航视图对齐", t_nav_views)
    check("前后端接口对齐", t_api_methods)
    check("设置项登记", t_settings_keys)
    check("CSS 玻璃变量", t_css_alpha)
    check("后端接口联调", t_backend_roundtrip)
    check("壁纸链路", t_wallpaper_pipeline)
    check("老配置迁移", t_settings_migrate)
    check("云端默认值补齐", t_cloud_defaults)
    check("破解·本地引擎加载", t_crack_local_load)
    check("破解·官方向量自检", t_crack_vectors)
    check("破解·双模式设置项", t_crack_settings)
    check("破解·降级链顺序", t_crack_chain)
    check("破解·云端两套凭据", t_cloud_credentials)
    check("破解·老配置云端迁移", t_cloud_store_migration)
    check("破解·无凭据自动降级", t_crack_fallback)

    _emit("-" * 62)
    try:
        t_ble()
    except Exception:                              # noqa: BLE001
        record(WARN, "蓝牙扫描实测",
               "扫描异常：" + traceback.format_exc(limit=1).strip().splitlines()[-1])

    bad = [r for r in _results if r[0] == FAIL]
    warn = [r for r in _results if r[0] == WARN]
    _emit("=" * 62)
    _emit(f"结果：{len(_results) - len(bad) - len(warn)} 通过 / {len(bad)} 失败 / {len(warn)} 警告")
    for level, name, detail in bad + warn:
        _emit(f"  - {level}: {name} {detail}")
    _emit("=" * 62)

    # 打包后没有控制台，报告落盘一份方便查看
    try:
        from . import paths

        report = paths.data_dir() / "selftest_report.txt"
        report.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        print(f"报告已写入：{report}", flush=True)
    except Exception:
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.selftest import run  # type: ignore
    sys.exit(run())
