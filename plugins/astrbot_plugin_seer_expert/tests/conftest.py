# -*- coding: utf-8 -*-
"""pytest 公共夹具：把插件本体和真实数据库接进来。

数据路径来源（优先级从高到低）：
1. 环境变量 SEER_DATA_DIR
2. 本机 AstrBot 实例的 plugin_data（默认值，改机器时改这里）
3. 都找不到 → 自动 skip，不让缺数据把整套测试变红

插件代码始终加载 **被测试的那份**（本文件所在目录的上一级），
所以「改工作区 → 跑测试」验的就是工作区，不用先同步到运行版。
"""
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent

# 让 `import db` 命中本目录的插件代码，而不是别的路径下的同名模块
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

DEFAULT_DATA_DIR = Path(
    r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47\core\data\plugin_data"
)


def _resolve_data_dir() -> Path:
    env = os.environ.get("SEER_DATA_DIR", "").strip()
    return Path(env) if env else DEFAULT_DATA_DIR


@pytest.fixture(scope="session")
def data_dir() -> Path:
    d = _resolve_data_dir()
    seer = d / "astrbot_plugin_seer_info" / "seerapi.sqlite"
    if not seer.is_file():
        pytest.skip(f"seerapi.sqlite 不在 {d}，设置 SEER_DATA_DIR 指向真实数据目录")
    return d


@pytest.fixture(scope="session")
def k(data_dir):
    """真实只读数据源上的 SeerKnowledge 实例。"""
    import db as db_mod

    return db_mod.SeerKnowledge(
        str(data_dir / "astrbot_plugin_seer_info" / "seerapi.sqlite"),
        str(data_dir / "astrbot_plugin_seer_expert" / "kb.sqlite"),
    )


@pytest.fixture()
def kb_copy(tmp_path, data_dir):
    """kb.sqlite 的临时副本 + 已建好的 SeerKnowledge，用来测写路径不碰真数据。"""
    import shutil

    import db as db_mod

    src = data_dir / "astrbot_plugin_seer_expert" / "kb.sqlite"
    if not src.is_file():
        pytest.skip(f"kb.sqlite 不在 {data_dir}")
    dst = tmp_path / "kb.sqlite"
    shutil.copy2(src, dst)
    inst = db_mod.SeerKnowledge(
        str(data_dir / "astrbot_plugin_seer_info" / "seerapi.sqlite"), str(dst)
    )
    yield inst
    try:
        inst._kb.close()
    except Exception:
        pass
