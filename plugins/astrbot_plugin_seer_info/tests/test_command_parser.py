"""宽松指令解析器的离线测试（不依赖 AstrBot、不依赖数据库）。

运行：python tests/test_command_parser.py
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "core" / "command_parser.py"

spec = importlib.util.spec_from_file_location("command_parser", MODULE_PATH)
parser = importlib.util.module_from_spec(spec)
# dataclass 需要能在 sys.modules 里找到本模块
sys.modules[spec.name] = parser
spec.loader.exec_module(parser)

parse_query = parser.parse_query
matches_strict_command = parser.matches_strict_command

failures: list[str] = []


def expect(text: str, key: str | None, arg: str = "", bare: bool = False) -> None:
    query = parse_query(text, allow_bare=bare)
    got_key = None if query is None else query.key
    got_arg = "" if query is None else query.arg
    ok = got_key == key and (key is None or got_arg == arg)
    if not ok:
        failures.append(
            f"parse_query({text!r}, allow_bare={bare}) -> "
            f"{(got_key, got_arg)!r}, expected {(key, arg)!r}"
        )


CASES = [
    # ---- 原版就支持的规范写法 ----
    ("精灵 雷伊", "pet_info", "雷伊"),
    ("精灵 雷伊 技能", "pet_info", "雷伊"),
    ("魂印 雷伊", "pet_info", "雷伊"),
    ("技能 雷伊", "pet_info", "雷伊"),
    ("查询精灵信息 雷伊", "pet_info", "雷伊"),
    ("立绘 雷伊", "pet_image", "雷伊"),
    ("皮肤 雷伊", "pet_image", "雷伊"),
    ("属性 火", "type_matchup", "火"),
    ("异常 烧伤", "battle_effect", "烧伤"),
    ("刻印 泰坦", "mintmark", "泰坦"),
    ("宝石 火", "gem", "火"),
    ("套装 银翼", "suit", "银翼"),
    ("部件 头盔", "equip", "头盔"),
    ("称号 铁皮", "title", "铁皮"),
    ("外号 雷伊", "alias", "雷伊"),
    ("别名 雷伊", "alias", "雷伊"),
    ("雷伊的外号", "alias", "雷伊"),
    ("雷伊别名", "alias", "雷伊"),
    ("下周预告", "preview"),
    ("开服了吗", "server"),
    ("帮助", "help"),
    # ---- 无空格 / 全角标点 ----
    ("精灵雷伊", "pet_info", "雷伊"),
    ("精灵：雷伊", "pet_info", "雷伊"),
    ("精灵:雷伊", "pet_info", "雷伊"),
    ("精灵　　雷伊", "pet_info", "雷伊"),
    ("精灵，雷伊", "pet_info", "雷伊"),
    ("精灵「雷伊」", "pet_info", "雷伊"),
    ("【精灵】雷伊", "pet_info", "雷伊"),
    ("魂印雷伊", "pet_info", "雷伊"),
    # ---- 名称在前 ----
    ("雷伊精灵", "pet_info", "雷伊"),
    ("雷伊 精灵", "pet_info", "雷伊"),
    ("雷伊的精灵", "pet_info", "雷伊"),
    ("雷伊精灵信息", "pet_info", "雷伊"),
    ("雷伊的属性", "type_matchup", "雷伊"),
    ("火 属性", "type_matchup", "火"),
    # ---- 配置图 ----
    ("配置图 雷伊", "pet_config", "雷伊"),
    ("配置 雷伊", "pet_config", "雷伊"),
    ("雷伊配置图", "pet_config", "雷伊"),
    ("雷伊的配置图", "pet_config", "雷伊"),
    ("精灵配置图 雷伊", "pet_config", "雷伊"),
    # ---- 修饰词 ----
    ("查精灵雷伊", "pet_info", "雷伊"),    ("查一下精灵 雷伊", "pet_info", "雷伊"),
    ("帮我查一下精灵雷伊", "pet_info", "雷伊"),
    ("帮我查一下 雷伊", None),
    ("帮我查一下 雷伊", "pet_info", "雷伊", True),
    ("赛尔号精灵雷伊", "pet_info", "雷伊"),
    ("赛尔号 精灵 雷伊", "pet_info", "雷伊"),
    ("查询一下雷伊精灵", "pet_info", "雷伊"),
    ("查询雷伊的精灵信息", "pet_info", "雷伊"),
    ("/精灵 雷伊", "pet_info", "雷伊"),
    ("！精灵雷伊", "pet_info", "雷伊"),
    # ---- 请求动词（发/来/整/弄/给 + 量词）----
    ("给我发一下南霜的立绘", "pet_image", "南霜"),
    ("发一下南霜的立绘", "pet_image", "南霜"),
    ("帮我发一张南霜立绘", "pet_image", "南霜"),
    ("来一张南霜的皮肤", "pet_image", "南霜"),
    ("发我南霜的立绘", "pet_image", "南霜"),
    ("给我来个南霜的立绘", "pet_image", "南霜"),
    ("弄个南霜立绘", "pet_image", "南霜"),
    ("给个南霜立绘", "pet_image", "南霜"),
    ("帮我看下南霜立绘", "pet_image", "南霜"),
    ("查下南霜的立绘", "pet_image", "南霜"),
    ("南霜立绘", "pet_image", "南霜"),
    # 以这些字开头的名字不能被当成请求动词吃掉
    ("上古炎兽的立绘", "pet_image", "上古炎兽"),
    ("发条鸟的立绘", "pet_image", "发条鸟"),
    ("来古的立绘", "pet_image", "来古"),
    # ---- 「魂印」类说法（灵魂印、句子形式）----
    ("魂印雷伊", "pet_info", "雷伊"),
    ("雷伊的魂印", "pet_info", "雷伊"),
    ("雷伊的魂印是什么", "pet_info", "雷伊"),
    ("雷伊的魂印有哪些", "pet_info", "雷伊"),
    ("雷伊的魂印有什么用", "pet_info", "雷伊"),
    ("雷伊魂印效果", "pet_info", "雷伊"),
    ("魂印效果雷伊", "pet_info", "雷伊"),
    ("魂印技能雷伊", "pet_info", "雷伊"),
    ("查一下雷伊的魂印", "pet_info", "雷伊"),
    ("南霜魂印", "pet_info", "南霜"),
    # 疑问尾巴不能把正常句子误伤
    ("属性克制表怎么看", None),
    ("火克什么", None),

    # ---- 长词优先 ----
    ("刻印宝石 火", "gem", "火"),
    ("刻印宝石火", "gem", "火"),
    ("属性克制表 火", "type_matchup", "火"),
    ("克制 火", "type_matchup", "火"),
    ("下期预告", "preview"),
    ("今天开服了吗", "server"),
    ("开服", "server"),
    # ---- 只给指令词，交给 handler 提示用法 ----
    ("属性表", "type_matchup", ""),
    ("精灵", "pet_info", ""),
    ("刻印", "mintmark", ""),
    # ---- 剩余部分是另一个指令词：按那个指令处理 ----
    ("精灵的属性", "type_matchup", ""),
    ("精灵 属性", "type_matchup", ""),
    ("精灵的技能", "pet_info", ""),
    # ---- 闲聊不应误触发 ----
    ("这只精灵", None),
    ("这只精灵真强", None),
    ("精灵好强", None),
    ("精灵怎么样", None),
    ("火克什么", None),
    ("今天精灵怎么样", None),
    ("雷伊", None),
    ("雷伊", "pet_info", "雷伊", True),
    ("123456", None),
    ("123456", "pet_info", "123456", True),
    ("", None),
    ("   ", None),
    ("？", None),
]

for case in CASES:
    expect(*case)


# --------------------------------------------------------------------------- #
# 严格匹配判断：必须与 AstrBot CommandFilter 的语义一致
# --------------------------------------------------------------------------- #

STRICT_TRUE = [
    "精灵 雷伊",
    "精灵",
    "精灵  雷伊",
    "精灵　　雷伊",
    "魂印 雷伊",
    "查询精灵信息 雷伊",
    "刻印宝石 火",
    "开服了吗",
    "  帮助  ",
]

STRICT_FALSE = [
    "精灵雷伊",
    "查精灵雷伊",
    "雷伊精灵",
    "雷伊 精灵",
    "精灵：雷伊",
    "帮助下",
    "精灵雷伊 技能",
]

for text in STRICT_TRUE:
    if not matches_strict_command(text):
        failures.append(f"matches_strict_command({text!r}) -> False, expected True")

for text in STRICT_FALSE:
    if matches_strict_command(text):
        failures.append(f"matches_strict_command({text!r}) -> True, expected False")


# --------------------------------------------------------------------------- #
# 一致性：main.py 里注册的指令名/别名 必须与指令表完全一致
# --------------------------------------------------------------------------- #


# 管理类指令不进宽松指令表（不需要自然语言识别），比对时排除
ADMIN_ONLY_COMMANDS = {
    "收录配置图",
    "配置备注",
    "白名单",
    "上传白名单",
    "奶龙精灵查询",
    "奶龙查询",
    "上传奶龙图",
}


def registered_commands() -> set[str]:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "command"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        for kw in node.keywords:
            if kw.arg != "alias" or not isinstance(kw.value, ast.Set):
                continue
            for elt in kw.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
    return names - ADMIN_ONLY_COMMANDS


declared = set(parser.strict_command_names())
registered = registered_commands()
if declared != registered:
    failures.append(
        "指令表与 main.py 注册不一致："
        f"仅在指令表={sorted(declared - registered)}，"
        f"仅在 main.py={sorted(registered - declared)}"
    )


# --------------------------------------------------------------------------- #

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print(f"OK: {len(CASES)} 条解析用例 + {len(STRICT_TRUE) + len(STRICT_FALSE)} 条严格匹配用例全部通过")
print(f"指令一致性检查通过，共 {len(declared)} 个严格指令名")
