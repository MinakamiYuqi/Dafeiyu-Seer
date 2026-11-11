"""配置图元数据解析的离线测试。

运行：python tests/test_pet_config_meta.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "pet_config_meta", ROOT / "core" / "pet_config_meta.py"
)
meta_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = meta_mod
spec.loader.exec_module(meta_mod)

parse_upload_arg = meta_mod.parse_upload_arg
ConfigMeta = meta_mod.ConfigMeta

failures: list[str] = []

CASES = [
    # (输入, 期望精灵名, 期望作者, 期望备注)
    ("雷伊", "雷伊", "", ""),
    ("雷伊 作者:张三", "雷伊", "张三", ""),
    ("雷伊 作者：张三 备注：物攻流，配泰坦", "雷伊", "张三", "物攻流，配泰坦"),
    ("雷伊 作者=张三 备注=物攻流", "雷伊", "张三", "物攻流"),
    ("雷伊 这是物攻流的配置", "雷伊", "", "这是物攻流的配置"),
    ("雷伊 来源:赛群 说明:带双防", "雷伊", "赛群", "带双防"),
    ("70 作者:老王", "70", "老王", ""),
    # 方括号/圆括号包裹（真实用户写法）
    (
        "艾夏拉[作者:佑栖] [备注:5级秘咒净灵咒，7级特攻加一天蛇，5级威力灵剑]",
        "艾夏拉",
        "佑栖",
        "5级秘咒净灵咒，7级特攻加一天蛇，5级威力灵剑",
    ),
    ("艾夏拉 [作者:佑栖] [备注:5级秘咒净灵咒]", "艾夏拉", "佑栖", "5级秘咒净灵咒"),
    ("艾夏拉 【作者：佑栖】【备注：双防】", "艾夏拉", "佑栖", "双防"),
    ("艾夏拉（作者=佑栖）", "艾夏拉", "佑栖", ""),
    ("", "", "", ""),
    ("   ", "", "", ""),
]

for text, want_name, want_author, want_note in CASES:
    name, meta = parse_upload_arg(text)
    if name != want_name or meta.author != want_author or meta.note != want_note:
        failures.append(
            f"parse_upload_arg({text!r}) -> {(name, meta.author, meta.note)!r}，"
            f"期望 {(want_name, want_author, want_note)!r}"
        )

# 序列化往返
m = ConfigMeta(author="张三", note="物攻流", uploader="uid", updated_at="2026-10-22 01:30")
roundtrip = meta_mod.loads(meta_mod.dumps(m))
if roundtrip != m:
    failures.append(f"序列化往返失败: {roundtrip!r}")

# 展示行：空字段不显示
lines = m.lines()
if lines[0] != "作者：张三" or lines[1] != "备注：物攻流" or lines[2] != "收录：2026-10-22 01:30":
    failures.append(f"lines() 不对: {lines!r}")
if ConfigMeta().lines():
    failures.append("空元数据不应产生展示行")

# 空白昵称（U+3164 HANGUL FILLER 这类）要当成空
if meta_mod.clean_text("\u3164") != "":
    failures.append("HANGUL FILLER 昵称应清洗为空")
if meta_mod.clean_text(" 张三 ") != "张三":
    failures.append("正常昵称不应被清洗掉")
if meta_mod.clean_text("") != "":
    failures.append("空串应返回空串")

# 改备注只覆盖传入字段
merged = meta_mod.merge_meta(m, author="", note="双防流")
if merged.author != "张三" or merged.note != "双防流" or merged.uploader != "uid":
    failures.append(f"merge_meta 不对: {merged!r}")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print(f"OK: {len(CASES)} 条解析用例 + 序列化/展示/合并检查全部通过")
