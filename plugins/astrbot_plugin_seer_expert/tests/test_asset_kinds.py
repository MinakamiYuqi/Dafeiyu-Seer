# -*- coding: utf-8 -*-
"""`_ASSET_KINDS` 里每个 kind 都必须真有实现分支。

★ 为什么单开一条（2026-11-16 加的）：
  `seer_asset` 用 `_ASSET_KINDS` 把群友说的「刻印」「道具」这类词映射成内部 key，
  然后 `_asset_id()` 按 key 查 id。问题是 **两个 dict 是各写各的**：
  往 `_ASSET_KINDS` 加一个 key 很容易，但忘了在 `_asset_id` 里加分支，
  运行时不会报错 —— 只是走到函数最后一句

      return None, f"不支持的素材类型：{key}"

  于是工具「看着能用、永远失败、没有日志」。mintmark_icon 和 item 就这样挂了两周，
  直到 2026-11-16 鱼片说「刻印图标查不到」才被发现。

  所以这里做静态对账：`_ASSET_KINDS` 的**值**集合 ⊆
  { pet_body/pet_head 特例, element 特例, table_map 里的表, 以及所有 `key == "xxx"` 分支 }。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _find_src() -> Path | None:
    for base in (ROOT, ROOT / "plugins"):
        p = base / "astrbot_plugin_seer_info" / "main.py"
        if p.is_file():
            return p
    return None


@pytest.fixture(scope="module")
def src() -> str:
    p = _find_src()
    if p is None:
        pytest.skip("找不到 astrbot_plugin_seer_info/main.py")
    return p.read_text(encoding="utf-8")


def _block(text: str, start_pat: str) -> str:
    """取出从 start_pat 开始到下一个同级缩进 `}` 为止的代码块。"""
    m = re.search(start_pat, text)
    assert m, f"找不到 {start_pat}"
    i = m.start()
    depth = 0
    for j in range(m.end() - 1, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"{start_pat} 的括号没闭合")


def test_every_asset_kind_has_implementation(src):
    kinds_src = _block(src, r"_ASSET_KINDS\s*=\s*\{")
    table_src = _block(src, r"table_map\s*=\s*\{")

    # 中文名 → 内部 key
    values = sorted({v for _k, v in re.findall(r'"([^"]+)":\s*"(\w+)"', kinds_src)})
    assert values, "没解析出 _ASSET_KINDS 的值"

    # _asset_id / seer_asset 里出现的 key 分支
    branch_keys = set(re.findall(r'key\s*==\s*"(\w+)"', src))
    table_keys = set(re.findall(r'"(\w+)":\s*\("', table_src))

    missing = []
    for v in values:
        if v in ("pet_body", "pet_head"):
            ok = '"pet_body", "pet_head"' in src
        elif v == "element":
            ok = "_element_icon_id" in src
        else:
            ok = (v in branch_keys) or (v in table_keys)
        if not ok:
            missing.append(v)

    assert not missing, (
        "这些 kind 在 _ASSET_KINDS 里挂了名，但 _asset_id 里没有实现分支 —— "
        "调用会静默返回「不支持的素材类型」：\n  " + "\n  ".join(missing)
    )


def test_no_orphan_branch(src):
    """反过来：实现里有的分支，_ASSET_KINDS 里也应该能映射到（防止死分支）。"""
    kinds_src = _block(src, r"_ASSET_KINDS\s*=\s*\{")
    values = {v for _k, v in re.findall(r'"([^"]+)":\s*"(\w+)"', kinds_src)}
    table_src = _block(src, r"table_map\s*=\s*\{")
    table_keys = set(re.findall(r'"(\w+)":\s*\("', table_src))
    branch_keys = set(re.findall(r'key\s*==\s*"(\w+)"', src))

    orphans = sorted((table_keys | branch_keys) - values)
    assert not orphans, f"这些实现分支没人能调到（_ASSET_KINDS 里没有对应中文名）：\n  " + "\n  ".join(orphans)
