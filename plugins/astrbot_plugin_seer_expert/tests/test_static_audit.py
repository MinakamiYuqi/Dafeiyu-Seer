# -*- coding: utf-8 -*-
"""静态审计：把 `_re/audit_seer.py` 的四项检查纳入 pytest。

① self.kb.X() / k.X() 调用了 db.py 里不存在的方法
② hasattr(self, "X") 守卫 —— X 真不存在时静默返回默认值（type_name_of 那类坑）
③ @llm_tool / @command 名字重复注册
④ 工作区 vs 运行版是否一致

检查逻辑复用 `_re/audit_seer.py`，不复制一份，避免两边慢慢漂移。
CLI 入口照旧：`python _re/audit_seer.py`
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "_re" / "audit_seer.py"

# (相对路径, 展示名)
TARGETS = [
    ("astrbot_plugin_seer_expert/main.py", "expert/main.py"),
    ("seer-cli/seer.py", "seer-cli/seer.py"),
    ("astrbot_plugin_seer_dsh/main.py", "dsh/main.py"),
    ("astrbot_plugin_seer_info/main.py", "info/main.py"),
    ("astrbot_plugin_seer_info/service.py", "info/service.py"),
]


@pytest.fixture(scope="module")
def audit():
    if not AUDIT_PATH.is_file():
        pytest.skip(f"找不到 {AUDIT_PATH}")
    spec = importlib.util.spec_from_file_location("audit_seer", AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def kb_ok(audit):
    return audit.kb_members()


def _path(rel: str) -> Path:
    p = ROOT / rel
    if not p.is_file():
        pytest.skip(f"{rel} 不存在")
    return p


# ── ① 未定义方法 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("rel,label", TARGETS)
def test_no_undefined_kb_calls(audit, kb_ok, rel, label):
    issues = audit.check_kb_calls(str(_path(rel)), kb_ok, label)
    assert not issues, "调用了 db.py 不存在的方法：\n" + "\n".join(
        f"  L{line}  {msg}" for _, line, msg in issues
    )


# ── ② hasattr 守卫陷阱 ───────────────────────────────────────────────────
@pytest.mark.parametrize("rel,label", TARGETS)
def test_no_hasattr_traps(audit, rel, label):
    issues = audit.hasattr_guards(str(_path(rel)), label)
    assert not issues, "hasattr 守着一个不存在的属性（会静默返回默认值）：\n" + "\n".join(
        f"  L{line}  hasattr(self, {name!r})" for _, line, name in issues
    )


# ── ③ 工具名重复 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("rel,label", TARGETS)
def test_no_duplicate_tools(audit, rel, label):
    issues = audit.dup_tools(str(_path(rel)), label)
    assert not issues, "工具/命令名重复注册：\n" + "\n".join(
        f"  L{line}  {msg}" for _, line, msg in issues
    )


# ── ④ 工作区 vs 运行版 ───────────────────────────────────────────────────
def test_workspace_matches_runtime(audit):
    """运行版目录不存在时（换机器 / 没装 AstrBot）自动跳过。"""
    run_root = Path(audit.RUN)
    if not run_root.is_dir():
        pytest.skip(f"运行版目录不存在：{run_root}")

    diffs = []
    for plugin in (audit.EXPERT, audit.INFO, audit.DSH):
        ws, rs = Path(audit.WORK) / plugin, run_root / plugin
        if not ws.is_dir() or not rs.is_dir():
            continue
        for p1 in ws.rglob("*.py"):
            if "__pycache__" in p1.parts or "tests" in p1.parts:
                continue
            rel = p1.relative_to(ws)
            p2 = rs / rel
            if not p2.is_file():
                diffs.append(f"{plugin}/{rel} 运行版缺失")
            elif p1.read_bytes() != p2.read_bytes():
                diffs.append(f"{plugin}/{rel} 内容不同")

    assert not diffs, "工作区和运行版没同步：\n" + "\n".join(f"  {d}" for d in diffs)
