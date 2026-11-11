"""渲染结果缓存（纯标准库，可离线测试）。

思路借鉴上游 IronsBot 的 ``FileRenderCache``：
把「渲染输入（HTML）+ 输出格式 + 模板/渲染器源码指纹」哈希成 key，
命中就直接返回上次的图片，省掉 Playwright 截图和资源下载。

和上游的差别：那边按「渲染文档」算 key，我们这里更粗暴——直接对最终 HTML
取哈希。反正 HTML 已经是数据的完整投影，模板或数据一变 key 自然就变了。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["cache_key", "source_fingerprint", "RenderCache"]


def cache_key(*parts: object) -> str:
    """把若干片段拼成稳定的短哈希。"""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def source_fingerprint(paths) -> str:
    """模板/渲染器源码指纹：文件增删改都会让它变化。

    用「相对路径 + 大小 + mtime」而不是内容哈希——目录不大，但每次渲染都
    读一遍所有模板没必要。
    """
    chunks: list[str] = []
    for path in sorted(Path(p) for p in paths):
        try:
            if path.is_dir():
                files = sorted(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                files = [path]
            else:
                continue
        except OSError:
            continue
        for item in files:
            try:
                stat = item.stat()
            except OSError:
                continue
            chunks.append(f"{item}|{stat.st_size}|{int(stat.st_mtime)}")
    return cache_key(*chunks)


class RenderCache:
    """按 mtime 做 LRU 的文件缓存。"""

    def __init__(self, root: Path | str, max_bytes: int):
        self._root = Path(root)
        self._max_bytes = max(0, int(max_bytes))

    @property
    def root(self) -> Path:
        return self._root

    @property
    def enabled(self) -> bool:
        return self._max_bytes > 0

    def _path(self, key: str, ext: str) -> Path:
        return self._root / f"{key}{ext}"

    def get(self, key: str, ext: str = ".jpeg") -> bytes | None:
        if not self.enabled:
            return None
        path = self._path(key, ext)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        # 命中就刷新 mtime，维持「最近使用」的顺序
        try:
            path.touch()
        except OSError:
            pass
        return data

    def put(self, key: str, ext: str, data: bytes) -> bool:
        if not self.enabled or not data:
            return False
        if len(data) > self._max_bytes:
            return False
        path = self._path(key, ext)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            return False
        self.evict()
        return True

    def evict(self) -> int:
        """超出容量时按最久未使用的顺序删，返回删除的字节数。"""
        if not self.enabled:
            return 0
        try:
            files = [p for p in self._root.iterdir() if p.is_file()]
        except OSError:
            return 0
        entries = []
        total = 0
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= self._max_bytes:
            return 0
        freed = 0
        for _, size, path in sorted(entries):
            if total - freed <= self._max_bytes:
                break
            try:
                path.unlink()
                freed += size
            except OSError:
                continue
        return freed

    def clear(self) -> int:
        freed = 0
        try:
            files = [p for p in self._root.iterdir() if p.is_file()]
        except OSError:
            return 0
        for path in files:
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError:
                continue
        return freed

    def stats(self) -> tuple[int, int]:
        """(文件数, 总字节)。"""
        count = 0
        total = 0
        try:
            files = [p for p in self._root.iterdir() if p.is_file()]
        except OSError:
            return 0, 0
        for path in files:
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
        return count, total
