"""魂印富文本解析的离线测试。

运行：python tests/test_soulmark_format.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "soulmark_format", ROOT / "core" / "soulmark_format.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

parse_rich_soulmark = mod.parse_rich_soulmark
strip_markup = mod.strip_markup

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


SAMPLE = (
    "<indent=0><sprite=0><indent=16>登场效果：\r\n"
    "<indent=16><sprite=3><indent=32>每次登场时令<b>双方</b><color=#64F9FA>全属性+1</color>，然后：\r\n"
    "<indent=32><sprite=4><indent=48>若复制成功则额外100%<color=#64F9FA>令双方凝滞</color>\r\n"
    "\r\n"
    "<indent=0><sprite=0><indent=16>战阶结束效果：\r\n"
    "<indent=16><sprite=3><indent=32>战斗阶段结束时：\r\n"
    "<indent=0><sprite=5><indent=15>不破诛罚：\r\n"
    "<indent=16><sprite=6><indent=32>下回合令对手的<b>先制等级</b>无法高于自身"
)

sections = parse_rich_soulmark(SAMPLE)
check(len(sections) == 3, f"应解析出 3 个小节（实际 {len(sections)}）")
check(sections[0]["title"] == "登场效果", f"第一节标题（实际 {sections[0]['title']!r}）")
check(sections[1]["title"] == "战阶结束效果", f"第二节标题（实际 {sections[1]['title']!r}）")
check(sections[2]["title"] == "不破诛罚", f"第三节标题（实际 {sections[2]['title']!r}）")

first = sections[0]["lines"]
check(len(first) == 2, f"第一节应有 2 行（实际 {len(first)}）")
check(first[0]["level"] == 2, f"首行层级（实际 {first[0]['level']}）")
check("<b>" in first[0]["html"] and "</b>" in first[0]["html"], "粗体应闭合")
check("color:#64F9FA" in first[0]["html"], "颜色应转成 span")
check(first[0]["html"].count("<span") == first[0]["html"].count("</span>"), "span 应闭合")
check(first[1]["level"] == 3, f"子行层级（实际 {first[1]['level']}）")

# 无标记文本不应被当成富文本
check(parse_rich_soulmark("每次登场时令双方全属性+1") == [], "纯文本返回空列表")
check(parse_rich_soulmark("") == [], "空串返回空列表")

# 去标记
check(strip_markup("<indent=16><sprite=3><b>双方</b>全属性") == "双方全属性", "strip_markup 去标记")
check(strip_markup("") == "", "strip_markup 空串")

# 只有颜色、没有缩进：也归到默认小节
only_color = parse_rich_soulmark("<indent=0><sprite=0><indent=16>标题：\r\n<color=#FFF779>重点</color>")
check(len(only_color) == 1 and only_color[0]["title"] == "标题", "只有颜色时仍归到当前小节")
check("重点" in only_color[0]["lines"][0]["text"], "颜色文本保留")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print(f"OK: 魂印富文本解析 {len(sections)} 小节、样式闭合与回退用例全部通过")
