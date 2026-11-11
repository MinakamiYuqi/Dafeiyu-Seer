"""奶龙图库（恶搞图）：一个奶龙名对应一只精灵。

目录结构::

    <插件数据目录>/milk_dragon/
        1_奶政.png
        index.json

`index.json` 里按加入顺序存条目，查询菜单就按这个顺序编号。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

__all__ = ["MilkEntry", "MilkDragonStore", "SUFFIXES"]

SUFFIXES: tuple[str, ...] = (".png", ".webp", ".jpg", ".jpeg")
MAX_BYTES = 8 * 1024 * 1024
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


@dataclass
class MilkEntry:
    name: str
    pet_id: int
    pet_name: str = ""
    file: str = ""
    added_at: str = ""
    uploader: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pet_id": self.pet_id,
            "pet_name": self.pet_name,
            "file": self.file,
            "added_at": self.added_at,
            "uploader": self.uploader,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MilkEntry | None:
        try:
            name = str(data.get("name") or "").strip()
            pet_id = int(data.get("pet_id") or 0)
        except Exception:
            return None
        if not name or pet_id <= 0:
            return None
        return cls(
            name=name,
            pet_id=pet_id,
            pet_name=str(data.get("pet_name") or ""),
            file=str(data.get("file") or ""),
            added_at=str(data.get("added_at") or ""),
            uploader=str(data.get("uploader") or ""),
        )

    def label(self) -> str:
        return f"{self.name}（{self.pet_name}）" if self.pet_name else self.name


@dataclass
class MilkDragonStore:
    root: Path
    entries: list[MilkEntry] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return
        raw = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return
        entries: list[MilkEntry] = []
        for item in raw:
            entry = MilkEntry.from_dict(item) if isinstance(item, dict) else None
            if entry is not None:
                entries.append(entry)
        self.entries = entries

    def save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"entries": [entry.to_dict() for entry in self.entries]}
            self.index_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            # 索引写不进去 = 新加的奶龙图下次重启就没了，不能静默
            logger.warning("[milk_dragon] 保存索引失败 %s：%s", self.index_path, exc)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def all(self) -> list[MilkEntry]:
        return list(self.entries)

    def find(self, name: str) -> MilkEntry | None:
        key = (name or "").strip()
        if not key:
            return None
        for entry in self.entries:
            if entry.name == key:
                return entry
        for entry in self.entries:
            if key in entry.name:
                return entry
        return None

    def path_of(self, entry: MilkEntry) -> Path | None:
        if not entry.file:
            return None
        path = self.root / entry.file
        return path if path.is_file() else None

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    @staticmethod
    def _safe(name: str) -> str:
        cleaned = _BAD_CHARS.sub("", name or "")
        return cleaned[:24] or "milk"

    def add(
        self,
        name: str,
        pet_id: int,
        pet_name: str,
        data: bytes,
        suffix: str,
        *,
        uploader: str = "",
    ) -> MilkEntry:
        """同名会覆盖（换图），保留原来的顺序。"""
        suffix = suffix.lower()
        if suffix not in SUFFIXES:
            suffix = ".png"
        filename = f"{pet_id}_{self._safe(name)}{suffix}"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / filename).write_bytes(data)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 只认完全同名，避免「奶盖」把「宿命的奶盖」覆盖掉
        target_name = name.strip()
        existing = next(
            (item for item in self.entries if item.name == target_name), None
        )
        if existing is not None:
            if existing.file and existing.file != filename:
                try:
                    (self.root / existing.file).unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("[milk_dragon] 旧图删除失败 %s：%s", existing.file, exc)
            existing.pet_id = int(pet_id)
            existing.pet_name = pet_name
            existing.file = filename
            existing.added_at = now
            existing.uploader = uploader
            entry = existing
        else:
            entry = MilkEntry(
                name=name.strip(),
                pet_id=int(pet_id),
                pet_name=pet_name,
                file=filename,
                added_at=now,
                uploader=uploader,
            )
            self.entries.append(entry)
        self.save()
        return entry

    def remove(self, name: str) -> bool:
        entry = self.find(name)
        if entry is None:
            return False
        path = self.path_of(entry)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("[milk_dragon] 删除图片失败 %s：%s", path, exc)
        self.entries.remove(entry)
        self.save()
        return True

    def stats(self) -> tuple[int, int]:
        total = 0
        for entry in self.entries:
            path = self.path_of(entry)
            if path is not None:
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return len(self.entries), total
