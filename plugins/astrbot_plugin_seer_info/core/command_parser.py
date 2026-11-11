"""宽松指令解析（纯标准库，不依赖 AstrBot，可独立测试）。

原插件的指令由 AstrBot 的 ``CommandFilter`` 匹配，要求消息严格以「指令名 + 空格」开头
（见 ``astrbot/core/star/filter/command.py`` 的
``message_str.startswith(f"{full_cmd} ") or message_str == full_cmd``），
所以「精灵雷伊」「查一下雷伊精灵」这类写法完全无法识别。

本模块提供一层宽松解析，识别顺序（先严格后宽松）：

1. 规范指令名 + 分隔符（等价于原来的行为）
2. 指令名 / 别名 / 同义词 与名字直接拼接（``精灵雷伊``）
3. 名字在前、指令词在后（``雷伊精灵``、``雷伊的精灵``、``雷伊 精灵``）
4. 先剥掉「查一下」「帮我」「赛尔号」等修饰词，再重复 1-3
5. 可选：整条消息就是一个名字时按精灵名查询（默认关闭）

为了不在闲聊里误触发，无分隔符的拼接形式只接受「像名字」的残余文本：
长度有限、不以「这只/怎么/好/真」等词开头、不以「吗/呢/啊」结尾。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "Intent",
    "Query",
    "INTENTS",
    "INTENT_BY_KEY",
    "parse_query",
    "matches_strict_command",
    "strict_command_names",
    "resolve_intent",
    "looks_like_bare_name",
    "bare_name_candidates",
    "ADMIN_COMMANDS",
    "is_known_command",
]

# --------------------------------------------------------------------------- #
# 指令表
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Intent:
    """一个指令意图。

    name/aliases 必须与 main.py 里 ``@filter.command(...)`` 注册的一致，
    synonyms 只参与宽松匹配，不会注册成 AstrBot 别名。
    """

    key: str
    name: str
    aliases: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    takes_arg: bool = True

    @property
    def strict_names(self) -> tuple[str, ...]:
        """AstrBot 严格匹配使用的全部指令名（指令名 + 别名）。"""
        return (self.name, *self.aliases)

    @property
    def keywords(self) -> tuple[str, ...]:
        """宽松匹配使用的全部关键词（严格名 + 同义词）。"""
        return (self.name, *self.aliases, *self.synonyms)


INTENTS: tuple[Intent, ...] = (
    Intent(
        "pet_info",
        "精灵",
        ("查询精灵信息", "魂印", "技能"),
        ("精灵信息", "精灵资料", "精灵图鉴", "赛尔号精灵", "查精灵", "精灵查询"),
    ),
    Intent(
        "pet_image",
        "立绘",
        ("皮肤", "查询立绘"),
        ("精灵立绘", "精灵皮肤", "立绘查询", "皮肤查询"),
    ),
    Intent(
        "type_matchup",
        "属性",
        ("属性表",),
        ("属性克制", "属性克制表", "属性相克", "克制表", "克制"),
    ),
    Intent(
        "pet_config",
        "配置图",
        ("配置",),
        ("精灵配置图", "配置图片", "配装图"),
    ),
    Intent(
        "battle_effect",
        "异常",
        ("查询异常", "异常状态", "查询异常状态"),
        ("异常效果", "异常查询"),
    ),
    Intent("mintmark", "刻印", (), ("刻印查询",)),
    Intent("gem", "宝石", ("刻印宝石",), ("宝石查询", "刻印宝石查询")),
    Intent("suit", "套装", ("查询套装信息",), ("套装信息", "套装查询")),
    Intent("equip", "部件", ("查询部件信息",), ("部件信息", "装备部件")),
    Intent("title", "称号", ("查询称号信息",), ("称号信息", "称号查询")),
    Intent("alias", "外号", ("别名",), ("别称", "外号查询", "别名查询")),
    Intent(
        "preview",
        "下周预告",
        ("预告",),
        ("预告图", "下周预告图"),
        takes_arg=False,
    ),
    Intent(
        "server",
        "开服查询",
        ("开服了吗",),
        ("开服", "服务器状态"),
        takes_arg=False,
    ),
    Intent("help", "帮助", (), ("菜单", "指令", "命令", "帮助信息"), takes_arg=False),
)

INTENT_BY_KEY: dict[str, Intent] = {i.key: i for i in INTENTS}

_PET_INTENT = INTENT_BY_KEY["pet_info"]

# 长词优先，保证「刻印宝石」不会被「刻印」抢先、「属性克制表」不会被「属性」抢先
_KEYWORD_INTENT: dict[str, Intent] = {}
for _intent in INTENTS:
    for _kw in _intent.keywords:
        _KEYWORD_INTENT.setdefault(_kw, _intent)
_KEYWORDS_LONG_FIRST: tuple[str, ...] = tuple(
    sorted(_KEYWORD_INTENT, key=lambda k: (-len(k), k))
)

_STRICT_NAMES: tuple[str, ...] = tuple(
    sorted({n for i in INTENTS for n in i.strict_names}, key=lambda k: (-len(k), k))
)


def strict_command_names() -> tuple[str, ...]:
    """返回严格匹配使用的全部指令名（供测试与 main.py 对照）。"""
    return _STRICT_NAMES


def resolve_intent(value: str) -> str | None:
    """把 ``pet_info`` / ``精灵`` / ``魂印`` 之类的输入解析成 intent key。

    给 AI 兜底用：模型偶尔会返回中文指令名而不是英文键，这里都认。
    """
    text = _normalize(value)
    if not text:
        return None
    lowered = text.lower()
    if lowered in INTENT_BY_KEY:
        return lowered
    intent = _KEYWORD_INTENT.get(text)
    return intent.key if intent else None


# --------------------------------------------------------------------------- #
# 字符集与修饰词
# --------------------------------------------------------------------------- #

_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
_WS_RE = re.compile(r"\s+")

_EDGE_CHARS = " \t\r\n\u3000,，、.。·・!！?？:：;；~～…—-—_/\\|+*#@&%$^&="
_QUOTE_CHARS = "\"'“”‘’「」『』【】《》()（）[]{}<>〈〉"
_PUNCT_CHARS = _EDGE_CHARS + _QUOTE_CHARS

# 指令词与名字之间的连接符：出现在这些字符之后即认为有分隔
_SEPARATORS = set(_PUNCT_CHARS)
# 「的/之」也算分隔，「雷伊的精灵」不应被当成拼接
_GLUE_CHARS = set("的之")

# 出现在名字之前的修饰词，按长度从长到短匹配
_FILLER_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "帮我查一下",
            "帮我查询",
            "帮我看看",
            "麻烦查一下",
            "麻烦查询",
            "查一下",
            "查询一下",
            "搜一下",
            "找一下",
            "看一下",
            "看下",
            "查下",
            "瞧下",
            "瞅瞅",
            "请帮我",
            "帮我",
            "帮忙",
            "麻烦",
            "请问",
            "给我",
            "发我",
            "我想查",
            "我要查",
            "我想",
            "我要",
            "查查",
            "看看",
            "查询",
            "查看",
            "搜索",
            "赛尔号",
            "赛尔",
            "图鉴",
            "查",
            "搜",
            "找",
            "看",
            "请",
            "seer",
        ),
        key=lambda k: (-len(k), k),
    )
)

# 「发/来/整/弄/给/上/搞 + 量词」这类请求动词：「给我发一下南霜的立绘」→「南霜」。
# 量词是必需的：否则「发条鸟」「来古」「上古炎兽」这类名字会被吃掉开头。
# 不带量词的「发我南霜」由上面的固定词表处理。
_MEASURE = r"(?:一下|一张|一个|一幅|一份|个|张|下|份)"
_REQUEST_PREFIX_RE = re.compile(
    r"^(?:(?:给我|帮我|帮忙|麻烦|请|能不能|可不可以)\s*)*"
    r"(?:发|来|整|弄|给|上|搞)" + _MEASURE
)

# 名字尾部可以安全丢弃的语气词/说明词
_TAIL_NOISE: tuple[str, ...] = tuple(
    sorted(
        (
            "的信息",
            "的资料",
            "的图鉴",
            "的属性",
            "信息",
            "资料",
            "图鉴",
            "数据",
            "详情",
            "面板",
            "技能",
            "魂印",
            "呢",
            "吗",
            "吧",
            "啊",
            "呀",
            "哦",
            "嘛",
            "啦",
            "了",
            "的",
        ),
        key=lambda k: (-len(k), k),
    )
)

# 名字前面残留的说明词：「魂印效果雷伊」「雷伊魂印效果」里多出来的「效果」
_LEAD_NOISE: tuple[str, ...] = tuple(
    sorted(
        ("效果", "技能", "魂印", "属性", "信息", "资料", "详情", "介绍"),
        key=lambda k: (-len(k), k),
    )
)

# 句尾的疑问成分：「雷伊的魂印是什么」「雷伊魂印效果」——剥掉后再按「名字+指令词」匹配
_QUESTION_TAILS: tuple[str, ...] = tuple(
    sorted(
        (
            "的效果是什么",
            "是什么效果",
            "有什么效果",
            "有什么用",
            "干什么用",
            "有啥用",
            "长什么样",
            "是什么",
            "是啥",
            "有什么",
            "有哪些",
            "怎么样",
            "咋样",
            "如何",
            "效果",
            "呢",
        ),
        key=lambda k: (-len(k), k),
    )
)

# 明显的闲聊句首，仅在「无分隔符拼接」时用于排除误判
_CHATTY_HEADS: tuple[str, ...] = (
    "这只",
    "那只",
    "这个",
    "那个",
    "这些",
    "那些",
    "什么",
    "怎么",
    "怎样",
    "如何",
    "为什么",
    "是不是",
    "有没有",
    "能不能",
    "可不可以",
    "可以",
    "我的",
    "你的",
    "他的",
    "她的",
    "它的",
    "今天",
    "明天",
    "昨天",
    "大家",
    "我们",
    "你们",
    "他们",
    "有人",
    "感觉",
    "看起来",
    "应该",
    "为啥",
    "干嘛",
    "怎",
    "咋",
    "都",
    "好",
    "真",
    "太",
    "很",
    "最",
    "挺",
    "是",
    "有",
    "谁",
    "哪",
)

_CHATTY_TAILS: tuple[str, ...] = ("吗", "呢", "啊", "吧", "呀", "嘛", "么", "了", "啥")

# 出现这些词基本就是句子而不是名字，无分隔符拼接时直接排除
_CHATTY_ANYWHERE: tuple[str, ...] = (
    "怎么",
    "怎样",
    "咋样",
    "如何",
    "为什么",
    "为啥",
    "哪些",
    "哪个",
    "多少",
    "什么",
    "是不是",
    "有没有",
    "能不能",
    "可不可以",
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """全角转半角、去零宽字符、压缩空白、去掉首尾标点。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH_RE.sub("", t)
    t = t.replace("\u3000", " ")
    t = _WS_RE.sub(" ", t).strip()
    return t.strip(_PUNCT_CHARS)


