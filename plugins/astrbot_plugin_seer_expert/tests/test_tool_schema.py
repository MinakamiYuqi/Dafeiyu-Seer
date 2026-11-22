# -*- coding: utf-8 -*-
"""@llm_tool 的参数 schema 必须和函数签名对齐。

★ 为什么单独一套（2026-11-16 加）：
  AstrBot 的 register_llm_tool **只遍历 docstring 的 Args 段**建参数 schema
  （core/astrbot/core/star/register/star_handler.py:657-694），完全不看函数签名。
  所以「Args 段没写 / 写漏 / 缩进错位」→ 注册出来的 schema 是空数组或少了参数，
  模型看不到参数名，只能从描述正文里瞎猜 —— 猜对能用，猜错就 TypeError。

  这种错在 audit_seer.py 里查不出来，在 AstrBot 日志里也只是一条 TypeError，
  原来的 audit_tool_docstrings.py 更是报「✅ 全部通过」（解析出 0 个参数就跳过检查）。

  改为复用 `_re/audit_tool_docstrings.py` 的 check_file，不复制逻辑。

注意两套目录布局：
  workspace 根目录     插件在 <root>/astrbot_plugin_seer_*
  seer-repo 目录       插件在 <root>/plugins/astrbot_plugin_seer_*
所以路径要探测，不能写死。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 两套目录布局都要兼容：
#   workspace   <root>/astrbot_plugin_seer_*         （parents[2] 就是 workspace 根）
#   seer-repo   <root>/plugins/astrbot_plugin_seer_* （parents[2] 是 plugins，还要再上一层）
# 写死相对层数会错，所以从本文件往上逐层找。
_ANCESTORS = list(Path(__file__).resolve().parents)

# 要找的插件入口文件（有 @llm_tool 的都在 main.py）
TARGETS = [
    "astrbot_plugin_seer_expert/main.py",
    "astrbot_plugin_seer_info/main.py",
    "astrbot_plugin_seer_dsh/main.py",
]


def _find(rel: str) -> Path | None:
    for base in _ANCESTORS:
        for p in (base / rel, base / "plugins" / rel):
            if p.is_file():
                return p
    return None


def _find_any(rel: str) -> Path | None:
    for base in _ANCESTORS:
        p = base / rel
        if p.is_file():
            return p
    return None


def _audit_path() -> Path | None:
    return _find_any("_re/audit_tool_docstrings.py")


@pytest.fixture(scope="module")
def audit_mod():
    path = _audit_path()
    if path is None:
        pytest.skip("找不到 _re/audit_tool_docstrings.py")
    spec = importlib.util.spec_from_file_location("audit_tool_docstrings", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rel", TARGETS)
def test_schema_matches_signature(audit_mod, rel):
    """签名里的每个参数，docstring 的 Args 段里都必须有。"""
    path = _find(rel)
    if path is None:
        pytest.skip(f"{rel} 不存在")
    _tools, bad, _warns = audit_mod.check_file(str(path))
    assert not bad, (
        f"{rel} 里这些 @llm_tool 的参数不会出现在 schema 里"
        f"（模型看不到，只能瞎猜）：\n" + "\n".join(f"  · {x}" for x in bad)
    )


def test_audit_catches_broken_docstrings(audit_mod):
    """反例文件必须被抓住 —— 否则说明审计脚本本身退化成假绿了。"""
    cand = [ROOT / "_re" / "_audit_selftest_bad.py",
            ROOT.parent / "_re" / "_audit_selftest_bad.py"]
    bad_case = next((p for p in cand if p.is_file()), None) or _find_any(
        "_re/_audit_selftest_bad.py")
    if bad_case is None:
        pytest.skip("反例文件不存在")

    tools, bad, _warns = audit_mod.check_file(str(bad_case))
    assert len(tools) == 4, f"反例文件应该有 4 个工具，实际 {len(tools)}"
    joined = "\n".join(bad)
    # ① 完全没写 Args 段
    assert "bad_tool" in joined and "'pet'" in joined
    # ② 只写了一半
    assert "half_tool" in joined and "'b'" in joined
    # ③ 类型不支持
    assert "bad_type" in joined
    # ④ 对照组不该出现在问题里
    assert "good_tool" not in joined, "正常工具被误报了"


def test_llm_tool_count(audit_mod):
    """工具总数只减不增时提醒一句（防止有人误删工具）。"""
    total = 0
    for rel in TARGETS:
        path = _find(rel)
        if path is None:
            continue
        tools, _b, _w = audit_mod.check_file(str(path))
        total += len(tools)
    if total == 0:
        pytest.skip("一个插件入口都没找到")
    assert total >= 40, f"@llm_tool 数量掉到 {total}，低于 2026-11-16 的 40 个"
