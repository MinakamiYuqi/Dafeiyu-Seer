from collections.abc import Awaitable, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)


class _SenderOnlyFilter(SessionFilter):
    """按「会话 + 发送者」隔离等待器。

    默认的 SessionFilter 只按会话（群）区分，于是菜单一弹出，群里**任何人**
    说话都会被当成在选序号，回一句「序号无效」，直到有人发 0。
    加上发送者后，只有发起查询的人能继续选。
    """

    def filter(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        sender = ""
        if callable(getter):
            try:
                sender = str(getter() or "")
            except Exception:
                sender = ""
        return f"{event.unified_msg_origin}:{sender}"


async def multi_select_query(
    event: AstrMessageEvent,
    arg: str,
    *,
    getter: Callable,
    prepare_result: Callable[..., Awaitable],
    label: str,
    result_type: str = "chain",
    error_log_name: str = "",
):
    from ..data import db_manager

    if not arg.strip():
        yield event.plain_result(f"❌请提供要查询的{label}名称。\n用法：/{label} <名称>")
        return

    if not db_manager.is_database_loaded("seerapi"):
        yield event.plain_result("❌数据库未加载，请稍后再试")
        return

    sessions = db_manager.get_all_sessions()
    results = getter(sessions, arg)

    if not results:
        yield event.plain_result(f"❌未找到匹配的{label}: {arg}")
        return

    if len(results) > 20:
        yield event.plain_result("❌重名超过20个，请重新检索关键词！")
        return

    if len(results) == 1:
        components = await prepare_result(results[0])
        if result_type == "image":
            yield event.image_result(components)
        else:
            yield event.chain_result(components)
        return

    prompt_items = [
        {"name": item.name, "desc": str(item.id), "value": item.id} for item in results[:20]
    ]
    prompt_map = {str(i + 1): p["value"] for i, p in enumerate(prompt_items)}

    async def send_result(item, evt):
        components = await prepare_result(item)
        await evt.send(evt.chain_result(components))

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

        index = int(user_input) - 1
        await send_result(results[index], evt)
        controller.stop()

    msg = f"请问你想查询的{label}是……\n"
    for i, item in enumerate(prompt_items, 1):
        msg += f"{i}. {item['name']}（{item['desc']}）\n"
    msg += "\n💬 输入序号选择 · 输入 0 退出"

    yield event.plain_result(msg)

    try:
        await handler(event, _SenderOnlyFilter())
    except TimeoutError:
        yield event.plain_result("⏰选择超时，已退出")
    except Exception as e:
        log_name = error_log_name or label
        logger.error(f"{log_name} selection error: {e}")
        yield event.plain_result(f"发生错误: {e}")
