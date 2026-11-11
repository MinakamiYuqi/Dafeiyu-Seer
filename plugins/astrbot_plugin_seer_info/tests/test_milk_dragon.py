"""奶龙图库存储的离线测试。

运行：python tests/test_milk_dragon.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "milk_dragon", ROOT / "core" / "milk_dragon.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

MilkDragonStore = mod.MilkDragonStore

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


with tempfile.TemporaryDirectory() as tmp:
    store = MilkDragonStore(Path(tmp) / "milk")
    store.load()
    check(store.all() == [], "空目录加载后没有条目")

    first = store.add("奶政", 3466, "始皇帝·嬴政", b"PNGDATA1", ".png", uploader="u1")
    check(first.name == "奶政" and first.pet_id == 3466, "新增条目字段")
    check(first.label() == "奶政（始皇帝·嬴政）", "展示文案")
    check(first.file == "3466_奶政.png", f"文件名规则（实际 {first.file}）")
    check(store.path_of(first) is not None, "图片文件已写入")

    second = store.add("奶霜", 4511, "不破帝·南霜", b"PNGDATA2", ".png")
    check([e.name for e in store.all()] == ["奶政", "奶霜"], "顺序按加入先后")

    # 同名覆盖：换图但保留顺序
    third = store.add("奶政", 3466, "始皇帝·嬴政", b"NEWBYTES", ".jpg")
    check(len(store.all()) == 2, "同名不会新增条目")
    check(store.all()[0].file.endswith(".jpg"), "同名换图更新文件名")
    check(store.all()[0].added_at and store.all()[0].added_at == third.added_at, "覆盖时刷新时间")
    check(not (store.root / "3466_奶政.png").exists(), "旧文件被清掉")

    # 名字相近但不是同名，不能互相覆盖
    store.add('奶盖', 4766, '灭战神决·盖亚', b'A', '.png')
    store.add('宿命的奶盖', 4766, '灭战神决·盖亚', b'B', '.png')
    check(len(store.all()) == 4, f'相近名字各自独立（实际 {len(store.all())}）')

    # 查找
    check(store.find("奶霜") is not None, "精确查找")
    check(store.find("奶") is not None, "模糊查找（子串）")
    check(store.find("不存在") is None, "查不到返回 None")

    # 非法文件名清洗
    dirty = store.add("奶/政:测试*?<>|", 1, "某精灵", b"X", ".png")
    check("/" not in dirty.file and ":" not in dirty.file, f"文件名已清洗（{dirty.file}）")

    # 落盘与重新加载
    store.save()
    reloaded = MilkDragonStore(Path(tmp) / "milk")
    reloaded.load()
    check(len(reloaded.all()) == 5, "重新加载条目数一致")
    check(reloaded.all()[0].name == "奶政", "重新加载保持顺序")
    check(reloaded.path_of(reloaded.all()[0]) is not None, "重新加载能找到图片")

    # 删除
    check(reloaded.remove("奶霜"), "删除存在的条目")
    check(reloaded.find("奶霜") is None, "删除后查不到")
    check(not reloaded.remove("奶霜"), "重复删除返回 False")

    count, total = reloaded.stats()
    check(count == 4 and total > 0, f"统计（{count} 张 / {total} 字节）")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print("OK: 奶龙图库 新增/覆盖/查找/清洗/落盘/删除 用例全部通过")
