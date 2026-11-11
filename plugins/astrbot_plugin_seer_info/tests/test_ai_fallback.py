"""AI 兜底的离线测试（不依赖 AstrBot、不依赖网络）。

运行：python tests/test_ai_fallback.py
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用合成包把 core/ 当成一个包导入，这样模块里的相对导入能正常工作，
# 又不会触发 core/__init__.py（它依赖 AstrBot）
_pkg = types.ModuleType("seer_core_test")
_pkg.__path__ = [str(ROOT / "core")]
sys.modules[_pkg.__name__] = _pkg

parser = importlib.import_module("seer_core_test.command_parser")
ai = importlib.import_module("seer_core_test.ai_fallback")

parse_intent_json = ai.parse_intent_json
should_ask_ai = ai.should_ask_ai
build_messages = ai.build_messages

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


# --------------------------------------------------------------------------- #
# 模型输出解析
# --------------------------------------------------------------------------- #

JSON_CASES = [
    ('{"intent": "pet_info", "keyword": "雷伊"}', "pet_info", "雷伊"),
    ('{"intent":"pet_info","keyword":"  雷伊  "}', "pet_info", "雷伊"),
    ('{"intent":"pet_info","keyword":"「雷伊」"}', "pet_info", "雷伊"),
    ('{"intent":"pet_info","keyword":"雷伊\\n第二行"}', "pet_info", "雷伊"),
    ('{"intent":"pet_info"}', "pet_info", ""),
    ('{"intent":"preview"}', "preview", ""),
    ('{"intent": "PET_INFO", "keyword": "雷伊"}', "pet_info", "雷伊"),
    # 模型返回中文指令名也要能认
    ('{"intent": "精灵", "keyword": "雷伊"}', "pet_info", "雷伊"),
    ('{"intent": "魂印", "keyword": "雷伊"}', "pet_info", "雷伊"),
    ('{"intent": "属性表", "keyword": "火"}', "type_matchup", "火"),
    # 代码块 / 多余解释
    ('```json\n{"intent": "gem", "keyword": "火"}\n```', "gem", "火"),
    ('好的，解析结果如下：\n{"intent": "suit", "keyword": "银翼"}\n希望有帮助', "suit", "银翼"),
]

for raw, key, keyword in JSON_CASES:
    result = parse_intent_json(raw)
    if result is None:
        failures.append(f"parse_intent_json({raw!r}) -> None，期望 {(key, keyword)!r}")
    elif result.key != key or result.keyword != keyword:
        failures.append(
            f"parse_intent_json({raw!r}) -> {(result.key, result.keyword)!r}，"
            f"期望 {(key, keyword)!r}"
        )

NONE_CASES = [
    "",
    "   ",
    "我不确定",
    "not a json",
    '{"intent": "none", "keyword": ""}',
    '{"intent": "none"}',
    '{"intent": "chat"}',
    '{"intent": "unknown_intent", "keyword": "雷伊"}',
    '{"foo": "bar"}',
    "[1, 2, 3]",
]

for raw in NONE_CASES:
    if parse_intent_json(raw) is not None:
        failures.append(f"parse_intent_json({raw!r}) 应返回 None")

# --------------------------------------------------------------------------- #
# 触发判断
# --------------------------------------------------------------------------- #

ASK_CASES = [
    # (消息, mode, allow_bare, max_length, 期望)
    ("南霜的资料", "keyword", False, 120, True),
    # 管理指令不能被当成自然语言再查一遍
    ("收录配置图 南霜 [作者:佑栖] [备注:7级威力星纪]", "keyword", False, 120, False),
    ("配置备注 南霜 [备注:改一下]", "keyword", False, 120, False),
    ("帮我看看刻印都有哪些词条", "keyword", False, 120, True),
    ("属性克制表怎么看", "keyword", False, 120, True),
    ("今天天气不错", "keyword", False, 120, False),
    ("今天天气不错", "always", False, 120, True),
    ("雷伊的技能都有啥", "off", False, 120, False),
    # 严格指令 / 宽松指令能处理的都不该走 AI
    ("精灵 雷伊", "always", False, 120, False),
    ("精灵雷伊", "always", False, 120, False),
    ("雷伊精灵", "always", False, 120, False),
    ("刻印宝石 火", "always", False, 120, False),
    ("雷伊的技能都有啥", "keyword", False, 120, True),
    ("雷伊立绘", "keyword", False, 120, False),
    # 「雷伊的立绘长什么样」现在规则层就能解析（剥掉疑问尾巴），不再需要 AI
    ("雷伊的立绘长什么样", "keyword", False, 120, False),
    # 长度限制
    ("刻印" + "很" * 200, "keyword", False, 120, False),
    ("", "keyword", False, 120, False),
    # 裸名模式开启时，裸名已被宽松层接走
    ("雷伊", "always", True, 120, False),
    ("雷伊", "always", False, 120, True),
]

for text, mode, allow_bare, max_length, expected in ASK_CASES:
    got = should_ask_ai(text, mode=mode, allow_bare=allow_bare, max_length=max_length)
    if got != expected:
        failures.append(
            f"should_ask_ai({text!r}, mode={mode!r}, allow_bare={allow_bare}) "
            f"-> {got}，期望 {expected}"
        )

# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

system_prompt, user_prompt = build_messages("雷伊的技能都有啥")
for intent in parser.INTENTS:
    if intent.key not in system_prompt:
        failures.append(f"system prompt 缺少 intent: {intent.key}")
check("none" in system_prompt, "system prompt 缺少 none 选项")
check('{"intent"' in system_prompt, "system prompt 缺少 JSON 格式示例")
check("雷伊的技能都有啥" in user_prompt, "user prompt 缺少原始消息")

# 说明文案与指令表一一对应，避免以后新增指令忘记补描述
for intent in parser.INTENTS:
    if intent.key not in ai._INTENT_DESC:
        failures.append(f"_INTENT_DESC 缺少 {intent.key} 的说明")

result = ai.IntentResult("pet_info", "雷伊")
check(result.takes_arg, "IntentResult(pet_info).takes_arg 应为 True")
check(not ai.IntentResult("preview", "").takes_arg, "IntentResult(preview).takes_arg 应为 False")

# --------------------------------------------------------------------------- #
# 否认能力的判定
# --------------------------------------------------------------------------- #

DENIAL_TRUE = [
    "查不了，我这儿没接图鉴数据",
    "我没有数据",
    "这个真查不到",
    "接不了",
    "没有图鉴",
    "我这边没法查",
]
DENIAL_FALSE = [
    "❌未找到匹配的精灵: 南霜",
    "❌数据库未加载，请稍后再试",
    "能啊，发「精灵 雷伊」就行",
    "今天天气不错",
    "",
    "随便说点什么" * 40,
]
for text in DENIAL_TRUE:
    if not ai.looks_like_capability_denial(text):
        failures.append(f"looks_like_capability_denial({text!r}) 应为 True")
for text in DENIAL_FALSE:
    if ai.looks_like_capability_denial(text):
        failures.append(f"looks_like_capability_denial({text!r}) 应为 False")

# --------------------------------------------------------------------------- #
# 别的游戏不能套赛尔号的库
# --------------------------------------------------------------------------- #

OTHER_TRUE = [
    "原神的立绘",
    "宝可梦精灵有哪些",
    "洛克王国的精灵",
    "皮卡丘长啥样",
    "鸣潮的属性克制",
]
OTHER_FALSE = [
    "赛尔号原神联动",
    "精灵雷伊",
    "赛尔号宝可梦联动活动是什么",
    "今天天气不错",
]
for text in OTHER_TRUE:
    if not ai.mentions_other_game(text):
        failures.append(f"mentions_other_game({text!r}) 应为 True")
    if should_ask_ai(text, mode="keyword"):
        failures.append(f"should_ask_ai({text!r}) 应跳过（不是赛尔号）")
for text in OTHER_FALSE:
    if ai.mentions_other_game(text):
        failures.append(f"mentions_other_game({text!r}) 应为 False")

# --------------------------------------------------------------------------- #

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print(
    f"OK: {len(JSON_CASES)} 条 JSON 解析用例 + {len(NONE_CASES)} 条空结果用例 + "
    f"{len(ASK_CASES)} 条触发判断用例全部通过"
)
