"""专注模式状态：写在插件数据目录里的 JSON，方便其他插件读取。

seer_expert 写，seer_info 读——这样发起深挖时，老插件能主动让路。
"""
from __future__ import annotations

import json
import os
import time

PLUGIN = "astrbot_plugin_seer_expert"
FILENAME = "focus.json"

_CACHE: dict[str, dict] = {}       # path → loaded dict
_CACHE_AT: dict[str, float] = {}   # path → load time
_CACHE_TTL = 5.0                   # 秒：5 秒内不重复读文件


def _path(data_root: str) -> str:
    return os.path.join(data_root, PLUGIN, FILENAME)


def _load(data_root: str) -> dict:
    path = _path(data_root)
    now = time.time()
    if _CACHE.get(path) is not None and now - _CACHE_AT.get(path, 0.0) < _CACHE_TTL:
        return _CACHE[path]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        result = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        result = {}
    _CACHE[path] = result
    _CACHE_AT[path] = now
    return result


def _save(data_root: str, data: dict) -> None:
    path = _path(data_root)
    _CACHE[path] = data            # 写时立即更新缓存，避免下一次读还走文件
    _CACHE_AT[path] = time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def set_focus(data_root: str, umo: str, minutes: int) -> float:
    data = _load(data_root)
    expires = time.time() + max(1, int(minutes)) * 60
    data[str(umo)] = expires
    _save(data_root, data)
    return expires


def clear_focus(data_root: str, umo: str) -> bool:
    data = _load(data_root)
    existed = data.pop(str(umo), None) is not None
    if existed:
        _save(data_root, data)
    return existed


def is_active(data_root: str, umo: str) -> bool:
    data = _load(data_root)
    try:
        exp = float(data.get(str(umo)) or 0)
    except (TypeError, ValueError):
        return False
    if exp < time.time():
        return False
    return True
