"""配置图白名单的管理命令（列出候选 / 添加 / 移除）。

候选来自插件被动记录（谁在群里说过话就记一笔），所以不用手打 UID。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .uploader_store import UploaderStore

__all__ = ["WhitelistHandler"]

_MAX_ROWS = 20


class WhitelistHandler:
    def __init__(self, store: UploaderStore):
        self._store = store

    # ------------------------------------------------------------------ #
    # 权限：管理员，或已经在白名单里的人
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sender_id(event) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                return ""
        return ""

    def allowed(self, event) -> bool:
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return True
            except Exception:
                pass
        return self._store.contains(self._sender_id(event))

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #

    COMMANDS = ("白名单", "上传白名单")

    def _full_arg(self, event, arg: str) -> str:
        """取完整参数。

        AstrBot 的 CommandFilter 对 ``arg: str`` 只传第一个空格前的词，
        「白名单 添加 17 18」进到这里 arg 只有「添加」，所以直接读消息文本。
        """
        text = ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                text = str(getter() or "").strip()
            except Exception:
                text = ""
        body = text.lstrip("/／").strip()
        for cmd in self.COMMANDS:
            if body.startswith(cmd):
                rest = body[len(cmd) :].strip()
                if rest:
                    return rest
                break
        return (arg or "").strip()

    async def command(self, event: AstrMessageEvent, arg: str = ""):
        arg = self._full_arg(event, arg)
        if not self.allowed(event):
            yield event.plain_result(
                "❌只有管理员或已在白名单里的人能管理白名单（发 /sid 查看自己的 ID）。"
            )
            return

        text = (arg or "").strip()
        if not text:
            yield event.plain_result(self._render_list())
            return

        action, _, rest = text.partition(" ")
        rest = rest.strip()
        if action in {"添加", "加", "add"}:
            async for result in self._add(event, rest):
                yield result
            return
        if action in {"移除", "删", "remove", "del"}:
            async for result in self._remove(event, rest):
                yield result
            return
        if action in {"清空候选", "清空列表"}:
            self._store.candidates.clear()
            self._store.save()
            yield event.plain_result("✅已清空候选名单（白名单本身没动）")
            return
        yield event.plain_result(
            "用法：\n"
            "白名单            查看常聊的人 + 当前白名单\n"
            "白名单 添加 1 3 5  按序号加入\n"
            "白名单 添加 佑栖    按昵称加入\n"
            "白名单 添加 UID    直接加 UID\n"
            "白名单 移除 佑栖    按昵称或 UID 移除"
        )

    def _render_list(self) -> str:
        members = self._store.members
        lines = [f"🧾 配置图白名单：{len(members)} 人"]
        for uid, name in list(members.items())[:20]:
            lines.append(f"  · {name or '（未记录昵称）'}（{uid}）")
        if len(members) > 20:
            lines.append(f"  ……还有 {len(members) - 20} 人")

        top = self._store.top(_MAX_ROWS)
        if not top:
            lines.append("\n💬 还没记录到群里谁在说话。等大家聊几句再看。")
            return "\n".join(lines)

        lines.append("\n👥 常聊的人（按发言次数）：")
        for index, item in enumerate(top, 1):
            mark = "✅" if self._store.contains(item.uid) else "☐"
            name = item.name or "（无昵称）"
            lines.append(
                f"{index}. {mark} {name}（{item.uid}）  {item.count} 条"
            )
        lines.append(
            "\n💬 加人：白名单 添加 1 3 5（可混用昵称/UID）\n"
            "💬 移除：白名单 移除 佑栖\n"
            "✅=已在白名单 ☐=还没加"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 添加 / 移除
    # ------------------------------------------------------------------ #

    def _resolve_tokens(self, tokens: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """把序号/昵称/UID 解析成 [(uid, name)]，返回 (命中, 未识别)。"""
        top = self._store.top(_MAX_ROWS)
        found: list[tuple[str, str]] = []
        missing: list[str] = []
        for token in tokens:
            item = token.strip()
            if not item:
                continue
            if item.isdigit() and 1 <= int(item) <= len(top):
                candidate = top[int(item) - 1]
                found.append((candidate.uid, candidate.name))
                continue
            if item in self._store.members:
                found.append((item, self._store.members[item]))
                continue
            matched = [
                (uid, cand.name)
                for uid, cand in self._store.candidates.items()
                if cand.name and (cand.name == item or item in cand.name)
            ]
            if matched:
                found.extend(matched)
                continue
            # 看起来像 UID 就直接收
            if len(item) >= 6 and item.isalnum():
                found.append((item, ""))
                continue
            missing.append(item)

        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for uid, name in found:
            if uid in seen:
                continue
            seen.add(uid)
            unique.append((uid, name))
        return unique, missing

    async def _add(self, event: AstrMessageEvent, rest: str):
        tokens = [part for part in rest.replace("，", " ").replace(",", " ").split() if part]
        if not tokens:
            yield event.plain_result("❌要加谁？例：白名单 添加 1 3 5")
            return
        resolved, missing = self._resolve_tokens(tokens)
        added: list[str] = []
        for uid, name in resolved:
            if self._store.add(uid, name):
                added.append(self._store.label(uid))
        self._store.save()
        if added:
            logger.info(f"白名单新增：{added}")
            yield event.plain_result(
                f"✅已加入 {len(added)} 人：\n" + "\n".join(f"  · {item}" for item in added)
                + f"\n当前共 {len(self._store.members)} 人（已保存，重载不丢）"
            )
        elif resolved:
            yield event.plain_result("这几位已经在白名单里了。")
        if missing:
            yield event.plain_result(
                "⚠️没认出来：" + "、".join(missing) + "\n可以先用序号，或直接发 UID。"
            )

    async def _remove(self, event: AstrMessageEvent, rest: str):
        tokens = [part for part in rest.replace("，", " ").replace(",", " ").split() if part]
        if not tokens:
            yield event.plain_result("❌要移除谁？例：白名单 移除 佑栖")
            return
        removed: list[str] = []
        missing: list[str] = []
        for token in tokens:
            label = token
            if token.isdigit() and token in self._store.members:
                label = token
            elif token not in self._store.members:
                hit = [uid for uid, name in self._store.members.items() if name and name == token]
                if len(hit) == 1:
                    label = hit[0]
                else:
                    missing.append(token)
                    continue
            if self._store.remove(label):
                removed.append(label)
        self._store.save()
        if removed:
            yield event.plain_result(
                f"✅已移除 {len(removed)} 人：" + "、".join(removed)
                + f"\n当前共 {len(self._store.members)} 人"
            )
        if missing:
            yield event.plain_result("⚠️不在白名单里：" + "、".join(missing))
