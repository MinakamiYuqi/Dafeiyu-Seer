# -*- coding: utf-8 -*-
"""临时图清理：玩家战绩图一人一张、只增不减，必须能自己收。

背景（2026-11-23）：`_arena_send` 每发一次图就在 plugin_data 落一张，
看板那三张是固定名（覆盖、不涨），但 `arena_player_<uid>.png` 一个号一张。
实测两个多月已经 8 张 2.8 MB；一年能到几百 MB，比数据库还大 —— 而它们发完就没用了。
"""
from __future__ import annotations

import os
import time

from seer_arena_view import prune_pngs


def _touch(d, name: str, age_days: float) -> str:
    """造一个文件并把 mtime 挪到 age_days 天前。"""
    p = os.path.join(str(d), name)
    with open(p, "wb") as f:
        f.write(b"x" * 16)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_超期被删_近期保留(tmp_path):
    old = _touch(tmp_path, "arena_player_111.png", 8)
    fresh = _touch(tmp_path, "arena_player_222.png", 1)
    gone = prune_pngs(str(tmp_path))
    assert gone == 1
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


def test_刚好在边界内不删(tmp_path):
    """6 天前的不该删（keep_days=7）。"""
    p = _touch(tmp_path, "arena_player_333.png", 6)
    assert prune_pngs(str(tmp_path)) == 0
    assert os.path.exists(p)


def test_数量超上限按旧到新删(tmp_path):
    for i in range(8):
        _touch(tmp_path, f"arena_player_{i}.png", age_days=6 - i * 0.5)
    gone = prune_pngs(str(tmp_path), keep_max=3)
    assert gone == 5
    left = sorted(f for f in os.listdir(tmp_path) if f.startswith("arena_player_"))
    assert len(left) == 3
    # 留下的是最新的三个（age 最小 = i 最大的三个）
    assert left == ["arena_player_5.png", "arena_player_6.png", "arena_player_7.png"]


def test_看板图不归它管(tmp_path):
    """arena_board_*.png 是固定名、每次覆盖，不该被清掉。"""
    board = _touch(tmp_path, "arena_board_pets.png", 30)
    prune_pngs(str(tmp_path))
    assert os.path.exists(board)


def test_目录不存在不崩(tmp_path):
    assert prune_pngs(str(tmp_path / "nope")) == 0


def test_空目录返回零(tmp_path):
    assert prune_pngs(str(tmp_path)) == 0
