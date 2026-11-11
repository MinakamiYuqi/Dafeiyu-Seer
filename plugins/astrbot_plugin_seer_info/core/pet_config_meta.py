"""配置图的元数据：作者、备注、收录时间（纯标准库，可单独测试）。

图片本身按 `<精灵ID>.<后缀>` 存放，元数据写在同名的 `<精灵ID>.json` 里：

    pet_configs/70.png
    pet_configs/70.json
    {"author": "张三", "note": "物攻流，配泰坦刻印", "uploader": "...", "updated_at": "..."}

分开存的好处：改备注不用重新上传图片，手动丢图也能不带元数据正常工作。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "ConfigMeta",
    "parse_upload_arg",
    "clean_text",
    "META_KEYS",
]

# 支持「作者:张三」「作者：张三」「作者=张三」，以及 [作者:张三] 这种方括号包裹
_KEY_RE = re.compile(r"[\[\(【〔]?\s*(作者|来源|备注|说明)\s*[:：=]\s*")
_WRAPPERS = " \t\r\n[]【】〔〕()（）\"'“”‘’"
META_KEYS = {"作者": "author", "来源": "author", "备注": "note", "说明": "note"}


@dataclass(frozen=True)
class ConfigMeta:
    author: str = ""
    note: str = ""
    uploader: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "author": self.author,
            "note": self.note,
            "uploader": self.uploader,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConfigMeta:
        if not isinstance(data, dict):
            return cls()

        def _s(key: str) -> str:
            value = data.get(key)
            return str(value).strip() if isinstance(value, (str, int, float)) else ""

        return cls(
            author=_s("author"),
            note=_s("note"),
            uploader=_s("uploader"),
            updated_at=_s("updated_at"),
        )

    def lines(self) -> list[str]:
        """展示用的行，空字段不显示。"""
        out: list[str] = []
        if self.author:
            out.append(f"作者：{self.author}")
        if self.note:
            out.append(f"备注：{self.note}")
        if self.updated_at:
            out.append(f"收录：{self.updated_at}")
        return out


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# QQ 上常见的「空白昵称」：HANGUL FILLER、零宽字符、盲文空格等
_INVISIBLE = "\u3164\u115f\u1160\u2800\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"


def clean_text(text: str) -> str:
    """去掉首尾括号/引号/空白，以及整条只有隐形字符的情况。"""
    value = (text or "").strip(_WRAPPERS).strip()
    if not value.strip(_INVISIBLE).strip():
        return ""
    return value


def _strip_wrappers(text: str) -> str:
    """去掉首尾的括号、引号、空白。"""
    return clean_text(text)


def _split_paren_author(text: str) -> tuple[str, str]:
    """把结尾的（作者）/ (作者) / 【作者】 拆出来。

    只认结尾那一组，且内容不长（<=20 字）、不含「备注」之类字段名，
    避免把「（物攻流）」这种顺手写的说明当成作者。
    """
    m = re.search(r"[（(【\[]([^（()）【】\[\]]{1,20})[）)】\]]\s*$", text)
    if not m:
        return text, ""
    inner = clean_text(m.group(1))
    if not inner or _KEY_RE.search(inner):
        return text, ""
    return text[: m.start()].strip(), inner


_LEAD_CMD_RE = re.compile(r"^(?:收录配置图|配置图|配置|收录)\s*")


def parse_upload_arg(text: str) -> tuple[str, ConfigMeta]:
    """解析上传参数，尽量宽松。

    支持的写法（有没有空格、指令前缀带不带都行）：
        配置图 雷伊
        配置图雷伊
        收录配置图 雷伊 作者:张三 备注:物攻流
        配置图 雷伊 物攻流（张三）
        配置图 雷伊（张三）
        配置图 雷伊 [作者:张三] [备注:物攻流]
    解析不出名字就返回空串，由调用方回一句正确格式。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ConfigMeta()

    # 指令词可能被一起带进来（宽松匹配时 _full_arg 不一定切干净）
    raw = _LEAD_CMD_RE.sub("", raw).strip()
    if not raw:
        return "", ConfigMeta()

    author = ""
    note_parts: list[str] = []

    # ① 先拆结尾括号里的作者
    raw, paren_author = _split_paren_author(raw)
    if paren_author:
        author = paren_author

    # ② 再认 作者:/备注: 这类键值字段
    matches = list(_KEY_RE.finditer(raw))
    if matches:
        name = _strip_wrappers(raw[: matches[0].start()])
        if " " in name:
            name, _, free = name.partition(" ")
            free_note = free.strip()
        else:
            free_note = ""
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            value = _strip_wrappers(raw[match.end() : end])
            if not value:
                continue
            field = META_KEYS.get(match.group(1), "")
            if field == "author" and not author:
                author = value
            elif field == "note":
                note_parts.append(value)
        note = "，".join(p for p in note_parts if p) or free_note
        return _strip_wrappers(name), ConfigMeta(author=author, note=note, updated_at=_now())

    # ③ 没有键值字段：按空白切「名字 + 自由备注」；一个空格都没有就整条当名字，
    #    由 handler 再逐字回退去数据库里试（支持「配置图雷伊物攻流」这种连写）
    if " " in raw:
        head, _, free = raw.partition(" ")
        name = _strip_wrappers(head)
        note = free.strip()
    else:
        name = _strip_wrappers(raw)
        note = ""
    return name, ConfigMeta(author=author, note=note, updated_at=_now())


def merge_meta(base: ConfigMeta, *, author: str = "", note: str = "") -> ConfigMeta:
    """改备注时只覆盖传进来的字段。"""
    return ConfigMeta(
        author=author or base.author,
        note=note or base.note,
        uploader=base.uploader,
        updated_at=_now(),
    )


def dumps(meta: ConfigMeta) -> str:
    return json.dumps(meta.to_dict(), ensure_ascii=False, indent=2)


def loads(text: str) -> ConfigMeta:
    try:
        return ConfigMeta.from_dict(json.loads(text))
    except Exception:
        return ConfigMeta()
