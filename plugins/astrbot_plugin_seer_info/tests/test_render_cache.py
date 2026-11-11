"""渲染缓存的离线测试。

运行：python tests/test_render_cache.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "render_cache", ROOT / "core" / "render_cache.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

cache_key = mod.cache_key
source_fingerprint = mod.source_fingerprint
RenderCache = mod.RenderCache

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---- cache_key 稳定性与区分度 ----
check(cache_key("a", "b") == cache_key("a", "b"), "同样的输入应得到同样的 key")
check(cache_key("a", "b") != cache_key("ab", ""), "拼接不能产生歧义")
check(cache_key("a", "b") != cache_key("b", "a"), "顺序不同应得到不同 key")
check(len(cache_key("x")) == 32, "key 长度为 32")

# ---- source_fingerprint：文件变化要让指纹变 ----
with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    a = tmp_path / "a.j2"
    a.write_text("hello", encoding="utf-8")
    first = source_fingerprint([tmp_path])
    check(first == source_fingerprint([tmp_path]), "没有改动时指纹应稳定")

    time.sleep(0.01)
    a.write_text("hello world", encoding="utf-8")
    check(source_fingerprint([tmp_path]) != first, "文件内容变化后指纹应改变")

    b = tmp_path / "b.j2"
    b.write_text("x", encoding="utf-8")
    check(source_fingerprint([tmp_path]) != first, "新增文件后指纹应改变")

    before = source_fingerprint([tmp_path])
    b.unlink()
    check(source_fingerprint([tmp_path]) != before, "删除文件后指纹应改变")

# ---- 缓存读写 ----
with tempfile.TemporaryDirectory() as tmp:
    cache = RenderCache(Path(tmp) / "cache", 10_000)
    key = cache_key("html", "jpeg", 85)
    check(cache.get(key) is None, "空缓存应返回 None")
    check(cache.put(key, ".jpeg", b"image-bytes"), "写入应成功")
    check(cache.get(key) == b"image-bytes", "命中应返回原始字节")
    check(cache.stats() == (1, len(b"image-bytes")), "统计应正确")

    # 超过单条容量的直接拒绝
    big = RenderCache(Path(tmp) / "cache2", 10)
    check(not big.put(cache_key("big"), ".jpeg", b"x" * 100), "超容量单条不应写入")
    check(big.get(cache_key("big")) is None, "被拒绝的条目不该存在")

    # 关闭缓存
    off = RenderCache(Path(tmp) / "cache3", 0)
    check(not off.enabled, "max_bytes=0 视为关闭")
    check(not off.put(cache_key("k"), ".jpeg", b"data"), "关闭时写入直接失败")
    check(off.get(cache_key("k")) is None, "关闭时读取返回 None")

# ---- LRU 淘汰 ----
with tempfile.TemporaryDirectory() as tmp:
    cache = RenderCache(Path(tmp) / "cache", 30)
    for index in range(3):
        cache.put(cache_key("k", index), ".jpeg", b"0123456789")  # 每个 10 字节
        time.sleep(0.01)
    check(cache.stats()[0] == 3, "三个条目都在")
    # 加第四个，总量 40 > 30，最旧的应被淘汰
    cache.put(cache_key("k", 3), ".jpeg", b"0123456789")
    count, total = cache.stats()
    check(count == 3 and total <= 30, f"超容量应淘汰最旧的（实际 {count} 个 / {total} 字节）")
    check(cache.get(cache_key("k", 0)) is None, "最旧的条目应被删除")
    check(cache.get(cache_key("k", 3)) is not None, "最新的条目应保留")

# ---- 淘汰顺序按最近使用 ----
with tempfile.TemporaryDirectory() as tmp:
    cache = RenderCache(Path(tmp) / "cache", 25)
    cache.put(cache_key("a"), ".jpeg", b"0123456789")
    time.sleep(0.01)
    cache.put(cache_key("b"), ".jpeg", b"0123456789")
    time.sleep(0.01)
    cache.get(cache_key("a"))  # 访问 a，让 b 变成最旧
    time.sleep(0.01)
    cache.put(cache_key("c"), ".jpeg", b"0123456789")
    check(cache.get(cache_key("a")) is not None, "刚访问过的应保留")
    check(cache.get(cache_key("b")) is None, "最久未使用的应被淘汰")

# ---- clear ----
with tempfile.TemporaryDirectory() as tmp:
    cache = RenderCache(Path(tmp) / "cache", 1000)
    cache.put(cache_key("x"), ".jpeg", b"123")
    freed = cache.clear()
    check(freed == 3, f"clear 应返回释放字节数（实际 {freed}）")
    check(cache.stats() == (0, 0), "clear 后应为空")

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print("OK: 渲染缓存用例全部通过（key/指纹/读写/关闭/LRU/clear）")
