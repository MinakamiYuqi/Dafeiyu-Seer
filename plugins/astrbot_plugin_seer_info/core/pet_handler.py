import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..data import PetDataGetter, PetORM, PetSkinDataGetter, db_manager
from ..service import PetBodyImageGetter, save_bytes_to_temp_file
from .common_handler import multi_select_query
from .pet_renderer import PET_TEMPLATE, render_pet_info_data
from .renderer import render_to_image


def _pet_image_candidates(sessions, arg):
    """立绘候选：精灵优先，其次精灵自带的皮肤，最后是独立皮肤。

    对齐 Murmansk5000/IronsBot 的 PetQueryService._image_choices：
    那边 `for pet in pets:` 先把精灵本身作为候选（resource_id = pet.id），
    再把 `pet.skins` 作为子选项，最后才遍历独立 skin 查询。
    我们原来只有 PetSkinDataGetter，等于只做了最后一步。
    """
    pets = PetDataGetter(sessions, arg)
    skins = PetSkinDataGetter(sessions, arg)
    if not skins:
        return pets
    if not pets:
        return skins
    # 精灵在前：用户问「帝录立绘」时优先给精灵本体，而不是同名皮肤
    return tuple(pets) + tuple(skins)



class PetHandler:
    def __init__(self, html_render=None, image_format: str = "jpeg", jpeg_quality: int = 85):
        self._html_render = html_render
        self._image_format = image_format
        self._jpeg_quality = jpeg_quality

    async def _render_pet_info_html(self, pet) -> str:
        render_data = await render_pet_info_data(pet)
        return await render_to_image(
            PET_TEMPLATE,
            render_data,
            html_render=self._html_render,
            image_format=self._image_format,
            jpeg_quality=self._jpeg_quality,
        )

    async def pet_info(self, event: AstrMessageEvent, arg: str = ""):
        if not arg.strip():
            yield event.plain_result("❌请提供要查询的精灵名称或ID。\n用法：/精灵 <名称>")
            return

        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return

        sessions = db_manager.get_all_sessions()
        pets = PetDataGetter(sessions, arg)

        if not pets:
            yield event.plain_result(f"❌未找到匹配的精灵: {arg}")
            return

        if len(pets) > 20:
            yield event.plain_result("❌重名超过20个，请重新检索关键词！")
            return

        if len(pets) == 1:
            yield event.image_result(await self._render_pet_info_html(pets[0]))
            return

        prompt_items = [
            {"name": pet.name, "desc": str(pet.id), "value": pet.id} for pet in pets[:20]
        ]
        prompt_map = {str(i + 1): p["value"] for i, p in enumerate(prompt_items)}

        async def send_result(pet_obj, evt):
            await evt.send(evt.image_result(await self._render_pet_info_html(pet_obj)))

        @session_waiter(timeout=60, record_history_chains=False)
        async def handler(controller: SessionController, evt: AstrMessageEvent):
            user_input = evt.message_str.strip()
            if user_input == "0":
                await evt.send(evt.plain_result("❌已退出查询"))
                controller.stop()
                return

            if user_input not in prompt_map:
                await evt.send(evt.plain_result("⚠️序号无效，请重新输入"))
                controller.keep(timeout=60, reset_timeout=True)
                return

            pet_obj = sessions.get("seerapi").get(PetORM, prompt_map[user_input])
            if not pet_obj:
                await evt.send(evt.plain_result(f"❌未找到精灵 ID: {prompt_map[user_input]}"))
                controller.stop()
                return
            await send_result(pet_obj, evt)
            controller.stop()

        msg = "请问你想查询的精灵是……\n"
        for i, item in enumerate(prompt_items, 1):
            msg += f"{i}. {item['name']}（{item['desc']}）\n"
        msg += "\n💬 输入序号选择 · 输入 0 退出"

        yield event.plain_result(msg)

        try:
            await handler(event)
        except TimeoutError:
            yield event.plain_result("⏰选择超时，已退出")
        except Exception as e:
            logger.error(f"pet_info selection error: {e}")
            yield event.plain_result(f"发生错误: {e}")

    async def pet_image(self, event: AstrMessageEvent, arg: str = ""):
        """查立绘。

        2026-11-14 修：原来 getter 只给 PetSkinDataGetter（只查 pet_skin 表），
        没皮肤的精灵就查不到 —— 例如「帝录天锋」(id=4903) 在 pet 表里有
        resource_id=4903、立绘 4903.png 也能下，但 pet_skin 里 0 条，
        于是回「未找到匹配的立绘」。参考 Murmansk5000/IronsBot 的
        PetQueryService._image_choices：它**以精灵为主体**（pet.id 当 resource_id），
        皮肤是挂在精灵下面的子选项，最后才补一遍独立皮肤查询。
        这里对齐成同样的优先级：精灵 → 精灵的皮肤 → 其余皮肤。
        """
        async for result in multi_select_query(
            event,
            arg,
            getter=_pet_image_candidates,
            prepare_result=self._prepare_skin_result,
            label="立绘",
            error_log_name="pet_image",
        ):
            yield result

    async def _prepare_skin_result(self, skin):
        # skin 可能是 PetSkinORM，也可能是 PetORM（精灵本体）—— 两者都有
        # name / resource_id，精灵没有 pet / series 关系，用 getattr 兜住。
        image_bytes = await PetBodyImageGetter.get_bytes(str(skin.resource_id))
        temp_path = save_bytes_to_temp_file(image_bytes)
        lines = [f"💎【{skin.name}】"]
        pet = getattr(skin, "pet", None)
        if pet is not None and getattr(pet, "name", None):
            lines.append(f"所属精灵：{pet.name}")
        series = getattr(skin, "series", None)
        if series:
            series_name = getattr(series, "name", series)
            sub_type = getattr(skin, "sub_type", None)
            if sub_type is not None and getattr(sub_type, "name", None):
                series_name = f"{series_name} - {sub_type.name}"
            lines.append(f"所属系列：{series_name}")
        for field, label in (
            ("card_price", "礼卡价格"),
            ("diamond_price", "钻石价格"),
            ("skinhouse_price", "神秘屋价格"),
            ("discounted_skinhouse_price", "神秘屋折扣价格"),
            ("ticket_num", "可使用风尚券数量"),
        ):
            value = getattr(skin, field, None)
            if value:
                lines.append(f"{label}：{value}")
        return [Comp.Image.fromFileSystem(temp_path), Comp.Plain("\n".join(lines))]