def _collapse_ws(text: str) -> str:
    """与 AstrBot CommandFilter 完全一致的空白处理。"""
    return _WS_RE.sub(" ", (text or "").strip())


def _strip_lead_noise(text: str, limit: int = 2) -> str:
    """剥掉名字前面残留的说明词（「效果雷伊」→「雷伊」）。"""
    s = text
    for _ in range(limit):
        for noise in _LEAD_NOISE:
            if s.startswith(noise) and len(s) > len(noise):
                s = s[len(noise) :].strip(_PUNCT_CHARS)
                break
        else:
            return s
    return s


def _strip_question_tail(text: str, limit: int = 2) -> str:
    """剥掉句尾的疑问成分：「雷伊的魂印是什么」→「雷伊的魂印」。"""
    s = text.strip(_PUNCT_CHARS)
    for _ in range(limit):
        for tail in _QUESTION_TAILS:
            if s.endswith(tail) and len(s) > len(tail):
                s = s[: -len(tail)].strip(_PUNCT_CHARS)
                break
        else:
            return s
    return s


def _strip_filler(text: str, limit: int = 3) -> str:
    """剥掉开头的「查一下」「帮我」「发一下」等修饰词。"""
    s = text.strip(_PUNCT_CHARS)
    for _ in range(limit):
        for filler in _FILLER_PREFIXES:
            if s.startswith(filler) and len(s) > len(filler):
                s = s[len(filler) :].strip(_PUNCT_CHARS)
                break
        else:
            match = _REQUEST_PREFIX_RE.match(s)
            if match and 0 < match.end() < len(s):
                s = s[match.end() :].strip(_PUNCT_CHARS)
                continue
            return s
    return s


