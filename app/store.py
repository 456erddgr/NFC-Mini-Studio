# -*- coding: utf-8 -*-
"""本地持久化：设置项 + 卡夹（整卡数据）。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .paths import cards_file, settings_file

# 云端默认值直接取自 crack_engine 的厂商协议常量，避免两处硬编码写岔。
# 注意 token 语义：recovery 接口（NICETHEME）要的是登录态 token，不可预置 -> 默认空串；
# keygen 用的是厂商内置固定凭据，由 crack_engine.VENDOR_KEYGEN_TOKEN 直接使用，不进设置。
try:  # crack_engine 只依赖标准库 + protocol，不会形成循环导入
    from .crack_engine import DEFAULT_CLOUD_ENDPOINT, DEFAULT_CLOUD_TOKEN
except Exception:  # pragma: no cover - 极端情况下的兜底（值与小程序抓包实证一致）
    DEFAULT_CLOUD_ENDPOINT = ("https://nfctools.nfcreader.net/nice-json/"
                              "mini-program/v1/recovery/mfkey32v2")
    DEFAULT_CLOUD_TOKEN = ""

DEFAULT_SETTINGS: Dict[str, Any] = {
    "wallpaper": "",            # 背景图文件名（位于 wallpapers 目录），空 = 内置动态渐变
    "preset": "sonoma",         # 内置动态背景 id（默认 macOS 索诺玛渐变）
    "glass": 26,                # 玻璃模糊强度 0-100（小模糊=清透折射，大模糊=发糊发白）
    "opacity": 4,               # 玻璃乳白度 0-60（越低越通透）
    "theme": "dark",            # dark / light
    "default_key": "FFFFFFFFFFFF",
    "default_key_type": 0,      # 0=KEY A, 1=KEY B
    "auto_find": True,          # 连接后自动寻卡
    "last_mac": "",
    # ---- v2.0.0 新增 ----
    "auto_crack": True,         # 寻卡后自动在后台级联破解
    "crack_sector": -1,         # 自动破解的目标扇区；-1 = 全卡（全部 16 个扇区）
    "crack_timeout": 25,        # 单种破解方式的最长等待（秒）
    "crack_all_budget": 300,    # 全卡破解（16 扇区逐把验证）的总时间上限（秒）
    "crack_batch_auth": True,   # 候选密钥批量认证（约 1.1x；设备漏应答自动退回串行）
    "show_device": True,        # 设备页 3D 展示开关
    "device_spin": 18,          # 设备 3D 自转速度（度/秒，0 = 停转）
    # ---- v2.2.0 新增：破解双模式（云端 / 本地 mfkey32）----
    # v2.2.0 修复：默认值直接预置成厂商官方云端 recovery 接口 + 小程序固定 token，
    # 做到开箱即用优先云端；用户可在设置页替换成自己小程序账号的地址 / token。
    # （老配置里这两项若为空，启动时由 _migrate_cloud 一次性补齐，详见 CLOUD_REV）
    "crack_mode": "cloud",      # cloud=官方云端 recovery 解算（默认），local=本地离线 mfkey32
    "cloud_endpoint": DEFAULT_CLOUD_ENDPOINT,   # 官方 recovery 接口（/recovery/mfkey32v2）
    "cloud_token": DEFAULT_CLOUD_TOKEN,         # PUT 进 NICETHEME 请求头；需填自己的登录态 token（默认空）
    "cloud_timeout": 8,         # 云端解算超时（秒），超时自动降级本地
    "crack_nonces": "",         # 手动粘贴的 mfkey32 采集数据（uid+两轮 nt/nr_enc/ar_enc）
    # ---- v2.3.0 新增：功能设置（对齐小程序 function-setting 页的 6 个开关）----
    "hid_enable": False,            # 启用/显示 HID 卡读取
    "non_full_key_read": False,     # 非完整密钥读卡（采集 + 密钥缓存批量验证）
    "encrypted_zero_sectors": True, # 自动加密 0 扇区（写卡时同时写密钥块）
    "write_card_tips": True,        # 写卡前提示滚动码风险
    "power_display": False,         # 设备页显示读卡器电量
    "sak88": False,                 # SAK88 开关（0x88 类卡特殊处理）
}

# 已下线功能的历史配置键：旧版本写过的"USB / 串口直连"参数。
# 该功能官方文档并不存在（会把扩展坞误识别成读卡器），故彻底移除；
# 这些键现在没有任何代码再读写，仅作一次性清理，避免老配置里继续残留。
LEGACY_USB_KEYS = (
    "connection_mode", "last_port", "baud",
    "wired_vid", "wired_pid", "wired_serial",
)

# 观感参数版本：调过一次默认值就 +1，用于老配置的一次性迁移
UI_REV = 7
# 破解参数版本：全卡破解成为默认（-1），老配置里的 0 一次性迁到 -1
CRACK_REV = 1
# 云端协议版本：v2.2.0 修复后 +1；2 = 追加"清理旧版自动塞入的无效固定 token"
CLOUD_REV = 2

# 旧版（v2.2.0 修复中途）曾把厂商 keygen 固定凭据自动写进 cloud_token。
# 该值用于 recovery 接口必然 401（recovery 要的是小程序登录态 token），
# 因此升级到 CLOUD_REV=2 时若发现配置里仍是这个值，判定为“程序自动填入的脏值”并清空，
# 交回用户在设置页填自己的登录态 token（不会触碰用户手填的其它任何 token）。
LEGACY_AUTO_TOKENS = (
    "7a422ed2462aaabd3bda83cebe62f5602f83e8fca3cda089407db9aa5000c8d6",
)


class JsonStore:
    def __init__(self, path: Path, default: Any) -> None:
        self.path = path
        self._default = default
        self._data = self._load()

    def _load(self) -> Any:
        if not self.path.exists():
            return json.loads(json.dumps(self._default))
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(self._default, dict):
                merged = json.loads(json.dumps(self._default))
                merged.update(data or {})
                return merged
            return data
        except Exception:
            return json.loads(json.dumps(self._default))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    @property
    def data(self) -> Any:
        return self._data


class Settings(JsonStore):
    def __init__(self) -> None:
        super().__init__(settings_file(), DEFAULT_SETTINGS)
        self._migrate()

    def _migrate(self) -> None:
        self._drop_legacy_usb()
        self._migrate_ui()
        self._migrate_cloud()
        self._migrate_crack()
        self._migrate_macos()

    def _migrate_macos(self) -> None:
        """一次性把默认观感切到 macOS 风格（索诺玛渐变背景 + 深色）。

        自定义壁纸文件仍保留在 %APPDATA%\\NFCMiniStudio\\wallpapers 里，
        在「外观设置」点一下即可切回，不删除用户数据。
        """
        try:
            rev = int(self._data.get("_mac_rev", 0))
        except Exception:
            rev = 0
        if rev >= 2:
            return
        self._data["preset"] = "sonoma"
        self._data["wallpaper"] = ""
        self._data["theme"] = "dark"
        self._data["_mac_rev"] = 2
        self.save()

    def _migrate_crack(self) -> None:
        """破解扇区默认值迁移：老配置的 0（只解 0 扇区）→ -1（全卡）。

        官方行为是把整卡所有扇区都解出来；只解 0 扇区是历史默认值造成的。
        这里只做一次性迁移，之后用户想改回单扇区仍然可以在界面上选。
        """
        try:
            rev = int(self._data.get("_crack_rev", 0))
        except Exception:
            rev = 0
        if rev >= CRACK_REV:
            return
        try:
            cur = int(self._data.get("crack_sector", -1))
        except Exception:
            cur = -1
        if cur >= 0:
            self._data["crack_sector"] = -1
        self._data["_crack_rev"] = CRACK_REV
        self.save()

    def _drop_legacy_usb(self) -> None:
        """一次性清理已下线的 USB / 串口直连配置键（当前无任何代码读写它们）。"""
        removed = [k for k in LEGACY_USB_KEYS if k in self._data]
        if removed:
            for k in removed:
                self._data.pop(k, None)
            self.save()

    def _migrate_ui(self) -> None:
        """老配置里的观感参数若还是旧默认值（偏乳白 / 模糊偏弱），一次性迁到更通透的新默认。

        只迁移"恰好等于旧默认值"的项：用户手动调过的其它数值一律保留。
        """
        # preset 的"去蓝"迁移必须无条件执行（用户可能已是最新 rev 但仍是 aurora）
        if self._data.get("preset") in (None, "aurora"):
            self._data["preset"] = DEFAULT_SETTINGS["preset"]
            self.save()
        try:
            rev = int(self._data.get("_ui_rev", 0))
        except Exception:
            rev = 0
        if rev >= UI_REV:
            return
        # 旧默认 opacity：26(初版) → 13(二版) → 6(三版) → 现在 4
        if self._data.get("opacity") in (None, 26, 13, 6):
            self._data["opacity"] = DEFAULT_SETTINGS["opacity"]
        # 旧默认背景：极光（偏蓝）→ 石墨灰中性色。
        # 注意：这里不受 _ui_rev 影响——用户即使已经是最新 rev，只要还停在
        # 偏蓝的 aurora，也强制迁到中性 graphite，否则界面依旧发蓝。
        if self._data.get("preset") in (None, "aurora"):
            self._data["preset"] = DEFAULT_SETTINGS["preset"]
        # 旧默认 glass：70 → 90 → 现在 26
        if self._data.get("glass") in (None, 70, 90):
            self._data["glass"] = DEFAULT_SETTINGS["glass"]
        self._data["_ui_rev"] = UI_REV
        self.save()

    def _migrate_cloud(self) -> None:
        """v2.2.0 修复：老配置里云端地址 / token 为空 → 一次性补成官方默认值。

        这正是"设置里选了云端、实际却一直走本地 mfkey32"的直接原因：
        endpoint 为空时 crack_engine 判定"未配置云端解算接口"并直接降级本地。
        只补空值，用户自己填过的地址 / token 一律不动；补齐后写 _cloud_rev 标记，
        之后用户若又想清空，程序不会再擅自填回去。
        """
        # ① 值判定先行（与 _cloud_rev 版本号无关）：旧版自动写入的固定 keygen 凭据对
        #    recovery 接口必然 401，只要还留在配置里就清掉；只匹配已知历史常量，
        #    用户自己填过的登录态 token 绝不触碰。之所以放在版本闸门之前，是为了修复
        #    "已打过 _cloud_rev 标记、token 却是脏值" 的历史配置——只查版本的迁移会漏掉它。
        dirty = False
        if str(self._data.get("cloud_token", "") or "").strip() in LEGACY_AUTO_TOKENS:
            self._data["cloud_token"] = ""
            dirty = True
        try:
            rev = int(self._data.get("_cloud_rev", 0))
        except Exception:
            rev = 0
        if rev >= CLOUD_REV:
            if dirty:
                self.save()
            return
        if not str(self._data.get("cloud_endpoint", "") or "").strip():
            self._data["cloud_endpoint"] = DEFAULT_CLOUD_ENDPOINT
        # token 只在"键缺失"时补成空串占位；绝不填入无效固定值，避免云端必然 401
        if "cloud_token" not in self._data:
            self._data["cloud_token"] = ""
        if str(self._data.get("crack_mode", "") or "").strip().lower() not in ("cloud", "local"):
            self._data["crack_mode"] = DEFAULT_SETTINGS["crack_mode"]
        self._data["_cloud_rev"] = CLOUD_REV
        self.save()

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in (patch or {}).items():
            if k in DEFAULT_SETTINGS:
                self._data[k] = v
        self.save()
        return self._data


class CardBox(JsonStore):
    """卡夹：保存复制的整卡数据。"""

    def __init__(self) -> None:
        super().__init__(cards_file(), {"cards": []})

    # ------------------------------------------------------------ 查询
    def list(self) -> List[dict]:
        cards = self._data.get("cards", [])
        return sorted(cards, key=lambda c: c.get("created_at", 0), reverse=True)

    def get(self, card_id: str) -> dict | None:
        for c in self._data.get("cards", []):
            if c.get("id") == card_id:
                return c
        return None

    # ------------------------------------------------------------ 写入
    def add(self, name: str, uid: str, blocks: Dict[str, Any],
            keys: Dict[str, str] | None = None, note: str = "",
            card_type: str = "", class_label: str = "") -> dict:
        card = {
            "id": uuid.uuid4().hex[:12],
            "name": name or f"卡片 {time.strftime('%m-%d %H:%M')}",
            "uid": uid,
            "blocks": blocks,
            "keys": keys or {},
            "note": note,
            "card_type": card_type,
            "class_label": class_label,
            "created_at": time.time(),
        }
        self._data.setdefault("cards", []).insert(0, card)
        self.save()
        return card

    def update(self, card_id: str, patch: Dict[str, Any]) -> bool:
        card = self.get(card_id)
        if not card:
            return False
        for k, v in (patch or {}).items():
            if k != "id":
                card[k] = v
        self.save()
        return True

    def set_keys(self, card_id: str, keys: Dict[str, str],
                 meta: Dict[str, Any] | None = None) -> bool:
        """记录某张卡破解出来的扇区密钥（合并写入，不覆盖已有扇区）。"""
        card = self.get(card_id)
        if not card:
            return False
        cur = card.setdefault("keys", {})
        for k, v in (keys or {}).items():
            if v:
                cur[str(k)] = v
        if meta:
            card.setdefault("crack_meta", {}).update(meta)
        self.save()
        return True

    def rename(self, card_id: str, name: str) -> bool:
        card = self.get(card_id)
        if not card:
            return False
        card["name"] = name
        self.save()
        return True

    def remove(self, card_id: str) -> bool:
        before = len(self._data.get("cards", []))
        self._data["cards"] = [c for c in self._data.get("cards", []) if c.get("id") != card_id]
        self.save()
        return len(self._data["cards"]) < before

    def export(self, card_id: str, target: Path) -> Path:
        card = self.get(card_id)
        if not card:
            raise KeyError("卡夹中不存在该卡片")
        with open(target, "w", encoding="utf-8") as fp:
            json.dump(card, fp, ensure_ascii=False, indent=2)
        return target

    def import_file(self, source: Path) -> dict:
        with open(source, "r", encoding="utf-8") as fp:
            card = json.load(fp)
        card["id"] = uuid.uuid4().hex[:12]
        card.setdefault("created_at", time.time())
        self._data.setdefault("cards", []).insert(0, card)
        self.save()
        return card
