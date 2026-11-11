import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("command_parser", ROOT / "core" / "command_parser.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

samples = [
    "精灵雷伊", "雷伊精灵", "雷伊的精灵", "查一下 雷伊", "查一下精灵雷伊",
    "赛尔号精灵雷伊", "精灵「雷伊」", "【精灵】雷伊",
    "精灵好强", "精灵怎么获得", "这只精灵", "这只精灵真强", "精灵的属性",
    "属性 火", "属性克制表火", "火 属性", "火克什么",
    "今天开服了吗", "下期预告", "预告", "赛尔号精灵好强",
    "布莱克", "不灭战神 精灵", "不灭战神精灵", "帮我查一下 雷伊",
    "刻印宝石火", "刻印 泰坦", "魂印雷伊", "技能雷伊", "精灵雷伊技能",
    "666", "雷伊666",
]
width = max(len(s) for s in samples)
for s in samples:
    q = m.parse_query(s, allow_bare=False)
    q2 = m.parse_query(s, allow_bare=True)
    a = "None" if q is None else f"{q.key}/{q.arg!r}"
    b = "None" if q2 is None else f"{q2.key}/{q2.arg!r}"
    strict = "STRICT" if m.matches_strict_command(s) else ""
    print(f"{s:<{width}}  宽松={a:<22} 裸名模式={b:<22} {strict}")