def _looks_like_name(text: str) -> bool:
    """结构判断：是否像一个可以拿去查数据的名字。"""
    s = text.strip(_PUNCT_CHARS)
    if not s or len(s) > 16:
        return False
    if "\n" in s:
        return False
    return True


def _looks_like_glued_name(text: str) -> bool:
    """无分隔符拼接时的额外保守判断，避免把闲聊当成查询。"""
    if not _looks_like_name(text):
        return False
    s = text.strip(_PUNCT_CHARS)
    if " " in s:
        # 名字里不该有空格，有空格说明后面还跟着句子
        return False
    if s.startswith(_CHATTY_HEADS):
        return False
    if s.endswith(_CHATTY_TAILS):
        return False
    if any(word in s for word in _CHATTY_ANYWHERE):
        return False
    return True


def _strip_tail_noise(text: str, limit: int = 3) -> str:
    s = text
    for _ in range(limit):
        for noise in _TAIL_NOISE:
            if s.endswith(noise) and len(s) > len(noise):
                s = s[: -len(noise)]
                break
        else:
            return s
    return s


def _clean_arg(text: str) -> str:
    """把指令词后面的那一段整理成查询参数。"""
    s = text.strip(_PUNCT_CHARS)
    s = s.lstrip("的之是叫为")
    s = _strip_filler(s, limit=3)
    s = s.strip(_PUNCT_CHARS)
    s = _strip_lead_noise(s)
    # 与 AstrBot 原行为保持一致：只取第一个词
    if s and " " in s:
        s = s.split(" ", 1)[0]
    s = _strip_tail_noise(s)
    return s.strip(_PUNCT_CHARS)


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Query:
    """解析结果。"""

    intent: Intent
    arg: str = ""

    @property
    def key(self) -> str:
        return self.intent.key

    @property
    def takes_arg(self) -> bool:
        return self.intent.takes_arg


