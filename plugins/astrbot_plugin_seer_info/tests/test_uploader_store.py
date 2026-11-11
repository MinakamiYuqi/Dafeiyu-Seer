"""白名单存储的离线测试。

运行：python tests/test_uploader_store.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "uploader_store", ROOT / "core" / "uploader_store.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

parse_uploader_config = mod.parse_uploader_config
UploaderStore = mod.UploaderStore

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---- 配置解析 ----
check(
    parse_uploader_config("F81DAEACBA6205676130A3271EEDE726") == {"F81DAEACBA6205676130A3271EEDE726": ""},
    "纯 UID",
)
check(
    parse_uploader_config("佑栖=F81DAEACBA6205676130A3271EEDE726")
    == {"F81DAEACBA6205676130A3271EEDE726": "佑栖"},
    "昵称=UID",
)
check(
    parse_uploader_config("佑栖：F81DAEACBA6205676130A3271EEDE726")["F81DAEACBA6205676130A3271EEDE726"] == "佑栖",
    "全角冒号",
)
parsed = parse_uploader_config(
    "佑栖=F81DAEACBA6205676130A3271EEDE726\n霜宝=987086EEA0FF74D90EADA6FFD6EA3B92，某人=ABC123DEF456"
)
check(len(parsed) == 3, f"多行/逗号混合（实际 {len(parsed)}）")
check(parsed.get("987086EEA0FF74D90EADA6FFD6EA3B92") == "霜宝", "第二行昵称")
check(parsed.get("ABC123DEF456") == "某人", "全角逗号分隔")
check(parse_uploader_config("# 注释\n\n") == {}, "注释和空行")
check(parse_uploader_config("") == {}, "空串")

# ---- 白名单操作 ----
store = UploaderStore()
store.load_config("佑栖=F81DAEACBA6205676130A3271EEDE726")
check(store.contains("F81DAEACBA6205676130A3271EEDE726"), "配置里的 UID 命中")
check(not store.contains("别人"), "不在白名单的不命中")
check(store.label("F81DAEACBA6205676130A3271EEDE726") == "佑栖（F81DAEACBA6205676130A3271EEDE726）", "标签带昵称")

check(store.add("NEWUID123456", "新人"), "添加新成员")
check(store.contains("NEWUID123456"), "添加后命中")
check(not store.add("NEWUID123456", "新人"), "重复添加返回 False")
store.remove("NEWUID123456")
check(not store.contains("NEWUID123456"), "移除后不命中")

# ---- 候选池 ----
store.touch("A1", "甲")
store.touch("A1", "甲")
store.touch("B2", "乙")
check(store.candidates["A1"].count == 2, "计数累加")
check(store.candidates["A1"].name == "甲", "记住昵称")
top = store.top(10)
check(top[0].uid == "A1" and top[1].uid == "B2", "按次数排序")

# ---- 落盘与重新加载 ----
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "nested" / "uploaders.json"
    s1 = UploaderStore(path=path)
    s1.load_config("佑栖=F81DAEACBA6205676130A3271EEDE726")
    s1.touch("A1", "甲")
    s1.add("MANUAL1", "手动")
    s1.save()
    check(path.is_file(), "保存后文件存在")

    s2 = UploaderStore(path=path)
    s2.load()
    check(s2.contains("MANUAL1"), "手动加的白名单能恢复")
    check(s2.candidates["A1"].count == 1, "候选池能恢复")
    s2.load_config("霜宝=987086EEA0FF74D90EADA6FFD6EA3B92")
    check(s2.contains("987086EEA0FF74D90EADA6FFD6EA3B92"), "配置条目加载")
    check(s2.contains("MANUAL1"), "配置加载不清掉手动条目")
    check(s2.members["987086EEA0FF74D90EADA6FFD6EA3B92"] == "霜宝", "配置昵称生效")

# ---- 候选池上限 ----
big = UploaderStore()
for index in range(520):
    big.touch(f"U{index:04d}", f"n{index}")
check(len(big.candidates) <= 500, f"候选池应被裁剪（实际 {len(big.candidates)}）")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print("OK: 白名单解析/增删/候选池/落盘用例全部通过")
