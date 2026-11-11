"""精灵外号（别名）查询。

数据来自插件自带的别名库（`aliases.sqlite` 的 `pet_aliases` 表），
配置里的自定义外号也会被写入同一张表，所以这里能一起看到。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from sqlmodel import select

from ..data import PetAliasORM, PetDataGetter, db_manager


class AliasHandler:
    async def query(self, event: AstrMessageEvent, arg: str = ""):
        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return
        sessions = db_manager.get_all_sessions()
        name = (arg or "").strip()
        if not name:
            yield event.plain_result(self._summary(sessions))
            return

        pets = PetDataGetter(sessions, name)
        if not pets:
            yield event.plain_result(f"❌未找到匹配的精灵: {name}")
            return
        if len(pets) > 1:
            lines = [f"匹配到 {len(pets)} 只精灵，用 ID 再查一次："]
            lines.extend(f"{pet.id}. {pet.name}" for pet in pets[:20])
            yield event.plain_result("\n".join(lines))
            return

        pet = pets[0]
        aliases = self._aliases(sessions, int(pet.id))
        if not aliases:
            yield event.plain_result(f"【{pet.name}（{pet.id}）】没有收录外号。")
            return
        yield event.plain_result(
            f"🏷️【{pet.name}（{pet.id}）】的外号（{len(aliases)} 个）：\n"
            + "、".join(aliases)
        )

    @staticmethod
    def _aliases(sessions, pet_id: int) -> list[str]:
        session = sessions.get("aliases")
        if session is None:
            return []
        try:
            rows = session.exec(
                select(PetAliasORM.name).where(PetAliasORM.target_id == pet_id)
            ).all()
        except Exception as e:
            logger.warning(f"外号查询失败: {e}")
            return []
        return sorted({str(row) for row in rows if str(row).strip()})

    def _summary(self, sessions) -> str:
        session = sessions.get("aliases")
        if session is None:
            return "❌别名库未加载，请稍后再试"
        try:
            rows = session.exec(select(PetAliasORM)).all()
        except Exception as e:
            logger.warning(f"别名库统计失败: {e}")
            return "❌别名库读取失败"
        if not rows:
            return "🏷️ 别名库是空的。发「外号 <精灵名>」也查不到东西。"
        targets = {row.target_id for row in rows}
        return (
            f"🏷️ 别名库共 {len(rows)} 条外号，覆盖 {len(targets)} 只精灵。\n"
            "发「外号 <精灵名或ID>」看具体某只。"
        )