def _match_prefix(text: str) -> tuple[Intent, str] | None:
    for kw in _KEYWORDS_LONG_FIRST:
        if not text.startswith(kw):
            continue
        rest = text[len(kw) :]
        if not rest:
            return _KEYWORD_INTENT[kw], rest
        head = rest[0]
        if head in _SEPARATORS or head in _GLUE_CHARS:
            return _KEYWORD_INTENT[kw], rest
        if _looks_like_glued_name(rest):
            return _KEYWORD_INTENT[kw], rest
    return None


def _match_suffix(text: str) -> tuple[Intent, str] | None:
    for kw in _KEYWORDS_LONG_FIRST:
        if len(text) <= len(kw) or not text.endswith(kw):
            continue
        head = text[: -len(kw)]
        intent = _KEYWORD_INTENT[kw]
        if not intent.takes_arg:
            # 「今天开服了吗」「这周预告」：指令词本身已表达完整意图
            return intent, ""
        tail = head[-1]
        if tail in _SEPARATORS or tail in _GLUE_CHARS:
            return intent, head
        if _looks_like_glued_name(head):
            return intent, head
    return None


def _parse_command(text: str) -> Query | None:
    stripped_text = text.strip(_PUNCT_CHARS)
    working = _strip_question_tail(text)
    tail_stripped = working != stripped_text
    for _ in range(4):
        hit = _match_prefix(working) or _match_suffix(working)
        if hit is not None:
            intent, rest = hit
            if not intent.takes_arg:
                return Query(intent, "")
            arg = _clean_arg(rest)
            if arg and arg in _KEYWORD_INTENT:
                # 「精灵的属性」：剩下的部分本身是另一个指令词，
                # 按那个指令处理（参数为空，由 handler 提示用法）
                return Query(_KEYWORD_INTENT[arg], "")
            if tail_stripped and not arg:
                # 剥掉疑问尾巴后只剩下裸指令词（如「精灵怎么样」「魂印是什么」），
                # 那是在闲聊/泛问，不该当成查询
                return None
            return Query(intent, arg)
        stripped = _strip_filler(working)
        if stripped == working:
            break
        working = stripped
    return None


