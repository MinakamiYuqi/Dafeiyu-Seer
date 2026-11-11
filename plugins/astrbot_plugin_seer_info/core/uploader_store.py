"""配置图收录白名单：支持「昵称 = UID」写法，并记录群友发言次数做候选。

配置里可以写（`pet_config_uploaders`）::

    F81DAEACBA6205676130A3271EEDE726
    佑栖=F81DAEACBA6205676130A3271EEDE726
    佑栖：F81DAEACBA6205676130A3271EEDE726

匹配只看 UID，昵称只是给人看的。除此之外还有一个自动记录的候选池
（`uploaders.json`）：谁在群里说过话就记一笔，`白名单` 命令据此列出常聊的人，
再一键加进去，不用一个个手打 UID。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

__all__ = [
    "parse_uploader_config",
    "UploaderStore",
]

_SPLIT_RE = re.compile(r"[\n;；,，]+")
_PAIR_RE = re.compile(r"^\s*(?P<name>[^=:：\s]+)\s*[=:：]\s*(?P<uid>[0-9A-Za-z_-]{6,})\s*$")
_MAX_CANDIDATES = 500
# 累计发言达到这个次数，自动把候选转进白名单（只增不减，可用 remove 撤销）
_AUTO_PROMOTE_COUNT = 30


def parse_uploader_config(text: str) -> dict[str, str]:
    """解析白名单配置，返回 {uid: 昵称}（昵称可能为空串）。"""
    mapping: dict[str, str] = {}
    if not text:
        return mapping
    for chunk in _SPLIT_RE.split(str(text)):
        item = chunk.strip()
        if not item or item.startswith("#"):
            continue
        match = _PAIR_RE.match(item)
        if match:
            uid = match.group("uid").strip()
            name = match.group("name").strip()
            mapping[uid] = name
            continue
        # 纯 UID
        mapping[item.strip()] = ""
    return mapping


@dataclass
class Candidate:
    uid: str
    name: str = ""
    count: int = 0
    last_seen: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "last_seen": self.last_seen,
            "flag": _flag_of(self.uid),
        }

    @classmethod
    def from_dict(cls, uid: str, data: dict) -> Candidate:
        return cls(
            uid=uid,
            name=str(data.get("name") or ""),
            count=int(data.get("count") or 0),
            last_seen=str(data.get("last_seen") or ""),
        )


def _flag_of(uid: str) -> int | None:
    stripped = uid.strip("#")
    return int(stripped) if stripped.isdigit() else None


@dataclass
class UploaderStore:
    """白名单 + 候选池。白名单来自配置，候选池落在磁盘里。"""

    members: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    path: Path | None = None
    # 运行期记录哪些是自动转正的，便于管理员分辨
    _auto_promoted: set = field(default_factory=set)

    # ---------------- 白名单 ----------------

    def load_config(self, text: str) -> None:
        """配置里的条目覆盖昵称；磁盘里手动加的保留。"""
        merged = dict(self.members)
        for uid, name in parse_uploader_config(text).items():
            merged[uid] = name or merged.get(uid, "")
        self.members = merged

    def add(self, uid: str, name: str = "") -> bool:
        uid = (uid or "").strip()
        if not uid:
            return False
        if uid in self.members and (not name or self.members[uid]):
            return False
        self.members[uid] = name or self.members.get(uid, "")
        return True

    def remove(self, uid: str) -> bool:
        return self.members.pop((uid or "").strip(), None) is not None

    def contains(self, uid: str) -> bool:
        return (uid or "").strip() in self.members

    def label(self, uid: str) -> str:
        name = self.members.get((uid or "").strip(), "")
        return f"{name}（{uid}）" if name else str(uid)

    # ---------------- 候选池 ----------------

    def touch(self, uid: str, name: str = "", *, when: str | None = None) -> None:
        uid = (uid or "").strip()
        if not uid:
            return
        entry = self.candidates.get(uid) or Candidate(uid=uid)
        entry.count += 1
        if name:
            entry.name = name
        entry.last_seen = when or datetime.now().strftime("%Y-%m-%d %H:%M")
        self.candidates[uid] = entry
        # 活跃度自动转正：说够 _AUTO_PROMOTE_COUNT 次就进白名单，省得管理员手动加
        if entry.count >= _AUTO_PROMOTE_COUNT and uid not in self.members:
            self.members[uid] = entry.name or ""
            self._auto_promoted.add(uid)
        if len(self.candidates) > _MAX_CANDIDATES:
            self._trim()

    def _trim(self) -> None:
        ordered = sorted(
            self.candidates.values(),
            key=lambda item: (item.count, item.last_seen),
        )
        for item in ordered[: len(self.candidates) - _MAX_CANDIDATES]:
            self.candidates.pop(item.uid, None)

    def top(self, limit: int = 20) -> list[Candidate]:
        return sorted(
            self.candidates.values(),
            key=lambda item: (-item.count, item.last_seen),
        )[:limit]

    # ---------------- 落盘 ----------------

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        members = data.get("members") if isinstance(data, dict) else None
        if isinstance(members, dict):
            self.members = {
                str(uid): str(name or "") for uid, name in members.items() if str(uid).strip()
            }
        raw = data.get("candidates") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            self.candidates = {
                str(uid): Candidate.from_dict(str(uid), info)
                for uid, info in raw.items()
                if isinstance(info, dict)
            }

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "members": {uid: name for uid, name in self.members.items()},
            "candidates": {
                uid: {"name": item.name, "count": item.count, "last_seen": item.last_seen}
                for uid, item in self.candidates.items()
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            # 候选池丢了不影响主流程，但静默失败会让「谁说过话」统计悄悄失真
            logger.warning("[uploader_store] 保存候选池失败 %s：%s", self.path, exc)
