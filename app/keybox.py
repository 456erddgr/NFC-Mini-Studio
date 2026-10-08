# -*- coding: utf-8 -*-
"""密码库（对齐小程序「密码管理」页）：公共资源库 / 用户密钥库 / 读卡历史库。

小程序三个 Tab 的数据语义：
  * 公共资源库：厂商云端下发的常用密钥清单（云端未配置时用内置常用表兜底）；
  * 用户密钥库：用户自己新增 / 批量导入的密钥（本地持久化，可增删）；
  * 读卡历史库：每次读卡 / 破解命中后记录的 UID + 该卡密钥，可清空。

存储：%APPDATA%\\NFCMiniStudio\\keybox.json
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import data_dir

# 内置常用密钥（对齐小程序「公共资源库」截图，再补行业常见出厂默认）
PUBLIC_KEYS: List[str] = [
    "A1B2C3D4E5F6", "D6FACAD6D732", "C2F3CCEF1DF9", "B1B2B3B4B5B6",
    "A1A2A3A4A5A6", "010203040506", "A754DE67A103", "FEFAEAEAFEFE",
    "F89C86B2A961", "FFFFFFFFFFFF", "A0A1A2A3A4A5", "D3F7D3F7D3F7",
    "000000000000", "484558414354", "5C8FF9990DA2", "D01AFEEB890A",
    "75CCB59C9BED", "8FD0A4F256E9", "1A9823DB2C4E", "6A6B6C6D6E6F",
]

_LIB_ALIAS = {"public": "public", "user": "user", "history": "history",
              "公共资源库": "public", "用户密钥库": "user", "读卡历史库": "history"}


def _norm_key(value: Any) -> Optional[str]:
    """把任意输入规整成 12 位大写 Hex 密钥；不合法返回 None。"""
    if isinstance(value, (list, tuple)):
        try:
            value = "".join("%02X" % (int(x) & 0xFF) for x in value)
        except Exception:
            return None
    text = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    if len(text) < 12:
        text = text.rjust(12, "0")
    text = text[-12:].upper()
    return text if len(text) == 12 else None


class KeyBox:
    """三个密钥库的统一门面。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (data_dir() / "keybox.json")
        self._data = self._load()

    # ---- 存储 ----
    def _load(self) -> dict:
        base = {"user": [], "history": [], "public_extra": []}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    for k in base:
                        data.setdefault(k, [])
                    return data
            except Exception:
                pass
        return base

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def _rows(self, lib: str) -> List[dict]:
        if lib == "public":
            out = [{"id": f"pub{i}", "key": k, "builtin": True}
                   for i, k in enumerate(PUBLIC_KEYS)]
            for i, k in enumerate(self._data.get("public_extra", [])):
                out.append({"id": f"pubx{i}", "key": k, "builtin": False})
            # 去重（保留先出现的）
            seen, uniq = set(), []
            for row in out:
                if row["key"] in seen:
                    continue
                seen.add(row["key"])
                uniq.append(row)
            return uniq
        return list(self._data.get(lib, []))

    # ---- 对外 ----
    def list(self, lib: str = "public") -> List[dict]:
        return self._rows(_LIB_ALIAS.get(lib, "public"))

    def add(self, keys: Any, lib: str = "user") -> dict:
        lib = _LIB_ALIAS.get(lib, "user")
        if lib not in ("user", "public"):
            raise ValueError("只有用户密钥库可写入")
        items = keys if isinstance(keys, (list, tuple)) else re.split(r"[\s,;]+", str(keys or ""))
        added, skipped = 0, 0
        bucket = self._data.setdefault("user" if lib == "user" else "public_extra", [])
        existing = {r["key"] for r in self._rows(lib)}
        for raw in items:
            key = _norm_key(raw)
            if not key or key in existing:
                skipped += 1
                continue
            bucket.append({"id": f"{lib}-{int(time.time() * 1000)}-{added}",
                           "key": key, "created_at": time.time()})
            existing.add(key)
            added += 1
        self.save()
        return {"added": added, "skipped": skipped, "total": len(self._rows(lib))}

    def remove(self, key_id: str = "", key: str = "", lib: str = "user") -> bool:
        lib = _LIB_ALIAS.get(lib, "user")
        if lib == "public":
            bucket = self._data.get("public_extra", [])
            target = _norm_key(key)
            before = len(bucket)
            self._data["public_extra"] = [r for r in bucket
                                          if not (r.get("id") == key_id or (target and r.get("key") == target))]
            self.save()
            return len(self._data["public_extra"]) < before
        bucket = self._data.get(lib, [])
        before = len(bucket)
        self._data[lib] = [r for r in bucket
                           if not (r.get("id") == key_id or (_norm_key(key) and r.get("key") == _norm_key(key)))]
        self.save()
        return len(self._data[lib]) < before

    def clear(self, lib: str = "history") -> dict:
        lib = _LIB_ALIAS.get(lib, "history")
        if lib == "public":
            self._data["public_extra"] = []
        elif lib in ("user", "history"):
            self._data[lib] = []
        else:
            self._data["history"] = []
        self.save()
        return {"cleared": lib}

    def record_history(self, uid: str = "", keys: Any = None,
                       card_type: str = "", note: str = "") -> Optional[dict]:
        """读卡 / 破解命中后写一条历史记录（同 UID 合并密钥，不重复建卡）。"""
        uid = re.sub(r"[^0-9A-Fa-f]", "", str(uid or "")).upper()
        if not uid:
            return None
        key_list: List[str] = []
        for k in (keys if isinstance(keys, (list, tuple)) else [keys]):
            nk = _norm_key(k)
            if nk:
                key_list.append(nk)
        history = self._data.setdefault("history", [])
        for row in history:
            if row.get("uid") == uid:
                merged = set(row.get("keys") or [])
                merged.update(key_list)
                row["keys"] = sorted(merged)
                row["updated_at"] = time.time()
                self.save()
                return row
        row = {"id": f"his-{int(time.time() * 1000)}", "uid": uid, "keys": key_list,
               "card_type": card_type, "note": note, "created_at": time.time()}
        history.insert(0, row)
        self.save()
        return row

    def stats(self) -> dict:
        return {"public": len(self._rows("public")),
                "user": len(self._data.get("user", [])),
                "history": len(self._data.get("history", []))}