def _parse_bare(text: str) -> Query | None:
    """整条消息就是一个名字（可选功能，默认关闭）。"""
    candidate = _strip_filler(text)
    candidate = _clean_arg(candidate)
    if len(candidate) < 2 or not _looks_like_name(candidate):
        return None
    if not re.fullmatch(r"[\w\u4e00-\u9fff·]+", candidate):
        return None
    return Query(_PET_INTENT, candidate)


def parse_query(text: str, *, allow_bare: bool = False) -> Query | None:
    """把一条消息解析成指令；识别不出时返回 ``None``。"""
    normalized = _normalize(text)
    if not normalized:
        return None
    query = _parse_command(normalized)
    if query is not None:
        return query
    if allow_bare:
        return _parse_bare(normalized)
    return None


def matches_strict_command(text: str) -> bool:
    """判断这条消息是否会被 AstrBot 的 CommandFilter 直接匹配。

    严格指令已经能处理的消息必须交给它，否则同一个 handler 集合里
    会有两个处理器同时命中，重复回复。
    """
    message = _collapse_ws(text)
    if not message:
        return False
    for name in _STRICT_NAMES:
        if message == name or message.startswith(f"{name} "):
            return True
    return False


# 只给管理员用的指令：不参与宽松解析，但 AI 兜底必须认识它们，
# 否则「收录配置图 南霜 [作者:…]」会被当成自然语言再查一遍。
ADMIN_COMMANDS: tuple[str, ...] = ("收录配置图", "配置备注")


def is_known_command(text: str) -> bool:
    """消息是否以任意已注册指令开头（含仅管理指令）。"""
    message = _collapse_ws(text)
    if not message:
        return False
    for name in (*_STRICT_NAMES, *ADMIN_COMMANDS):
        if message == name or message.startswith(f"{name} "):
            return True
    return False


def bare_name_candidates(text: str) -> tuple[str, ...]:
    """把一条消息清洗成「可能的候选名字」，去重保序。

    * 原样（去掉首尾标点）：保留以「查/看/赛」开头的真名字（如 `查尔顿`）
    * 去掉修饰词后的：`查一下漫游者` → `漫游者`
    """
    raw = _normalize(text)
    if not raw:
        return ()
    candidates: list[str] = []
    for cand in (raw, _strip_filler(_strip_question_tail(raw))):
        cand = cand.strip(_PUNCT_CHARS)
        if cand and cand not in candidates:
            candidates.append(cand)
    return tuple(candidates)


def looks_like_bare_name(text: str) -> bool:
    """整条消息是不是一个「可能的名字」。

    给「裸名自动识别」用：先做结构判断，真正的确认交给数据库
    （在各表里查一下，查得到才算）。
    """
    for cand in bare_name_candidates(text):
        if not (2 <= len(cand) <= 20):
            continue
        if " " in cand:
            continue
        if _looks_like_glued_name(cand):
            return True
    return False
