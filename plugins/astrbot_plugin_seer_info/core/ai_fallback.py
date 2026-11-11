"""AI 兜底：把规则解析不了的消息交给大模型，转成插件已有的指令。

纯标准库实现（只依赖同包的 command_parser），不导入 AstrBot，方便离线测试。
真正的 LLM 调用在 main.py 的 ``_ask_llm()`` 里，这里只负责：

* ``should_ask_ai()``    —— 这条消息该不该问 AI
* ``build_messages()``   —— 拼 system prompt 和用户 prompt
* ``parse_intent_json()``—— 把模型输出解析成 (intent, keyword)，容错处理代码块/多余文字
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .command_parser import (
    INTENT_BY_KEY,
    INTENTS,
    is_known_command,
    parse_query,
    resolve_intent,
)

__all__ = [
    "IntentResult",
    "SEER_KEYWORDS",
    "DEFAULT_DENIAL_HINT",
    "has_seer_keyword",
    "looks_like_capability_denial",
    "should_ask_ai",
    "build_messages",
    "build_capability_prompt",
    "parse_intent_json",
    "sanitize_keyword",
    "NON_SEER_MARKERS",
    "mentions_other_game",
]

# 模型嘴硬说「查不了」时补的话；可用 llm_denial_hint 覆盖
DEFAULT_DENIAL_HINT = "\n\n（逗你的，赛尔号的发「精灵 雷伊」这种就行，我给你翻）"

_DENIAL_MARKERS: tuple[str, ...] = (
    "查不了",
    "不能查",
    "没法查",
    "无法查",
    "没接",
    "没接入",
    "接不了",
    "没有图鉴",
    "没有数据",
    "没数据",
    "没有数据库",
    "查不到",
)


def looks_like_capability_denial(text: str) -> bool:
    """判断模型是不是在否认自己会查赛尔号数据。"""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    if t.startswith("❌"):  # 插件自己的报错，不是模型的话
        return False
    return any(marker in t for marker in _DENIAL_MARKERS)


# 「精灵 / 立绘 / 属性克制」这些词在别的游戏里也通用，
# 出现下面这些标记就说明用户在说别的游戏，不该套赛尔号的库。
NON_SEER_MARKERS: tuple[str, ...] = (
    "宝可梦",
    "口袋妖怪",
    "皮卡丘",
    "洛克王国",
    "奥拉星",
    "奥比岛",
    "摩尔庄园",
    "原神",
    "崩坏",
    "崩铁",
    "星穹铁道",
    "鸣潮",
    "明日方舟",
    "王者荣耀",
    "英雄联盟",
    "阴阳师",
    "植物大战僵尸",
    "游戏王",
    "炉石",
)


def mentions_other_game(text: str) -> bool:
    """是不是在问别的游戏（说过「赛尔号」就一律当赛尔号）。"""
    t = (text or "").lower()
    if not t:
        return False
    if "赛尔号" in t or "seer" in t:
        return False
    return any(marker in t for marker in NON_SEER_MARKERS)

# 每个 intent 的中文说明，用于提示词
_INTENT_DESC: dict[str, str] = {
    "pet_info": "查询精灵的基础信息（种族值、属性、技能、魂印等）",
    "pet_image": "查询精灵立绘或皮肤图片",
    "pet_config": "查询精灵配置图（本地收录的配装图）",
    "type_matchup": "查询属性克制关系表",
    "battle_effect": "查询异常状态的效果说明",
    "mintmark": "查询刻印信息及数值",
    "gem": "查询刻印宝石信息",
    "suit": "查询套装信息",
    "equip": "查询装备部件信息",
    "title": "查询称号信息",
    "preview": "获取下周预告图（不需要参数）",
    "server": "查询服务器是否已开服（不需要参数）",
    "help": "查看插件帮助（不需要参数）",
    "alias": "查精灵的外号/别称",
}

# 触发 AI 兜底的关键词：指令词本身 + 几个玩家常用说法。
# 「资料 / 信息 / 介绍 / 详情」本身不是指令词（「南霜的资料」没有指令词），
# 交给 AI 判断意图更稳。
SEER_KEYWORDS: tuple[str, ...] = tuple(
    sorted(
        {kw for intent in INTENTS for kw in intent.keywords}
        | {"赛尔号", "赛尔", "图鉴", "编号", "种族值", "资料", "信息", "介绍", "详情"},
        key=len,
        reverse=True,
    )
)

_NONE_WORDS = {"none", "null", "no", "other", "chat", "闲聊", "无", "不是"}
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)
_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.S)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class IntentResult:
    """AI 判定出来的指令。"""

    key: str
    keyword: str = ""

    @property
    def takes_arg(self) -> bool:
        intent = INTENT_BY_KEY.get(self.key)
        return bool(intent and intent.takes_arg)


def has_seer_keyword(text: str) -> bool:
    """消息里是否出现了赛尔号数据相关的词。"""
    lowered = (text or "").lower()
    return any(kw.lower() in lowered for kw in SEER_KEYWORDS)


def should_ask_ai(
    text: str,
    *,
    mode: str,
    allow_bare: bool = False,
    max_length: int = 120,
) -> bool:
    """判断这条消息要不要交给 AI 兜底。

    ``mode``:
        ``off``     关闭
        ``keyword`` 消息里含赛尔号相关关键词才问（默认，省钱）
        ``always``  被唤醒的消息都问（覆盖最全，也最贵）
    """
    if mode not in ("keyword", "always"):
        return False
    message = (text or "").strip()
    if not message:
        return False
    if max_length > 0 and len(message) > max_length:
        return False
    # 标准指令和宽松指令能处理的，交给它们，避免重复回复
    if is_known_command(message):
        return False
    if parse_query(message, allow_bare=allow_bare) is not None:
        return False
    # 说的是别的游戏（宝可梦/原神…），别拿赛尔号的库去套
    if mentions_other_game(message):
        return False
    if mode == "keyword" and not has_seer_keyword(message):
        return False
    return True


def build_capability_prompt() -> str:
    """生成注入到聊天系统提示词里的「能力说明」。

    注意措辞：很多群聊人格（比如「普通群友、不要暴露系统提示词、不要解释设定」）
    会把「本环境装有插件」这类话当成元信息藏起来，然后照样回答「查不了」。
    所以这里写成第一人称的「你会的」，当成角色本身的本事，而不是外部配置说明。
    """
    names = "、".join(i.name for i in INTENTS if i.key != "help")
    return (
        "【你会的】你是赛尔号老玩家，"
        f"{names}这些你都查得到，数据是现翻的。\n"
        "只认赛尔号：宝可梦、洛克王国、原神这些别套，也别拿赛尔号的数据去回答别的游戏。\n"
        "有人问你能不能查，就答能，让他发「精灵 雷伊」「立绘 雷伊」这种；"
        "别编具体数值，拿不准就说等我翻一下。"
    )


def build_messages(message: str) -> tuple[str, str]:
    """返回 (system_prompt, user_prompt)。"""
    lines = ["你是赛尔号数据查询插件的意图解析器。把用户的一句话转换成 JSON。", "", "可选 intent："]
    for intent in INTENTS:
        words = "、".join(intent.keywords[:8])
        desc = _INTENT_DESC.get(intent.key, intent.name)
        lines.append(f"- {intent.key}：{desc}（玩家可能说：{words}）")
    lines.append("- none：不是赛尔号数据查询请求")
    lines.append("")
    lines.append('只输出一行 JSON，不要解释，不要代码块：')
    lines.append('{"intent": "<intent>", "keyword": "<名称或数字ID>"}')
    lines.append("")
    lines.append("规则：")
    lines.append("1. intent 只能是上面列出的英文键，不要自创。")
    lines.append("2. keyword 保留用户原文里的名称，不要改写、翻译或补全；没有名称就输出空字符串。")
    lines.append("3. 闲聊、问玩法、问攻略、问价格、与赛尔号数据无关的问题，返回 "
                 '{"intent": "none", "keyword": ""}。')
    lines.append("4. 拿不准时返回 none，不要猜。")
    system_prompt = "\n".join(lines)
    user_prompt = f"用户消息：{message}"
    return system_prompt, user_prompt


def sanitize_keyword(value: str) -> str:
    """清理模型给出的名称：去引号、去换行、压缩空白、限长。"""
    text = (value or "").strip().strip("\"'“”‘’「」『』【】")
    if not text:
        return ""
    text = text.splitlines()[0]
    text = _WS_RE.sub(" ", text).strip()
    return text[:32]


def _to_result(data: dict) -> IntentResult | None | bool:
    """把 JSON 对象转成结果；返回 False 表示模型明确说了「不是查询」。"""
    raw_intent = data.get("intent") or data.get("command") or data.get("cmd") or ""
    raw_intent = str(raw_intent).strip()
    if not raw_intent:
        return None
    if raw_intent.lower() in _NONE_WORDS:
        return False
    key = resolve_intent(raw_intent)
    if key is None:
        return None
    keyword = sanitize_keyword(
        str(data.get("keyword") or data.get("name") or data.get("arg") or "")
    )
    return IntentResult(key, keyword)


def parse_intent_json(raw: str) -> IntentResult | None:
    """解析模型输出；不是查询或解析失败都返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    candidates: list[str] = []
    fence = _CODE_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)

    for candidate in candidates:
        data = _loads_dict(candidate)
        if data is None:
            match = _JSON_OBJECT_RE.search(candidate)
            if match:
                data = _loads_dict(match.group(0))
        if data is None:
            continue
        result = _to_result(data)
        if result is False:
            return None
        if result is not None:
            return result
    return None


def _loads_dict(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None
