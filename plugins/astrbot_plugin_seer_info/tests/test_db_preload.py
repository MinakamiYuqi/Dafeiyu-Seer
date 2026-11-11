r"""验证「数据库先用本地文件顶上」这个修复（需要 AstrBot 环境 + 本地库）。

背景：`register_database()` 原本只挂一个后台同步任务，要等下载完（25 MB，
实测 ~120 KB/s 约 3.6 分钟）才 `load_from_file()`。这段窗口里
`is_database_loaded()` 为 False，用户发指令会收到「数据库未加载，请稍后再试」；
如果下载失败，更是永远等不到。修复后：注册时先加载本地库，再后台更新。

运行：

    set ASTRBOT_ROOT=<实例>\core
    <实例>\venv\Scripts\python.exe tests\test_db_preload.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("ASTRBOT_ROOT") or os.getcwd()).resolve()
DB_PATH = ROOT / "data" / "plugin_data" / "astrbot_plugin_seer_info" / "seerapi.sqlite"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
    print(("  ok   " if cond else "  FAIL ") + msg)


try:
    import astrbot  # noqa: F401
except Exception as exc:
    print(f"SKIP: 当前解释器没有 astrbot（{exc}）")
    sys.exit(0)

if not DB_PATH.exists():
    print(f"SKIP: 找不到 {DB_PATH}")
    sys.exit(0)

_pkg = types.ModuleType("astrbot_plugin_seer_info")
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_pkg.__name__] = _pkg

# 按插件真实加载顺序先导入 main.py（它先 import .core 再 import .data），
# 直接单独导入 data.py 会踩到 core 与 data 的循环导入
_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_seer_info.main", PLUGIN_DIR / "main.py"
)
_main = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _main
_spec.loader.exec_module(_main)

data = sys.modules["astrbot_plugin_seer_info.data"]


async def main() -> None:
    from sqlalchemy import text

    db = data.db_manager
    db.dispose_all()

    # 让后台同步变成空操作：本测试只关心「注册时是否已经可用」，不想触发真实下载
    synced: list[str] = []

    async def fake_sync(name, *args, **kwargs):
        synced.append(name)
        return "skipped"

    data.sync_database = fake_sync

    print("\n[1] register_database（配置了非空 sync_url，模拟线上）")
    data.register_database(
        "seerapi",
        sync_url="https://example.invalid/seerapi-data.sqlite",
        sync_interval_minutes=60,
    )
    # 关键断言：register_database 返回时（还没 await 过任何东西）就必须可用
    check(db.is_database_loaded("seerapi"), "register_database 返回时数据库已就绪")

    session = db.get_session("seerapi")
    count = session.connection().execute(text("select count(*) from pet")).scalar()
    check(count and count > 1000, f"本地库可用（pet 表 {count} 行）")

    print("\n[2] 后台同步任务仍然在跑")
    await asyncio.sleep(0.1)
    check(synced == ["seerapi"], f"后台仍会检查更新（调用 {synced}）")
    check("seerapi" in data._sync_specs, "同步配置（URL）仍然保留")

    await data.cancel_sync_tasks()
    db.dispose_all()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)
print("OK: 本地库预加载生效，启动/重载不再出现「数据库未加载」")
