"""自定义外号配置解析（纯标准库）。

配置里写：

    二哈=雷伊
    奶妈=谱尼
    南霜=4511

支持 `=` / `:` / `：` 分隔，多行或用 `;` `；` `,` `，` 隔开，`#` 开头是注释。
"""

from __future__ import annotations

__all__ = ["parse_alias_config"]

_SEPARATORS = ("=", ":", "：")
_SPLIT_CHARS = "\n;；,，"


def parse_alias_config(text: str) -> dict[str, str]:
    """把配置文本解析成 {外号: 目标名或ID}，重复时后者覆盖前者。"""
    mapping: dict[str, str] = {}
    if not text:
        return mapping

    for raw_line in str(text).splitlines():
        for chunk in _split(raw_line):
            item = chunk.strip()
            if not item or item.startswith("#"):
                continue
            alias, target = _split_pair(item)
            alias = alias.strip().strip("\"'“”‘’")
            target = target.strip().strip("\"'“”‘’")
            if not alias or not target or alias == target:
                continue
            mapping[alias] = target
    return mapping


def _split(line: str) -> list[str]:
    out = [line]
    for ch in _SPLIT_CHARS:
        if ch == "\n":
            continue
        next_out: list[str] = []
        for part in out:
            next_out.extend(part.split(ch))
        out = next_out
    return out


def _split_pair(item: str) -> tuple[str, str]:
    for sep in _SEPARATORS:
        if sep in item:
            alias, _, target = item.partition(sep)
            return alias, target
    return "", ""
