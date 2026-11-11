"""自定义外号配置解析的离线测试。

运行：python tests/test_custom_alias.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "custom_alias", ROOT / "core" / "custom_alias.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

parse_alias_config = mod.parse_alias_config
failures: list[str] = []

CASES = [
    ("二哈=雷伊", {"二哈": "雷伊"}),
    ("二哈=雷伊\n奶妈=谱尼", {"二哈": "雷伊", "奶妈": "谱尼"}),
    ("二哈:雷伊；奶妈：谱尼", {"二哈": "雷伊", "奶妈": "谱尼"}),
    ("二哈=雷伊，奶妈=谱尼", {"二哈": "雷伊", "奶妈": "谱尼"}),
    ("南霜=4511", {"南霜": "4511"}),
    ("  # 注释\n二哈 = 雷伊 \n\n", {"二哈": "雷伊"}),
    ("二哈=\"雷伊\"", {"二哈": "雷伊"}),
    ("坏行没有分隔符", {}),
    ("", {}),
    ("=雷伊", {}),
    ("二哈=", {}),
    ("二哈=二哈", {}),
    ("同一个=雷伊\n同一个=谱尼", {"同一个": "谱尼"}),
]

for text, expected in CASES:
    got = parse_alias_config(text)
    if got != expected:
        failures.append(f"parse_alias_config({text!r}) -> {got!r}，期望 {expected!r}")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print(f"OK: {len(CASES)} 条外号解析用例全部通过")
