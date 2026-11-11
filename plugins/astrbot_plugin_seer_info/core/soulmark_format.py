"""魂印富文本解析（纯标准库，可离线测试）。

赛尔号的魂印描述里带一套排版标记：

    <indent=0><sprite=0><indent=16>登场效果：
    <indent=16><sprite=3><indent=32>每次登场时令<b>双方</b><color=#64F9FA>全属性+1</color>

`indent` 决定层级（每 16 一级），`sprite` 是图标编号，`<b>`/`<color=#xxx>` 是样式。
这里把它解析成「小节 + 缩进行」，渲染时就能像游戏里那样分块显示。
"""

from __future__ import annotations

import html
import re

__all__ = ["parse_rich_soulmark", "strip_markup"]

_TAG_RE = re.compile(r"<(indent|sprite)=(\d+)>")
_STRIP_RE = re.compile(
    r"<(?:indent|sprite)=\d+>|<color=#[0-9A-Fa-f]{3,8}>|</color>|<b>|</b>"
)
_INLINE_RE = re.compile(r"<color=#([0-9A-Fa-f]{3,8})>|<b>|</b>|</color>")
_SENTINEL = "\x00"


def strip_markup(text: str) -> str:
    """去掉所有排版标记，只留文字。"""
    return _STRIP_RE.sub("", text or "").strip()


def _inline_html(text: str) -> str:
    """把游戏标记转成 HTML（每行独立闭合，避免跨行污染）。"""
    source = _TAG_RE.sub("", text or "")
    out: list[str] = []
    open_spans = 0
    open_bold = 0
    index = 0
    for match in _INLINE_RE.finditer(source):
        out.append(html.escape(source[index : match.start()], quote=False))
        token = match.group(0)
        if token.startswith("<color="):
            out.append(f'<span style="color:#{match.group(1)}">')
            open_spans += 1
        elif token == "<b>":
            out.append("<b>")
            open_bold += 1
        elif token == "</b>" and open_bold:
            out.append("</b>")
            open_bold -= 1
        elif token == "</color>" and open_spans:
            out.append("</span>")
            open_spans -= 1
        index = match.end()
    out.append(html.escape(source[index:], quote=False))
    result = "".join(out)
    if open_bold:
        result += "</b>" * open_bold
    if open_spans:
        result += "</span>" * open_spans
    return result.strip()


def parse_rich_soulmark(text: str) -> list[dict]:
    """解析成 [{title, lines: [{level, html, text, sprite}]}]。

    没有标记时返回空列表，调用方应回退到纯文本渲染。
    """
    raw = (text or "").strip()
    if not raw or ("<indent=" not in raw and "<sprite=" not in raw):
        return []

    sections: list[dict] = []
    current: dict | None = None

    for line in re.split(r"[\r\n]+", raw):
        if not line.strip():
            continue
        indent = 0
        sprite = -1
        for match in _TAG_RE.finditer(line):
            kind, value = match.group(1), int(match.group(2))
            if kind == "indent":
                # 后一个 indent 覆盖前一个（游戏里是嵌套标注，取最内层）
                indent = value
            else:
                sprite = max(sprite, value)
        level = indent // 16
        plain = strip_markup(line)
        if not plain:
            continue
        rich = _inline_html(line)
        is_header = plain.endswith(("：", ":")) and level <= 1
        if is_header:
            current = {"title": plain.rstrip("：:"), "lines": []}
            sections.append(current)
            continue
        if current is None:
            current = {"title": "", "lines": []}
            sections.append(current)
        current["lines"].append(
            {"level": min(level, 3), "html": rich, "text": plain, "sprite": sprite}
        )

    return [section for section in sections if section["lines"] or section["title"]]
